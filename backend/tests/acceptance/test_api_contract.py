"""Соответствие API заявленному контракту; "сегодня" - 2026-10-15."""

import csv
import io
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.models import AnalyticsEvent, Item, MeetingParticipant
from app.modules.gaps.models import GapType
from app.modules.items.models import ItemStatus
from app.modules.notifications.models import NotificationStatus, NotificationType
from app.modules.series.models import MeetingStatus
from tests.acceptance.helpers_items import (
    API,
    TODAY,
    clock,  # noqa: F401
    events,
    frame_name,
    if_match,
    make_ctx,
    problem_code,
    ws_events,
)
from tests.acceptance.helpers_mailing import digest
from tests.factories import (
    World,
    make_access_link,
    make_gap,
    make_item,
    make_meeting,
    make_notification,
    make_series,
    make_user,
)

SENT_AT = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
DUE = date(2026, 10, 20)
TOKEN = "p9-link-token-" + "z" * 24
ADMIN_TOKEN = "e" * 32

pytestmark = pytest.mark.usefixtures("clock")


def sent_item(world: World, ctx, status: ItemStatus = ItemStatus.PENDING, **kw: Any) -> Item:
    kw.setdefault("assignee_person_id", ctx.ivan.id)
    kw.setdefault("due_date", DUE)
    return world.item(status=status, sent_at=SENT_AT, what="Старое", **kw)


def reload(world: World, item: Item) -> Item:
    world.db.expire_all()
    return world.db.get(Item, item.id)


def closing_meeting(world: World, authed: TestClient):
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=DUE)
    assert authed.post(f"{API}/meetings/{world.meeting.id}/closure").status_code == 200
    return ctx, item


def do_send(client: TestClient, world: World, **body: Any):
    body.setdefault("confirm_open_gaps", True)
    return client.post(f"{API}/meetings/{world.meeting.id}/send", json=body)


# WS: meeting.sent


@pytest.mark.fr("FR-28")
def test_send_publishes_meeting_sent_frame(authed: TestClient, world: World) -> None:
    closing_meeting(world, authed)
    frames = ws_events(authed, world.meeting.id, lambda: do_send(authed, world), ("meeting.sent",))
    sent = [f for f in frames if frame_name(f) == "meeting.sent"]
    assert len(sent) == 1
    assert str(world.meeting.id) in json.dumps(sent[0])


@pytest.mark.fr("FR-28")
def test_refused_send_publishes_no_meeting_sent_frame(authed: TestClient, world: World) -> None:
    _, item = closing_meeting(world, authed)
    make_gap(world.db, item, type=GapType.DUE_VAGUE)
    frames = ws_events(
        authed,
        world.meeting.id,
        lambda: do_send(authed, world, confirm_open_gaps=False),
        ("meeting.sent",),
        timeout=1.0,
    )
    assert "meeting.sent" not in {frame_name(f) for f in frames}


# --- DUE_TOO_LATE, BR-6 ---


@pytest.fixture
def page(client: TestClient, world: World) -> Item:
    make_access_link(world.db, world.person, world.meeting, token_hash=digest(TOKEN))
    return world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=SENT_AT,
        due_date=DUE,
        what="Мой пункт",
    )


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get(f"{API}/p/{TOKEN}").json()["csrf_token"]}


def reschedule(client: TestClient, item: Item, due: date):
    return client.post(
        f"{API}/p/{TOKEN}/items/{item.id}/reschedule",
        json={"due_date": due.isoformat()},
        headers=csrf_headers(client),
    )


@pytest.mark.fr("FR-31")
@pytest.mark.fr("BR-6")
@pytest.mark.parametrize("days", [61, 90])
def test_reschedule_beyond_60_days_is_due_too_late(
    client: TestClient, world: World, page: Item, days: int
) -> None:
    resp = reschedule(client, page, DUE + timedelta(days=days))
    assert resp.status_code == 422
    assert problem_code(resp) == "DUE_TOO_LATE"
    assert reload(world, page).due_date == DUE


