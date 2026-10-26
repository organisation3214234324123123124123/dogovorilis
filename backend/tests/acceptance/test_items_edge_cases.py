import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Item, Notification
from app.modules.gaps.models import GapType
from app.modules.items import service as items_service
from app.modules.items.models import ItemStatus
from app.modules.live import service as live_service
from app.modules.notifications.models import NotificationStatus, NotificationType
from tests.acceptance.helpers_items import (
    API,
    TODAY,
    clock,  # noqa: F401
    events,
    if_match,
    make_ctx,
    notifications,
    problem_code,
)
from tests.conftest import login_as
from tests.factories import (
    World,
    make_agenda_item,
    make_gap,
    make_item,
    make_meeting,
    make_notification,
    make_series,
)

pytestmark = pytest.mark.usefixtures("clock")

SOON = TODAY + timedelta(days=3)
SENT_AT = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
DUE = date(2026, 10, 20)


def load(world: World, person, n: int, *, status=ItemStatus.PENDING, due=SOON, meeting=None):
    extra = {"done_at": SENT_AT} if status == ItemStatus.DONE else {}
    return [
        make_item(
            world.db,
            meeting or world.meeting,
            assignee_person_id=person.id,
            status=status,
            due_date=due,
            sent_at=SENT_AT,
            **extra,
        )
        for _ in range(n)
    ]


def patch(client: TestClient, item, body: dict):
    return client.patch(f"{API}/items/{item.id}", json=body, headers=if_match(item))


def assign(client: TestClient, world: World, person):
    item = world.item(what="Новая задача")
    return patch(client, item, {"assignee_person_id": str(person.id)})


def post_item(client: TestClient, meeting_id, text: str = "Иван сделает отчёт", **extra):
    return client.post(f"{API}/meetings/{meeting_id}/items", json={"raw_text": text, **extra})


@pytest.mark.fr("FR-26")
def test_members_list_returns_open_items_per_person(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 3)
    load(world, ctx.ivan, 1, status=ItemStatus.ACCEPTED)
    load(world, ctx.ivan, 1, status=ItemStatus.DONE, due=SOON)
    load(world, ctx.ivan, 1, due=TODAY + timedelta(days=30))
    resp = authed.get(f"{API}/series/{world.series.id}/members")
    assert resp.status_code == 200
    counts = {m["person_id"]: m["open_items"] for m in resp.json()}
    assert counts[str(ctx.ivan.id)] == 4
    assert counts[str(ctx.anna.id)] == 0


@pytest.mark.fr("FR-26")
def test_assignee_in_item_response_carries_open_items(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 2)
    resp = assign(authed, world, ctx.ivan)
    assert resp.status_code == 200
    assert resp.json()["assignee"]["open_items"] == 2


@pytest.mark.fr("FR-26")
def test_patch_response_has_overload_warning_at_threshold(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 5)
    resp = assign(authed, world, ctx.ivan)
    assert resp.status_code == 200
    assert resp.json()["overload_warning"] == {"open_items": 5, "threshold": 5}


@pytest.mark.fr("FR-26")
def test_patch_response_has_no_warning_below_threshold(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 4)
    assert assign(authed, world, ctx.ivan).json()["overload_warning"] is None


@pytest.mark.fr("FR-26")
def test_warning_uses_series_threshold(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    world.series.load_warning_threshold = 3
    world.db.flush()
    load(world, ctx.ivan, 3)
    assert assign(authed, world, ctx.ivan).json()["overload_warning"] == {
        "open_items": 3,
        "threshold": 3,
    }


@pytest.mark.fr("FR-26")
def test_create_response_has_overload_warning(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 5)
    resp = post_item(authed, world.meeting.id, "Иван разберётся с выгрузкой к пятнице")
    assert resp.status_code == 201
    assert resp.json()["overload_warning"] == {"open_items": 5, "threshold": 5}


@pytest.mark.fr("FR-26")
def test_event_is_written_exactly_when_response_has_warning(
    authed: TestClient, world: World
) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 5)
    shown = assign(authed, world, ctx.ivan).json()
    quiet = assign(authed, world, ctx.anna).json()
    assert shown["overload_warning"] is not None
    assert quiet["overload_warning"] is None
    [event] = events(world.db, "overload_warning_shown")
    assert event.props["open_items"] == 5
    assert str(event.item_id) == shown["id"]


