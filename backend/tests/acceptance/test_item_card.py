# ruff: noqa: F811
"""Пункт и карточка пункта по контракту: исполнитель, нагрузка, пробелы, действия, история.

«Сегодня» = четверг 2026-10-15; время замораживает фикстура `clock`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MeetingParticipant
from app.modules.items.models import ActorType, DoneBy, ItemAction, ItemEvent, ItemStatus
from app.modules.series.models import MeetingStatus
from tests.acceptance.helpers import act, frozen
from tests.acceptance.helpers_items import (
    API,
    TODAY,
    clock,  # noqa: F401
    events,
    if_match,
    make_ctx,
)
from tests.acceptance.test_fr_52_events_journal import (  # noqa: F401
    ALIAS,
    FULL_NAME,
    admin,
    anonymize,
    create_item,
    last_frame,
    live,
)
from tests.factories import (
    World,
    add_member,
    events_named,
    make_event,
    make_gap,
    make_item,
    make_meeting,
    make_organization,
    make_person,
    make_series,
    make_user,
)

pytestmark = pytest.mark.usefixtures("clock")

SOON = TODAY + timedelta(days=3)
SENT = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
ALLOWED = {"edit", "done", "reschedule", "cancel", "reassign", "reopen"}


def card(client: TestClient, item) -> dict:
    resp = client.get(f"{API}/items/{item.id}")
    assert resp.status_code == 200
    return resp.json()


def load(world: World, person, n: int, *, status=ItemStatus.PENDING, due=SOON):
    return [
        make_item(
            world.db,
            world.meeting,
            assignee_person_id=person.id,
            status=status,
            due_date=due,
            sent_at=SENT,
        )
        for _ in range(n)
    ]


def new_task(world: World, person):
    return world.item(what="Новая задача", assignee_person_id=person.id)


# --- Исполнитель: PersonRef ---


@pytest.mark.fr("FR-16")
def test_card_assignee_is_person_ref(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(assignee_person_id=ctx.ivan.id)
    assert card(authed, item)["assignee"] == {
        "person_id": str(ctx.ivan.id),
        "full_name": "Иван Петров",
        "is_external": False,
    }


@pytest.mark.fr("FR-16")
def test_card_assignee_of_other_organization_is_external(authed: TestClient, world: World) -> None:
    make_ctx(world)
    outer_org = make_organization(world.db, world.user, is_own=False)
    guest = make_person(world.db, world.user, outer_org, full_name="Гость Внешний")
    add_member(world.db, world.series, guest)
    item = world.item(assignee_person_id=guest.id)
    assignee = card(authed, item)["assignee"]
    assert assignee["person_id"] == str(guest.id)
    assert assignee["is_external"] is True


@pytest.mark.fr("FR-16")
def test_patch_response_assignee_is_person_ref(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Новая задача")
    resp = authed.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(ctx.anna.id)},
        headers=if_match(item),
    )
    assert resp.status_code == 200
    assert set(resp.json()["assignee"]) == {"person_id", "full_name", "is_external"}
    assert resp.json()["assignee"]["person_id"] == str(ctx.anna.id)


@pytest.mark.fr("FR-11")
def test_created_item_assignee_is_person_ref(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    resp = authed.post(
        f"{API}/meetings/{world.meeting.id}/items", json={"raw_text": "Иван сделает отчёт"}
    )
    assert resp.status_code == 201
    assert resp.json()["assignee"]["person_id"] == str(ctx.ivan.id)


@pytest.mark.fr("FR-11")
def test_meeting_items_list_assignee_is_person_ref(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    world.item(assignee_person_id=ctx.ivan.id)
    rows = authed.get(f"{API}/meetings/{world.meeting.id}").json()["items"]
    assert [r["assignee"]["person_id"] for r in rows] == [str(ctx.ivan.id)]


@pytest.mark.fr("FR-52")
def test_anonymized_frame_assignee_keeps_person_id(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    person = make_person(world.db, world.user, world.org, full_name=FULL_NAME)
    add_member(world.db, world.series, person)
    live(world)
    item = create_item(authed, world, "Подготовить отчёт к пятнице", person)
    assert last_frame(world, item["id"])["assignee"]["person_id"] == str(person.id)
    anonymize(authed, admin, person)
    assignee = last_frame(world, item["id"])["assignee"]
    assert assignee["person_id"] == str(person.id)
    assert ALIAS.fullmatch(assignee["full_name"])


# --- Предупреждение о нагрузке: load_warning (BR-9) ---


@pytest.mark.fr("FR-26")
def test_load_warning_has_open_items_at_threshold(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 3)
    load(world, ctx.ivan, 2, status=ItemStatus.ACCEPTED)
    assert card(authed, new_task(world, ctx.ivan))["load_warning"] == {"open_items": 5}


@pytest.mark.fr("FR-26")
def test_load_warning_is_null_below_threshold(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 4)
    body = card(authed, new_task(world, ctx.ivan))
    assert "load_warning" in body
    assert body["load_warning"] is None


@pytest.mark.fr("FR-26")
def test_load_warning_is_null_without_assignee(authed: TestClient, world: World) -> None:
    make_ctx(world)
    body = card(authed, world.item())
    assert "load_warning" in body
    assert body["load_warning"] is None


@pytest.mark.fr("FR-26")
@pytest.mark.parametrize(
    ("status", "due", "expected"),
    [
        (ItemStatus.ACCEPTED, SOON, {"open_items": 5}),
        (ItemStatus.PENDING, TODAY + timedelta(days=8), None),
        (ItemStatus.DONE, SOON, None),
        (ItemStatus.CANCELLED, SOON, None),
        (ItemStatus.DISPUTED, SOON, None),
    ],
    ids=["accepted", "due-in-8-days", "done", "cancelled", "disputed"],
)
def test_load_warning_counting_rule(
    authed: TestClient, world: World, status: ItemStatus, due: date, expected
) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 4)
    extra = {}
    if status == ItemStatus.DONE:
        extra = {"done_at": SENT}
    if status == ItemStatus.CANCELLED:
        extra = {"cancelled_at": SENT}
    make_item(
        world.db,
        world.meeting,
        assignee_person_id=ctx.ivan.id,
        status=status,
        due_date=due,
        sent_at=SENT,
        **extra,
    )
    assert card(authed, new_task(world, ctx.ivan))["load_warning"] == expected


@pytest.mark.fr("FR-26")
def test_load_warning_uses_series_threshold(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    world.series.load_warning_threshold = 3
    world.db.flush()
    load(world, ctx.ivan, 3)
    assert card(authed, new_task(world, ctx.ivan))["load_warning"] == {"open_items": 3}


@pytest.mark.fr("FR-26")
def test_meeting_items_list_has_load_warning_key(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    world.item(assignee_person_id=ctx.ivan.id)
    rows = authed.get(f"{API}/meetings/{world.meeting.id}").json()["items"]
    assert rows
    assert all("load_warning" in r for r in rows)


@pytest.mark.fr("FR-26")
def test_create_and_patch_responses_have_load_warning(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 5)
    created = authed.post(
        f"{API}/meetings/{world.meeting.id}/items",
        json={"raw_text": "Иван разберётся с выгрузкой к пятнице"},
    )
    assert created.status_code == 201
    assert created.json()["load_warning"] == {"open_items": 5}
    item = world.item(what="Новая задача")
    patched = authed.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(ctx.ivan.id)},
        headers=if_match(item),
    )
    assert patched.json()["load_warning"] == {"open_items": 5}


@pytest.mark.fr("FR-26")
def test_item_has_no_overload_warning_field(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 5)
    body = card(authed, new_task(world, ctx.ivan))
    assert "overload_warning" not in body
    assert "open_items" not in (body["assignee"] or {})


@pytest.mark.fr("FR-26")
def test_openapi_item_schema_has_load_warning() -> None:
    props = app.openapi()["components"]["schemas"]["ItemRead"]["properties"]
    assert "load_warning" in props
    assert "overload_warning" not in props


@pytest.mark.fr("FR-26")
def test_overload_event_is_still_written_on_assignment(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 5)
    item = world.item(what="Новая задача")
    resp = authed.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(ctx.ivan.id)},
        headers=if_match(item),
    )
    assert resp.status_code == 200
    [event] = events(world.db, "overload_warning_shown")
    assert event.props["open_items"] == 5


# --- Вопрос пробела всегда заполнен, до 120 символов ---


@pytest.mark.fr("FR-20")
def test_gap_without_stored_question_gets_template(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item = world.item(what="Подготовить отчёт")
    make_gap(world.db, item, question=None)
    [gap] = card(authed, item)["gaps"]
    assert gap["question"]
    assert len(gap["question"]) <= 120


@pytest.mark.fr("FR-20")
def test_gap_with_long_stored_question_is_cut_to_120(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item = world.item(what="Подготовить отчёт")
    make_gap(world.db, item, question="в" * 250)
    [gap] = card(authed, item)["gaps"]
    assert 0 < len(gap["question"]) <= 120


@pytest.mark.fr("FR-20")
def test_openapi_gap_question_is_required_string_up_to_120() -> None:
    schemas = app.openapi()["components"]["schemas"]
    gap = schemas.get("Gap") or schemas["GapRead"]
    assert "question" in gap["required"]
    assert gap["properties"]["question"].get("type") == "string"
    assert gap["properties"]["question"].get("maxLength") == 120


# --- Допустимые действия карточки по таблице переходов ---


def item_in(world: World, status: ItemStatus, person):
    extra = {}
    if status == ItemStatus.DONE:
        extra = {"done_at": SENT, "done_by": DoneBy.HOST}
    if status == ItemStatus.CANCELLED:
        extra = {"cancelled_at": SENT}
    if status != ItemStatus.DRAFT:
        extra["sent_at"] = SENT
    return world.item(
        what="Подготовить отчёт",
        assignee_person_id=person.id,
        status=status,
        due_date=SOON,
        **extra,
    )


TRANSITIONS = [
    (ItemStatus.PENDING, {"edit", "done", "reschedule", "cancel", "reassign"}),
    (ItemStatus.ACCEPTED, {"edit", "done", "reschedule", "cancel", "reassign"}),
    (ItemStatus.DISPUTED, {"edit", "cancel", "reassign"}),
    (ItemStatus.DONE, {"reopen"}),
    (ItemStatus.CANCELLED, set()),
]


@pytest.mark.fr("FR-43")
@pytest.mark.parametrize(("status", "expected"), TRANSITIONS, ids=[t[0] for t in TRANSITIONS])
def test_allowed_actions_follow_transition_table(
    authed: TestClient, world: World, status: ItemStatus, expected: set[str]
) -> None:
    ctx = make_ctx(world)
    item = item_in(world, status, ctx.ivan)
    assert set(card(authed, item)["allowed_actions"]) == expected


@pytest.mark.fr("FR-43")
@pytest.mark.parametrize("status", list(ItemStatus))
def test_allowed_actions_are_known_actions(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    ctx = make_ctx(world)
    actions = card(authed, item_in(world, status, ctx.ivan))["allowed_actions"]
    assert isinstance(actions, list)
    assert set(actions) <= ALLOWED


@pytest.mark.fr("FR-13")
def test_draft_allows_only_edit(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    actions = card(authed, item_in(world, ItemStatus.DRAFT, ctx.ivan))["allowed_actions"]
    assert set(actions) <= {"edit"}


@pytest.mark.fr("FR-43")
def test_action_missing_from_allowed_actions_is_rejected(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = item_in(world, ItemStatus.DISPUTED, ctx.ivan)
    assert "done" not in set(card(authed, item)["allowed_actions"])
    assert act(authed, item, "done").status_code == 409


# --- История пункта ---


def seed_events(world: World, n: int) -> tuple[object, list[int]]:
    item = world.item()
    base = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
    made = [
        make_event(
            world.db,
            item,
            action=ItemAction.UPDATED,
            actor_type=ActorType.HOST,
            created_at=base + timedelta(minutes=i),
        ).id
        for i in range(n)
    ]
    return item, made


def history(client: TestClient, item, **params):
    return client.get(f"{API}/items/{item.id}/history", params=params)


@pytest.mark.fr("FR-45")
def test_history_is_object_with_events_newest_first(authed: TestClient, world: World) -> None:
    item, ids = seed_events(world, 3)
    resp = history(authed, item)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"events", "next_cursor"}
    assert [e["id"] for e in body["events"]] == sorted(ids, reverse=True)
    assert body["next_cursor"] is None


@pytest.mark.fr("FR-45")
def test_history_event_fields(authed: TestClient, world: World) -> None:
    item = world.item()
    make_event(
        world.db,
        item,
        action=ItemAction.DONE,
        actor_type=ActorType.HOST,
        actor_user_id=world.user.id,
        data={"status_from": "pending", "status_to": "done"},
    )
    [event] = history(authed, item).json()["events"]
    assert isinstance(event["id"], int)
    assert event["actor_type"] == "host"
    assert event["actor_name"] == world.user.full_name
    assert event["action"] == "done"
    assert event["data"] == {"status_from": "pending", "status_to": "done"}
    assert event["created_at"]


@pytest.mark.fr("FR-45")
def test_history_pages_by_cursor_without_gaps_or_repeats(authed: TestClient, world: World) -> None:
    item, ids = seed_events(world, 5)
    first = history(authed, item, limit=2).json()
    assert len(first["events"]) == 2
    assert first["next_cursor"] is not None
    seen = [e["id"] for e in first["events"]]
    cursor = first["next_cursor"]
    for _ in range(5):
        page = history(authed, item, limit=2, cursor=cursor).json()
        seen += [e["id"] for e in page["events"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert seen == sorted(ids, reverse=True)


@pytest.mark.fr("FR-45")
def test_history_next_cursor_is_null_when_limit_exceeds_count(
    authed: TestClient, world: World
) -> None:
    item, _ = seed_events(world, 3)
    body = history(authed, item, limit=10).json()
    assert len(body["events"]) == 3
    assert body["next_cursor"] is None


@pytest.mark.fr("FR-45")
def test_history_default_limit_is_50(authed: TestClient, world: World) -> None:
    item, _ = seed_events(world, 55)
    body = history(authed, item).json()
    assert len(body["events"]) == 50
    assert body["next_cursor"] is not None


@pytest.mark.fr("FR-45")
@pytest.mark.parametrize("limit", [0, 101, -1])
def test_history_limit_out_of_range_is_422(authed: TestClient, world: World, limit: int) -> None:
    item, _ = seed_events(world, 1)
    assert history(authed, item, limit=limit).status_code == 422


@pytest.mark.fr("FR-45")
def test_history_limit_100_is_allowed(authed: TestClient, world: World) -> None:
    item, _ = seed_events(world, 2)
    resp = history(authed, item, limit=100)
    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 2


@pytest.mark.fr("FR-45")
def test_history_of_foreign_item_is_404(authed: TestClient, world: World) -> None:
    other = make_series(world.db, make_user(world.db))
    foreign = make_item(world.db, make_meeting(world.db, other))
    assert history(authed, foreign).status_code == 404


# --- Строго больше 2 рабочих дней от sent_at ---

THURSDAY = "2026-10-15 09:00:00+00:00"


def sent_pending(world: World, when: str):
    return world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.fromisoformat(when),
        due_date=date(2099, 1, 1),
    )


def is_stale(client: TestClient, world: World, now: str, item) -> bool:
    with frozen(client, world.user, now):
        data = client.get(f"{API}/meetings/{world.meeting.id}/review").json()
    return str(item.id) in {r["id"] for r in data["stale_pending"]}


EXACTLY_TWO = [
    ("2026-10-14 09:00:00+00:00", "2026-10-16 09:00:00+00:00"),
    ("2026-10-15 09:00:00+00:00", "2026-10-19 09:00:00+00:00"),
    ("2026-10-16 09:00:00+00:00", "2026-10-20 09:00:00+00:00"),
    ("2026-10-17 09:00:00+00:00", "2026-10-20 09:00:00+00:00"),
]


@pytest.mark.fr("FR-42")
@pytest.mark.parametrize(("sent_at", "now"), EXACTLY_TWO, ids=str)
def test_pending_sent_exactly_two_business_days_ago_is_not_in_review(
    authed: TestClient, world: World, sent_at: str, now: str
) -> None:
    assert is_stale(authed, world, now, sent_pending(world, sent_at)) is False


@pytest.mark.fr("FR-42")
@pytest.mark.parametrize(
    ("sent_at", "now"),
    [
        ("2026-10-14 09:00:00+00:00", "2026-10-19 09:00:00+00:00"),
        ("2026-10-16 09:00:00+00:00", "2026-10-21 09:00:00+00:00"),
        ("2026-10-12 09:00:00+00:00", THURSDAY),
    ],
    ids=str,
)
def test_pending_sent_more_than_two_business_days_ago_is_in_review(
    authed: TestClient, world: World, sent_at: str, now: str
) -> None:
    assert is_stale(authed, world, now, sent_pending(world, sent_at)) is True


# --- Событие review_action пишется только из сверки идущей встречи ---


@pytest.mark.fr("FR-50")
def test_patch_writes_no_item_edited_event(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item = world.item(what="Подготовить отчёт")
    resp = authed.patch(
        f"{API}/items/{item.id}", json={"what": "Выгрузить отчёт"}, headers=if_match(item)
    )
    assert resp.status_code == 200
    assert events(world.db, "item_edited") == []


@pytest.mark.fr("FR-50")
def test_track_item_edited_writes_nothing(world: World) -> None:
    from app.modules.analytics.service import track

    assert track(world.db, "item_edited", fields=["what"]) is None
    assert events(world.db, "item_edited") == []


NEW_DUE = "2026-11-27"
REVIEW_ACTIONS = [
    ("done", {}),
    ("reschedule", {"due_date": NEW_DUE}),
    ("cancel", {}),
    ("reassign", {"assignee_person_id": "NEW"}),
]
ACTION_IDS = [a[0] for a in REVIEW_ACTIONS]


@dataclass
class Review:
    world: World
    item: object
    live: object


def overdue_item(world: World):
    add_member(world.db, world.series, world.person)
    return world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.ACCEPTED,
        due_date=date(2026, 10, 1),
        sent_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


@pytest.fixture
def in_review(world: World) -> Review:
    world.meeting.status = MeetingStatus.SENT
    item = overdue_item(world)
    started = make_meeting(world.db, world.series, status=MeetingStatus.LIVE)
    return Review(world, item, started)


def action_args(world: World, extra: dict) -> dict:
    if extra.get("assignee_person_id") != "NEW":
        return extra
    person = make_person(world.db, world.user, world.org, full_name="Новиков Новый")
    add_member(world.db, world.series, person)
    return {"assignee_person_id": str(person.id)}


def review_actions(world: World, item):
    return events_named(world.db, "review_action", item_id=item.id)


@pytest.mark.fr("FR-43")
@pytest.mark.parametrize(("action", "extra"), REVIEW_ACTIONS, ids=ACTION_IDS)
def test_action_from_review_of_live_meeting_writes_review_action(
    authed: TestClient, in_review: Review, action: str, extra: dict
) -> None:
    w = in_review.world
    review = authed.get(f"{API}/meetings/{in_review.live.id}/review").json()
    assert str(in_review.item.id) in {r["id"] for r in review["overdue"]}
    assert act(authed, in_review.item, action, **action_args(w, extra)).status_code == 200
    assert [e.props["action"] for e in review_actions(w, in_review.item)] == [action]


@pytest.mark.fr("FR-43")
@pytest.mark.parametrize(("action", "extra"), REVIEW_ACTIONS, ids=ACTION_IDS)
def test_card_action_without_live_meeting_writes_no_review_action(
    authed: TestClient, world: World, action: str, extra: dict
) -> None:
    world.meeting.status = MeetingStatus.SENT
    item = overdue_item(world)
    assert act(authed, item, action, **action_args(world, extra)).status_code == 200
    assert review_actions(world, item) == []
    host_rows = world.db.query(ItemEvent).filter(
        ItemEvent.item_id == item.id, ItemEvent.actor_type == ActorType.HOST
    )
    assert host_rows.count() == 1


@pytest.mark.fr("FR-43")
@pytest.mark.parametrize(("action", "extra"), REVIEW_ACTIONS, ids=ACTION_IDS)
def test_action_after_skipped_review_writes_no_review_action(
    authed: TestClient, in_review: Review, action: str, extra: dict
) -> None:
    w = in_review.world
    assert authed.post(f"{API}/meetings/{in_review.live.id}/review/skip").status_code == 204
    assert act(authed, in_review.item, action, **action_args(w, extra)).status_code == 200
    assert review_actions(w, in_review.item) == []


@pytest.mark.fr("FR-43")
def test_action_on_item_outside_review_writes_no_review_action(
    authed: TestClient, in_review: Review
) -> None:
    w = in_review.world
    fresh = make_item(
        w.db,
        w.meeting,
        assignee_person_id=w.person.id,
        status=ItemStatus.ACCEPTED,
        due_date=date(2099, 1, 1),
        sent_at=datetime(2026, 10, 15, tzinfo=UTC),
    )
    review = authed.get(f"{API}/meetings/{in_review.live.id}/review").json()
    assert str(fresh.id) not in {r["id"] for rows in review.values() for r in rows}
    assert act(authed, fresh, "cancel").status_code == 200
    assert review_actions(w, fresh) == []


# --- Падеж имени через pymorphy3 ---


@dataclass(frozen=True)
class P:
    id: uuid.UUID
    full_name: str


LYUBOV = uuid.UUID(int=21)
STAFF = [P(uuid.UUID(int=12), "Иван Петров"), P(LYUBOV, "Любовь Орлова")]


@pytest.mark.fr("FR-11")
@pytest.mark.parametrize(
    "text",
    [
        "Поручить Любови подготовить отчёт",
        "Договор у Любови",
        "с Любовью согласовать договор",
        "Отчёт для Любови к пятнице",
    ],
)
def test_assignee_name_in_any_case_is_found_with_morphology(text: str) -> None:
    from app.modules.items.parsing import parse_line

    assert parse_line(text, TODAY, STAFF).assignee_person_id == LYUBOV


@pytest.mark.fr("FR-11")
def test_pymorphy3_is_declared_in_pyproject() -> None:
    root = Path(__file__).resolve().parents[2]
    assert "pymorphy3" in (root / "pyproject.toml").read_text(encoding="utf-8")


# --- «Согласовано» с отсутствующим исполнителем: 422 GAPS_OPEN + причина ---


@pytest.mark.fr("FR-25")
def test_accept_with_absent_assignee_is_422_gaps_open_with_reason(
    authed: TestClient, world: World
) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=SOON)
    world.db.get(MeetingParticipant, (world.meeting.id, ctx.ivan.id)).present = False
    world.db.flush()
    body = card(authed, item)
    assert body["can_accept_in_meeting"] is False
    assert body["accept_blocked_reason"] == "Исполнитель отсутствует на встрече"
    resp = authed.post(
        f"{API}/items/{item.id}/accept-in-meeting", json={"value": True}, headers=if_match(item)
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "GAPS_OPEN"