@pytest.mark.fr("FR-31")
@pytest.mark.fr("BR-6")
def test_reschedule_into_the_past_is_due_in_past(client: TestClient, page: Item) -> None:
    resp = reschedule(client, page, TODAY - timedelta(days=1))
    assert resp.status_code == 422
    assert problem_code(resp) == "DUE_IN_PAST"


# --- CSRF_INVALID ---


@pytest.mark.fr("FR-32")
def test_post_without_csrf_is_csrf_invalid(client: TestClient, world: World, page: Item) -> None:
    resp = client.post(f"{API}/p/{TOKEN}/items/{page.id}/accept")
    assert resp.status_code == 403
    assert problem_code(resp) == "CSRF_INVALID"
    assert reload(world, page).status == ItemStatus.PENDING


@pytest.mark.fr("FR-32")
def test_post_with_wrong_csrf_is_csrf_invalid(client: TestClient, page: Item) -> None:
    client.get(f"{API}/p/{TOKEN}")
    resp = client.post(f"{API}/p/{TOKEN}/items/{page.id}/accept", headers={"X-CSRF-Token": "wrong"})
    assert resp.status_code == 403
    assert problem_code(resp) == "CSRF_INVALID"


@pytest.mark.fr("FR-32")
def test_post_with_header_but_without_cookie_is_csrf_invalid(
    client: TestClient, page: Item
) -> None:
    headers = csrf_headers(client)
    client.cookies.clear()
    resp = client.post(f"{API}/p/{TOKEN}/items/{page.id}/accept", headers=headers)
    assert resp.status_code == 403
    assert problem_code(resp) == "CSRF_INVALID"


# USE_REASSIGN (FR-16)


@pytest.mark.fr("FR-16")
@pytest.mark.parametrize("status", [ItemStatus.PENDING, ItemStatus.ACCEPTED, ItemStatus.DISPUTED])
def test_assignee_change_after_send_is_use_reassign(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx, status)
    resp = authed.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(ctx.anna.id)},
        headers=if_match(item),
    )
    assert resp.status_code == 422
    assert problem_code(resp) == "USE_REASSIGN"
    assert reload(world, item).assignee_person_id == ctx.ivan.id


@pytest.mark.fr("FR-16")
def test_assignee_removal_after_send_is_use_reassign(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    resp = authed.patch(
        f"{API}/items/{item.id}", json={"assignee_person_id": None}, headers=if_match(item)
    )
    assert resp.status_code == 422
    assert problem_code(resp) == "USE_REASSIGN"


@pytest.mark.fr("FR-16")
def test_use_reassign_rejects_whole_patch(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    resp = authed.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(ctx.anna.id), "what": "Новое"},
        headers=if_match(item),
    )
    assert problem_code(resp) == "USE_REASSIGN"
    stored = reload(world, item)
    assert (stored.version, stored.what) == (item.version, "Старое")


# --- /health ---


def health(client: TestClient) -> Any:
    return client.get(f"{API}/health")


@pytest.mark.fr("NFR-11")
def test_health_has_all_sections(client: TestClient) -> None:
    resp = health(client)
    assert resp.status_code == 200
    assert {"status", "db", "email_queue", "llm", "worker_last_run_at"} <= set(resp.json())


@pytest.mark.fr("NFR-11")
def test_health_all_ok_when_dependencies_work(client: TestClient) -> None:
    body = health(client).json()
    assert (body["status"], body["db"]) == ("ok", "ok")


@pytest.mark.fr("NFR-11")
def test_health_email_queue_is_empty_without_mail(client: TestClient) -> None:
    queue = health(client).json()["email_queue"]
    assert (queue["queued"], queue["failed_24h"], queue["oldest_queued_age_s"]) == (0, 0, None)