@pytest.mark.fr("FR-26")
def test_get_item_has_no_overload_warning(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 5)
    item = world.item(assignee_person_id=ctx.ivan.id)
    body = authed.get(f"{API}/items/{item.id}").json()
    assert body.get("overload_warning") is None
    assert events(world.db, "overload_warning_shown") == []


# --- слияние ITEM_CHANGED по получателю (FR-16, BR-16) ---


def sent_item(world: World, ctx) -> Item:
    return world.item(
        status=ItemStatus.PENDING,
        sent_at=SENT_AT,
        what="Старое",
        assignee_person_id=ctx.ivan.id,
        due_date=DUE,
    )


def changed_row(world: World, item, person, status, key: str) -> Notification:
    return make_notification(
        world.db,
        type=NotificationType.ITEM_CHANGED,
        recipient_person_id=person.id,
        meeting_id=world.meeting.id,
        item_id=item.id,
        dedupe_key=key,
        status=status,
        payload={"what": {"old": "Старое", "new": "Раннее"}},
    )


@pytest.mark.fr("FR-16")
def test_edit_after_letter_was_sent_creates_new_row(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    old = changed_row(world, item, ctx.ivan, NotificationStatus.SENT, f"{item.id}:ITEM_CHANGED:0")
    assert patch(authed, item, {"what": "Новое"}).status_code == 200
    world.db.expire_all()
    rows = notifications(world.db, NotificationType.ITEM_CHANGED, item.id)
    assert world.db.get(Notification, old.id).status == NotificationStatus.SENT
    fresh = [r for r in rows if r.id != old.id]
    assert [r.status for r in fresh] == [NotificationStatus.QUEUED]
    assert "Новое" in json.dumps(fresh[0].payload, ensure_ascii=False)


@pytest.mark.fr("FR-16")
def test_queued_row_of_another_recipient_is_not_merged(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    foreign = changed_row(
        world, item, ctx.anna, NotificationStatus.QUEUED, f"{item.id}:ITEM_CHANGED:0"
    )
    assert patch(authed, item, {"what": "Новое"}).status_code == 200
    world.db.expire_all()
    rows = notifications(world.db, NotificationType.ITEM_CHANGED, item.id)
    mine = [r for r in rows if r.recipient_person_id == ctx.ivan.id]
    assert len(mine) == 1
    assert mine[0].status == NotificationStatus.QUEUED
    other = world.db.get(Notification, foreign.id)
    assert other.recipient_person_id == ctx.anna.id
    assert "Новое" not in json.dumps(other.payload, ensure_ascii=False)


@pytest.mark.fr("FR-16")
def test_cancelled_row_is_not_reused(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    dead = changed_row(
        world, item, ctx.ivan, NotificationStatus.CANCELLED, f"{item.id}:ITEM_CHANGED:0"
    )
    patch(authed, item, {"what": "Новое"})
    world.db.expire_all()
    assert world.db.get(Notification, dead.id).status == NotificationStatus.CANCELLED
    queued = [
        r
        for r in notifications(world.db, NotificationType.ITEM_CHANGED, item.id)
        if r.status == NotificationStatus.QUEUED
    ]
    assert len(queued) == 1


# Лимит 60 решений в минуту считается по БД (NFR-10)


def seed_captured(world: World, n: int, created_at=None) -> None:
    for _ in range(n):
        kw = {} if created_at is None else {"created_at": created_at}
        make_item(world.db, world.meeting, **kw)


@pytest.mark.fr("FR-10")
def test_limit_counts_rows_already_in_database(authed: TestClient, world: World) -> None:
    make_ctx(world)
    seed_captured(world, 60)
    resp = post_item(authed, world.meeting.id)
    assert resp.status_code == 429
    assert problem_code(resp) == "RATE_LIMITED"
    assert int(resp.headers["Retry-After"]) > 0


@pytest.mark.fr("FR-10")
def test_limit_shared_between_seeded_rows_and_requests(authed: TestClient, world: World) -> None:
    make_ctx(world)
    seed_captured(world, 59)
    assert post_item(authed, world.meeting.id).status_code == 201
    assert post_item(authed, world.meeting.id).status_code == 429


@pytest.mark.fr("FR-10")
def test_rows_older_than_a_minute_are_not_counted(authed: TestClient, world: World) -> None:
    make_ctx(world)
    db_now = world.db.scalar(select(func.now()))
    seed_captured(world, 60, created_at=db_now - timedelta(seconds=120))
    assert post_item(authed, world.meeting.id).status_code == 201


@pytest.mark.fr("FR-10")
def test_limit_is_per_meeting(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    seed_captured(world, 60)
    other = make_meeting(world.db, make_series(world.db, world.user), status="live")
    del ctx
    assert post_item(authed, other.id).status_code == 201


@pytest.mark.fr("FR-24")
def test_dismiss_three_times_gives_one_event(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id)
    gap = make_gap(world.db, item, type=GapType.NO_DUE)
    codes = [
        authed.post(f"{API}/items/{item.id}/gaps/{gap.id}/dismiss").status_code for _ in range(3)
    ]
    assert codes == [200, 200, 200]
    assert len(events(world.db, "gap_dismissed", item_id=item.id)) == 1


# --- разбор строки ---


@dataclass(frozen=True)
class P:
    id: uuid.UUID
    full_name: str


PAVEL = uuid.UUID(int=21)
LEV = uuid.UUID(int=22)
SASHA = uuid.UUID(int=23)
IVAN = uuid.UUID(int=12)
COMPOSITION = [
    P(PAVEL, "Павел Орлов"),
    P(LEV, "Лев Мелехов"),
    P(SASHA, "Саша Крылова"),
    P(IVAN, "Иван Петров"),
]


def parse(text: str, today: date = TODAY):
    from app.modules.items.parsing import parse_line

    return parse_line(text, today, COMPOSITION)


@pytest.mark.fr("FR-11")
@pytest.mark.parametrize(
    ("text", "assignee"),
    [
        ("Поручили Павлу подготовить отчёт", PAVEL),
        ("Передать Павлу договор", PAVEL),
        ("Павел подготовит отчёт", PAVEL),
        ("Поручили Льву подготовить отчёт", LEV),
        ("Лев подготовит отчёт", LEV),
        ("Договор у Льва", LEV),
        ("Договор согласуем с Сашей", SASHA),
        ("Саша подготовит отчёт", SASHA),
        ("Поручили Саше подготовить отчёт", SASHA),
        ("С Павлом согласовать договор", PAVEL),
    ],
)
def test_assignee_with_fleeting_vowel_and_instrumental(text: str, assignee) -> None:
    assert parse(text).assignee_person_id == assignee


@pytest.mark.fr("FR-11")
@pytest.mark.parametrize(
    ("text", "due"),
    [
        ("Иван сделает отчёт в пятницу", date(2026, 10, 16)),
        ("Иван сделает отчёт в понедельник", date(2026, 10, 19)),
        ("Иван сделает отчёт к среде", date(2026, 10, 21)),
        ("Иван сделает отчёт в среду", date(2026, 10, 21)),
        ("Иван сделает отчёт до 20 ноября", date(2026, 11, 20)),
        ("Иван сделает отчёт до 3 декабря", date(2026, 12, 3)),
        ("Иван сделает отчёт к 1 ноября", date(2026, 11, 1)),
        ("Иван сделает отчёт через две недели", date(2026, 10, 29)),
        ("Иван сделает отчёт через три недели", date(2026, 11, 5)),
        ("Иван сделает отчёт через два дня", date(2026, 10, 17)),
        ("Иван сделает отчёт через 3 дня", date(2026, 10, 18)),
        ("Иван сделает отчёт через десять дней", date(2026, 10, 25)),
        ("Иван сделает отчёт к 20.10.26", date(2026, 10, 20)),
        ("Иван сделает отчёт до 20.10.2026", date(2026, 10, 20)),
        ("Иван сделает отчёт до конца месяца", date(2026, 10, 31)),
    ],
)
def test_due_weekdays_dates_and_offsets(text: str, due: date) -> None:
    assert parse(text).due_date == due


# OQ-08: "на следующей неделе" и "до конца недели" - пятница,
# "до конца месяца" - последний день месяца. Сегодня четверг 2026-10-15.
@pytest.mark.fr("FR-11")
@pytest.mark.parametrize(
    ("text", "phrase", "due"),
    [
        ("Иван сделает отчёт на следующей неделе", "на следующей неделе", date(2026, 10, 23)),
        ("Иван сделает отчёт до конца недели", "до конца недели", date(2026, 10, 16)),
        ("Иван сделает отчёт до конца месяца", "до конца месяца", date(2026, 10, 31)),
    ],
)
def test_period_phrases_give_exact_date(text: str, phrase: str, due: date) -> None:
    parsed = parse(text)
    assert parsed.due_text is not None
    assert parsed.due_text.lower() == phrase
    assert parsed.due_date == due


@pytest.mark.fr("FR-11")
@pytest.mark.parametrize("text", ["Иван сделает отчёт к 31.02", "Иван сделает отчёт до 31.02"])
def test_impossible_date_is_vague_with_phrase(text: str) -> None:
    parsed = parse(text)
    assert parsed.due_date is None
    assert parsed.due_text is not None
    assert "31.02" in parsed.due_text


@pytest.mark.fr("FR-11")
@pytest.mark.parametrize("text", ["Иван сделает отчёт к 20.10.26", "Иван сделает отчёт к 20.10"])
def test_numeric_date_leaves_no_tail_in_what(text: str) -> None:
    what = parse(text).what
    assert what
    assert "20" not in what
    assert "26" not in what
    assert "." not in what
    assert "отчёт" in what


@pytest.mark.fr("FR-11")
@pytest.mark.parametrize(
    "text",
    [
        "Иван проверит средства защиты",
        "Иван посчитает среднюю цену",
        "Иван изучит среду разработки",
    ],
)
def test_sred_inside_a_word_is_not_a_weekday(text: str) -> None:
    # Без предлога "в/к/до" слово "среду" тоже не срок (день недели берётся только по предлогу).
    parsed = parse(text)
    assert parsed.due_date is None
    assert parsed.due_text is None


@pytest.mark.fr("FR-11")
def test_sred_inside_a_word_keeps_word_in_what() -> None:
    assert "средства" in parse("Иван проверит средства защиты").what


@pytest.mark.fr("FR-11")
@pytest.mark.parametrize(
    ("phrase", "due", "gap"),
    [("к 31.02", None, "due_vague"), ("на следующей неделе", "2026-10-23", None)],
)
def test_api_period_and_bad_date_gaps(
    authed: TestClient, world: World, phrase: str, due: str | None, gap: str | None
) -> None:
    make_ctx(world)
    resp = post_item(authed, world.meeting.id, f"Иван сделает отчёт {phrase}")
    assert resp.status_code == 201
    card = authed.get(f"{API}/items/{resp.json()['id']}").json()
    assert card["due_text"] == phrase
    assert card["due_date"] == due
    gaps = {g["type"] for g in card["gaps"]}
    if gap:
        assert gap in gaps
    else:
        assert "due_vague" not in gaps


@pytest.mark.fr("FR-25")
def test_dismissed_due_past_does_not_block_accept(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=DUE)
    gap = make_gap(world.db, item, type=GapType.DUE_PAST)
    assert authed.post(f"{API}/items/{item.id}/gaps/{gap.id}/dismiss").status_code == 200
    world.db.refresh(item)
    resp = authed.post(
        f"{API}/items/{item.id}/accept-in-meeting", json={"value": True}, headers=if_match(item)
    )
    assert resp.status_code == 200
    assert resp.json()["accepted_in_meeting"] is True


@pytest.mark.fr("FR-25")
def test_open_due_past_still_blocks_accept(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=DUE)
    make_gap(world.db, item, type=GapType.DUE_PAST)
    resp = authed.post(
        f"{API}/items/{item.id}/accept-in-meeting", json={"value": True}, headers=if_match(item)
    )
    assert resp.status_code == 422
    assert problem_code(resp) == "GAPS_OPEN"


@pytest.mark.fr("FR-25")
def test_resolved_gap_does_not_block_accept(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=DUE)
    make_gap(world.db, item, type=GapType.DUE_VAGUE, resolved_at=SENT_AT)
    resp = authed.post(
        f"{API}/items/{item.id}/accept-in-meeting", json={"value": True}, headers=if_match(item)
    )
    assert resp.status_code == 200


@pytest.mark.fr("FR-10")
def test_agenda_item_of_another_meeting_is_422(authed: TestClient, world: World) -> None:
    make_ctx(world)
    other = make_meeting(world.db, world.series)
    foreign = make_agenda_item(world.db, other)
    resp = post_item(authed, world.meeting.id, agenda_item_id=str(foreign.id))
    assert resp.status_code == 422
    assert problem_code(resp) == "VALIDATION_ERROR"
    assert world.db.query(Item).filter_by(meeting_id=world.meeting.id).count() == 0


@pytest.mark.fr("FR-10")
def test_unknown_agenda_item_is_422(authed: TestClient, world: World) -> None:
    make_ctx(world)
    resp = post_item(authed, world.meeting.id, agenda_item_id=str(uuid.uuid4()))
    assert resp.status_code == 422


@pytest.mark.fr("FR-10")
def test_own_agenda_item_is_accepted(authed: TestClient, world: World) -> None:
    make_ctx(world)
    agenda = make_agenda_item(world.db, world.meeting)
    resp = post_item(authed, world.meeting.id, agenda_item_id=str(agenda.id))
    assert resp.status_code == 201
    assert resp.json()["agenda_item_id"] == str(agenda.id)


@pytest.mark.fr("FR-10")
@pytest.mark.parametrize("text", [" ", "     ", "\t\n  "])
def test_whitespace_only_line_is_422(authed: TestClient, world: World, text: str) -> None:
    make_ctx(world)
    resp = post_item(authed, world.meeting.id, text)
    assert resp.status_code == 422
    assert problem_code(resp) == "VALIDATION_ERROR"
    assert world.db.query(Item).filter_by(meeting_id=world.meeting.id).count() == 0


@pytest.mark.fr("FR-15")
def test_delete_without_if_match_is_428(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item = world.item()
    resp = authed.delete(f"{API}/items/{item.id}")
    assert resp.status_code == 428
    assert problem_code(resp) == "PRECONDITION_REQUIRED"


@pytest.mark.fr("FR-15")
def test_delete_with_stale_version_is_409(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item = world.item()
    resp = authed.delete(f"{API}/items/{item.id}", headers={"If-Match": str(item.version + 5)})
    assert resp.status_code == 409
    assert problem_code(resp) == "VERSION_CONFLICT"


# Кадры уходят только после commit (FR-14)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, dict]]:
    sent: list[tuple[uuid.UUID, dict]] = []
    monkeypatch.setattr(live_service, "publish", lambda mid, frame: sent.append((mid, frame)))
    return sent


@pytest.mark.fr("FR-14")
def test_frame_is_not_sent_before_commit(db, world: World, published) -> None:
    live_service.publish_after_commit(db, world.meeting.id, {"type": "item.created"})
    assert published == []
    db.commit()
    assert [f["type"] for _, f in published] == ["item.created"]


@pytest.mark.fr("FR-14")
def test_frame_is_dropped_on_rollback(db, world: World, published) -> None:
    live_service.publish_after_commit(db, world.meeting.id, {"type": "item.created"})
    db.rollback()
    db.commit()
    assert published == []


@pytest.mark.fr("FR-14")
def test_dropped_frame_does_not_leak_into_next_commit(db, world: World, published) -> None:
    live_service.publish_after_commit(db, world.meeting.id, {"type": "item.created"})
    db.rollback()
    live_service.publish_after_commit(db, world.meeting.id, {"type": "item.updated"})
    db.commit()
    assert [f["type"] for _, f in published] == ["item.updated"]


@pytest.mark.fr("FR-14")
def test_failed_request_sends_no_frames(
    client: TestClient, world: World, published, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_ctx(world)

    def boom(*args, **kwargs):
        raise RuntimeError("после item.created")

    monkeypatch.setattr(items_service, "parse_line", boom)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    login_as(quiet, world.user)
    resp = post_item(quiet, world.meeting.id)
    assert resp.status_code == 500
    assert published == []


@pytest.mark.fr("FR-14")
def test_successful_request_sends_frames_after_commit(
    authed: TestClient, world: World, published
) -> None:
    make_ctx(world)
    assert post_item(authed, world.meeting.id).status_code == 201
    kinds = [f["type"] for _, f in published]
    assert kinds[0] == "item.created"
    assert {"item.updated", "gaps.updated"} <= set(kinds)
    assert {mid for mid, _ in published} == {world.meeting.id}


@pytest.mark.fr("FR-14")
def test_delete_sends_frame_only_after_commit(authed: TestClient, world: World, published) -> None:
    make_ctx(world)
    item = world.item()
    assert authed.delete(f"{API}/items/{item.id}", headers=if_match(item)).status_code in (200, 204)
    assert [f["type"] for _, f in published] == ["item.deleted"]


@pytest.mark.fr("FR-14")
def test_rejected_edit_sends_no_frames(authed: TestClient, world: World, published) -> None:
    make_ctx(world)
    item = world.item()
    resp = authed.patch(
        f"{API}/items/{item.id}", json={"what": "x"}, headers={"If-Match": str(item.version + 3)}
    )
    assert resp.status_code == 409
    assert published == []


@pytest.mark.fr("FR-14")
def test_savepoint_rollback_keeps_outer_frames(db, world: World, published) -> None:
    live_service.publish_after_commit(db, world.meeting.id, {"type": "item.created"})
    nested = db.begin_nested()
    live_service.publish_after_commit(db, world.meeting.id, {"type": "item.updated"})
    nested.rollback()
    db.commit()
    assert "item.created" in [f["type"] for _, f in published]


@pytest.mark.fr("FR-15")
@pytest.mark.parametrize("status", [ItemStatus.PENDING, ItemStatus.DONE])
def test_delete_non_draft_without_if_match_is_428(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    make_ctx(world)
    extra = {"done_at": SENT_AT} if status == ItemStatus.DONE else {}
    item = world.item(status=status, sent_at=SENT_AT, **extra)
    resp = authed.delete(f"{API}/items/{item.id}")
    assert resp.status_code == 428
    assert problem_code(resp) == "PRECONDITION_REQUIRED"


@pytest.mark.fr("FR-11")
def test_deploy_in_test_environment_has_no_due() -> None:
    parsed = parse("Иван развернёт сборку в среде тестирования")
    assert (parsed.due_date, parsed.due_text) == (None, None)


@pytest.mark.fr("FR-11")
def test_next_week_wednesday_on_monday_is_wednesday_of_next_week() -> None:
    parsed = parse("Иван сделает отчёт на следующей неделе в среду", today=date(2026, 10, 12))
    assert parsed.due_date == date(2026, 10, 21)


@pytest.mark.fr("FR-11")
def test_bad_date_what_does_not_repeat_whole_line() -> None:
    line = "Льву к 31.02"
    parsed = parse(line)
    assert parsed.assignee_person_id == LEV
    assert parsed.due_date is None
    assert parsed.what != line


@pytest.mark.fr("FR-11")
@pytest.mark.parametrize(
    ("text", "today", "due"),
    [
        ("Иван сделает отчёт к 05.01", date(2026, 12, 20), date(2027, 1, 5)),
        ("Иван сделает отчёт к 05.01", date(2026, 10, 15), date(2027, 1, 5)),
    ],
)
def test_past_date_without_year_means_next_occurrence(text: str, today: date, due: date) -> None:
    assert parse(text, today=today).due_date == due