@pytest.mark.fr("NFR-11")
def test_health_counts_queued_and_failed_mail(client: TestClient, world: World) -> None:
    now = datetime.now(UTC)
    common = {"type": NotificationType.SUMMARY, "recipient_person_id": world.person.id}
    for age in (3, 1):
        make_notification(
            world.db,
            status=NotificationStatus.QUEUED,
            scheduled_for=now - timedelta(minutes=age),
            **common,
        )
    make_notification(world.db, status=NotificationStatus.FAILED, **common)
    old_failed = make_notification(world.db, status=NotificationStatus.FAILED, **common)
    old_failed.created_at = now - timedelta(days=3)
    old_failed.scheduled_for = now - timedelta(days=3)
    world.db.flush()
    queue = health(client).json()["email_queue"]
    assert queue["queued"] == 2
    assert queue["failed_24h"] == 1
    assert isinstance(queue["oldest_queued_age_s"], int)
    assert queue["oldest_queued_age_s"] >= 0


@pytest.mark.fr("NFR-11")
def test_health_llm_section(client: TestClient) -> None:
    llm = health(client).json()["llm"]
    assert isinstance(llm["available"], bool)
    assert isinstance(llm["provider"], str)


@pytest.mark.fr("NFR-11")
def test_health_worker_last_run_is_null_or_timestamp(client: TestClient) -> None:
    value = health(client).json()["worker_last_run_at"]
    assert value is None or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo


@pytest.mark.fr("NFR-11")
def test_health_is_503_when_database_is_down(
    monkeypatch: pytest.MonkeyPatch, settings_reset: None
) -> None:
    from app.core.db import get_engine

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:1/none")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        resp = TestClient(create_app()).get(f"{API}/health")
    finally:
        get_engine.cache_clear()
    assert resp.status_code == 503
    body = resp.json()
    assert body["db"] == "down"
    assert body["status"] in ("degraded", "down")
    assert {"email_queue", "llm", "worker_last_run_at"} <= set(body)


# --- порог нагрузки серии и open_items_count ---


def series_url(world: World) -> str:
    return f"{API}/series/{world.series.id}"


@pytest.mark.fr("FR-26")
def test_series_default_threshold_is_five(authed: TestClient, world: World) -> None:
    assert authed.get(series_url(world)).json()["load_warning_threshold"] == 5


@pytest.mark.fr("FR-26")
def test_threshold_is_changed_by_patch_and_returned(authed: TestClient, world: World) -> None:
    resp = authed.patch(series_url(world), json={"load_warning_threshold": 3})
    assert resp.status_code == 200
    assert resp.json()["load_warning_threshold"] == 3
    assert authed.get(series_url(world)).json()["load_warning_threshold"] == 3


@pytest.mark.fr("FR-26")
def test_threshold_is_in_series_list(authed: TestClient, world: World) -> None:
    authed.patch(series_url(world), json={"load_warning_threshold": 4})
    row = next(s for s in authed.get(f"{API}/series").json() if s["id"] == str(world.series.id))
    assert row["load_warning_threshold"] == 4


@pytest.mark.fr("FR-26")
@pytest.mark.parametrize("value", [0, -1, "abc"])
def test_threshold_below_one_is_validation_error(
    authed: TestClient, world: World, value: Any
) -> None:
    resp = authed.patch(series_url(world), json={"load_warning_threshold": value})
    assert resp.status_code == 422
    assert problem_code(resp) == "VALIDATION_ERROR"


@pytest.mark.fr("FR-26")
def test_patched_threshold_drives_overload_warning(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    for _ in range(3):
        make_item(
            world.db,
            world.meeting,
            assignee_person_id=ctx.ivan.id,
            status=ItemStatus.PENDING,
            due_date=TODAY + timedelta(days=3),
            sent_at=SENT_AT,
        )
    assert authed.patch(series_url(world), json={"load_warning_threshold": 3}).status_code == 200
    item = world.item(what="Новая задача")
    resp = authed.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(ctx.ivan.id)},
        headers=if_match(item),
    )
    assert resp.status_code == 200
    [event] = events(world.db, "overload_warning_shown")
    assert event.props["open_items"] == 3


def seed_items(world: World, series, statuses: list[ItemStatus]) -> None:
    meeting = make_meeting(world.db, series)
    for status in statuses:
        extra: dict[str, Any] = {}
        if status == ItemStatus.DONE:
            extra["done_at"] = SENT_AT
        if status == ItemStatus.CANCELLED:
            extra["cancelled_at"] = SENT_AT
        if status != ItemStatus.DRAFT:
            extra["sent_at"] = SENT_AT
        make_item(world.db, meeting, status=status, **extra)


@pytest.mark.fr("FR-26")
def test_open_items_count_counts_pending_and_accepted(authed: TestClient, world: World) -> None:
    seed_items(
        world,
        world.series,
        [
            ItemStatus.PENDING,
            ItemStatus.PENDING,
            ItemStatus.ACCEPTED,
            ItemStatus.DRAFT,
            ItemStatus.DONE,
            ItemStatus.CANCELLED,
        ],
    )
    assert authed.get(series_url(world)).json()["open_items_count"] == 3


@pytest.mark.fr("FR-26")
def test_open_items_count_is_zero_for_empty_series(authed: TestClient, world: World) -> None:
    assert authed.get(series_url(world)).json()["open_items_count"] == 0


@pytest.mark.fr("FR-26")
def test_open_items_count_is_per_series_in_list(authed: TestClient, world: World) -> None:
    other = make_series(world.db, world.user)
    seed_items(world, world.series, [ItemStatus.PENDING])
    seed_items(world, other, [ItemStatus.PENDING, ItemStatus.ACCEPTED])
    rows = {s["id"]: s for s in authed.get(f"{API}/series").json()}
    assert rows[str(world.series.id)]["open_items_count"] == 1
    assert rows[str(other.id)]["open_items_count"] == 2


@pytest.mark.fr("FR-26")
def test_open_items_count_ignores_other_hosts_series(authed: TestClient, world: World) -> None:
    stranger_series = make_series(world.db, make_user(world.db))
    seed_items(world, stranger_series, [ItemStatus.PENDING] * 4)
    assert authed.get(series_url(world)).json()["open_items_count"] == 0


# SendResult (FR-28.8)


def send_scenario(world: World, authed: TestClient):
    ctx = make_ctx(world)
    assigned = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=DUE)
    gappy = world.item(what="Позвонить подрядчику", assignee_person_id=ctx.anna.id, due_date=DUE)
    make_gap(world.db, gappy, type=GapType.DUE_VAGUE)
    unassigned = world.item(what="Найти площадку")
    assert authed.post(f"{API}/meetings/{world.meeting.id}/closure").status_code == 200
    return assigned, gappy, unassigned


@pytest.mark.fr("FR-28")
def test_send_result_items_total(authed: TestClient, world: World) -> None:
    send_scenario(world, authed)
    assert do_send(authed, world).json()["items_total"] == 3


@pytest.mark.fr("FR-28")
def test_send_result_items_with_open_gaps(authed: TestClient, world: World) -> None:
    _, _, unassigned = send_scenario(world, authed)
    make_gap(world.db, unassigned, type=GapType.NO_ASSIGNEE)
    assert do_send(authed, world).json()["items_with_open_gaps"] == 2


@pytest.mark.fr("FR-28")
@pytest.mark.fr("BR-4")
def test_send_result_items_unassigned(authed: TestClient, world: World) -> None:
    send_scenario(world, authed)
    assert do_send(authed, world).json()["items_unassigned"] == 1


@pytest.mark.fr("FR-28")
def test_send_result_recipients_equals_participants(authed: TestClient, world: World) -> None:
    send_scenario(world, authed)
    assert do_send(authed, world).json()["recipients"] == 3


@pytest.mark.fr("FR-28")
def test_send_result_meeting_status_and_time(authed: TestClient, world: World) -> None:
    send_scenario(world, authed)
    body = do_send(authed, world).json()
    assert body["meeting_id"] == str(world.meeting.id)
    assert body["status"] == MeetingStatus.SENT.value
    assert datetime.fromisoformat(body["summary_sent_at"].replace("Z", "+00:00"))


@pytest.mark.fr("FR-28")
def test_send_result_matches_summary_sent_event(authed: TestClient, world: World) -> None:
    send_scenario(world, authed)
    body = do_send(authed, world).json()
    [event] = events(world.db, "summary_sent", meeting_id=world.meeting.id)
    for key in ("items_total", "items_with_open_gaps", "recipients"):
        assert body[key] == event.props[key]


@pytest.mark.fr("FR-28")
def test_repeated_send_returns_same_counters(authed: TestClient, world: World) -> None:
    send_scenario(world, authed)
    first = do_send(authed, world).json()
    again = do_send(authed, world).json()
    keys = ("items_total", "items_with_open_gaps", "items_unassigned", "recipients")
    assert {k: again[k] for k in keys} == {k: first[k] for k in keys}


@pytest.mark.fr("FR-28")
def test_send_result_keeps_own_fields(authed: TestClient, world: World) -> None:
    send_scenario(world, authed)
    body = do_send(authed, world).json()
    assert {"meeting", "items_sent", "notifications_queued", "already_sent"} <= set(body)


# --- can_accept_in_meeting, accept_blocked_reason, Gap.blocking ---


def get_item(client: TestClient, item: Item) -> dict:
    resp = client.get(f"{API}/items/{item.id}")
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture
def ready(world: World):
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=DUE)
    return ctx, item


@pytest.mark.fr("FR-25")
def test_ready_item_can_be_accepted(authed: TestClient, ready) -> None:
    _, item = ready
    body = get_item(authed, item)
    assert body["can_accept_in_meeting"] is True
    assert body["accept_blocked_reason"] is None


@pytest.mark.fr("FR-25")
def test_absent_assignee_blocks_accept_with_reason(authed: TestClient, world: World, ready) -> None:
    ctx, item = ready
    world.db.get(MeetingParticipant, (world.meeting.id, ctx.ivan.id)).present = False
    world.db.flush()
    body = get_item(authed, item)
    assert body["can_accept_in_meeting"] is False
    assert isinstance(body["accept_blocked_reason"], str)
    assert body["accept_blocked_reason"].strip()


@pytest.mark.fr("FR-25")
def test_item_without_assignee_cannot_be_accepted(authed: TestClient, world: World) -> None:
    make_ctx(world)
    body = get_item(authed, world.item(due_date=DUE))
    assert body["can_accept_in_meeting"] is False
    assert body["accept_blocked_reason"]


@pytest.mark.fr("FR-25")
@pytest.mark.parametrize("due_text", [None, "на днях"])
def test_item_without_due_date_cannot_be_accepted(
    authed: TestClient, world: World, due_text: str | None
) -> None:
    ctx = make_ctx(world)
    item = world.item(assignee_person_id=ctx.ivan.id, due_text=due_text)
    body = get_item(authed, item)
    assert body["can_accept_in_meeting"] is False
    assert body["accept_blocked_reason"]


@pytest.mark.fr("FR-25")
@pytest.mark.parametrize(
    "gap_type", [GapType.NO_ASSIGNEE, GapType.NO_DUE, GapType.DUE_VAGUE, GapType.DUE_PAST]
)
def test_blocking_gap_blocks_accept(authed: TestClient, world: World, ready, gap_type) -> None:
    _, item = ready
    make_gap(world.db, item, type=gap_type)
    body = get_item(authed, item)
    assert body["can_accept_in_meeting"] is False
    assert body["accept_blocked_reason"]


@pytest.mark.fr("FR-25")
def test_vague_action_does_not_block_accept(authed: TestClient, world: World, ready) -> None:
    _, item = ready
    make_gap(world.db, item, type=GapType.VAGUE_ACTION)
    body = get_item(authed, item)
    assert body["can_accept_in_meeting"] is True
    assert body["accept_blocked_reason"] is None


@pytest.mark.fr("FR-25")
def test_can_accept_is_in_patch_response(authed: TestClient, ready) -> None:
    _, item = ready
    resp = authed.patch(f"{API}/items/{item.id}", json={"what": "Другое"}, headers=if_match(item))
    assert resp.status_code == 200
    assert resp.json()["can_accept_in_meeting"] is True


@pytest.mark.fr("FR-25")
def test_can_accept_follows_dismissed_gap(authed: TestClient, world: World, ready) -> None:
    _, item = ready
    gap = make_gap(world.db, item, type=GapType.DUE_VAGUE)
    assert get_item(authed, item)["can_accept_in_meeting"] is False
    resp = authed.post(f"{API}/items/{item.id}/gaps/{gap.id}/dismiss")
    assert resp.status_code == 200
    assert resp.json()["can_accept_in_meeting"] is True


@pytest.mark.fr("FR-25")
@pytest.mark.parametrize(
    ("gap_type", "blocking"),
    [
        (GapType.NO_ASSIGNEE, True),
        (GapType.NO_DUE, True),
        (GapType.DUE_VAGUE, True),
        (GapType.DUE_PAST, True),
        (GapType.VAGUE_ACTION, False),
    ],
)
def test_gap_blocking_flag_follows_br3(
    authed: TestClient, world: World, ready, gap_type: GapType, blocking: bool
) -> None:
    _, item = ready
    make_gap(world.db, item, type=gap_type)
    [gap] = get_item(authed, item)["gaps"]
    assert gap["type"] == gap_type.value
    assert gap["blocking"] is blocking


# --- выгрузка событий: from/to ---

EXPORT = f"{API}/admin/export/events.csv"


def add_event(world: World, name: str, at: datetime) -> None:
    world.db.add(AnalyticsEvent(name=name, created_at=at, props={}))
    world.db.flush()


def export_names(client: TestClient, monkeypatch: pytest.MonkeyPatch, **params: str) -> list[str]:
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    get_settings.cache_clear()
    resp = client.get(EXPORT, headers={"X-Admin-Token": ADMIN_TOKEN}, params=params)
    assert resp.status_code == 200
    return sorted(r[0] for r in list(csv.reader(io.StringIO(resp.text)))[1:])


@pytest.fixture
def spread(world: World) -> None:
    for day in (9, 10, 11, 12):
        add_event(world, f"day_{day}", datetime(2030, 1, day, 23, 59, tzinfo=UTC))
    add_event(world, "day_13", datetime(2030, 1, 13, 0, 0, tzinfo=UTC))


@pytest.mark.fr("FR-51")
def test_export_from_to_is_inclusive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, spread
) -> None:
    names = export_names(client, monkeypatch, **{"from": "2030-01-10", "to": "2030-01-12"})
    assert names == ["day_10", "day_11", "day_12"]


@pytest.mark.fr("FR-51")
def test_export_from_only(client: TestClient, monkeypatch: pytest.MonkeyPatch, spread) -> None:
    assert export_names(client, monkeypatch, **{"from": "2030-01-12"}) == ["day_12", "day_13"]


@pytest.mark.fr("FR-51")
def test_export_to_only(client: TestClient, monkeypatch: pytest.MonkeyPatch, spread) -> None:
    assert export_names(client, monkeypatch, to="2030-01-09") == ["day_9"]


@pytest.mark.fr("FR-51")
def test_export_without_dates_returns_everything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, spread
) -> None:
    assert len(export_names(client, monkeypatch)) == 5
