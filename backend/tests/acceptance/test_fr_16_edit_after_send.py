import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.models import AnalyticsEvent, Item, ItemEvent
from app.modules.items.models import ItemAction, ItemStatus
from app.modules.notifications.models import Notification, NotificationStatus, NotificationType
from tests.acceptance.helpers_items import (
    API,
    NOW,
    clock,  # noqa: F401
    if_match,
    make_ctx,
    notifications,
    problem_code,
)
from tests.factories import World, make_access_link

pytestmark = [
    pytest.mark.fr("FR-16"),
    pytest.mark.usefixtures("clock"),
]

SENT = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def sent_item(world: World, ctx, status=ItemStatus.PENDING, **kw) -> Item:
    kw.setdefault("assignee_person_id", ctx.ivan.id)
    kw.setdefault("due_date", datetime(2026, 10, 20).date())
    return world.item(status=status, sent_at=SENT, what="Старое", **kw)


def patch(client: TestClient, item: Item, body: dict, version: int | None = None):
    headers = if_match(item) if version is None else {"If-Match": str(version)}
    return client.patch(f"{API}/items/{item.id}", json=body, headers=headers)


def reload(world: World, item: Item) -> Item:
    world.db.expire_all()
    return world.db.get(Item, item.id)


@pytest.mark.parametrize("status", [ItemStatus.PENDING, ItemStatus.ACCEPTED, ItemStatus.DISPUTED])
def test_what_edit_keeps_status_and_bumps_version(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    item = sent_item(world, make_ctx(world), status)
    resp = patch(authed, item, {"what": "Новое"})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["status"], body["version"], body["what"]) == (status.value, 2, "Новое")


def test_what_edit_creates_item_changed_for_assignee(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    patch(authed, item, {"what": "Новое"})
    [mail] = notifications(world.db, NotificationType.ITEM_CHANGED, item.id)
    assert mail.recipient_person_id == ctx.ivan.id
    assert mail.dedupe_key == f"{item.id}:ITEM_CHANGED:2"
    payload = json.dumps(mail.payload, ensure_ascii=False)
    assert "Старое" in payload
    assert "Новое" in payload


def test_due_edit_increments_reschedule_count(authed: TestClient, world: World) -> None:
    item = sent_item(world, make_ctx(world))
    resp = patch(authed, item, {"due_date": "2026-10-22"})
    assert resp.status_code == 200
    assert resp.json()["reschedule_count"] == 1


def test_what_edit_does_not_touch_reschedule_count(authed: TestClient, world: World) -> None:
    item = sent_item(world, make_ctx(world))
    assert patch(authed, item, {"what": "Новое"}).status_code == 200
    assert reload(world, item).reschedule_count == 0


def test_due_edit_replans_due_tomorrow(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    old = Notification(
        type=NotificationType.DUE_TOMORROW,
        recipient_person_id=ctx.ivan.id,
        meeting_id=world.meeting.id,
        item_id=item.id,
        dedupe_key=f"{item.id}:DUE_TOMORROW:2026-10-20:1",
        scheduled_for=datetime(2026, 10, 19, 6, 0, tzinfo=UTC),
    )
    world.db.add(old)
    world.db.flush()
    patch(authed, item, {"due_date": "2026-10-22"})
    world.db.expire_all()
    assert world.db.get(Notification, old.id).status == NotificationStatus.CANCELLED
    fresh = [
        n
        for n in notifications(world.db, NotificationType.DUE_TOMORROW, item.id)
        if n.status == NotificationStatus.QUEUED
    ]
    assert [n.scheduled_for for n in fresh] == [datetime(2026, 10, 21, 6, 0, tzinfo=UTC)]


def test_due_edit_recalculates_link_expiry(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    link = make_access_link(world.db, ctx.ivan, world.meeting, expires_at=NOW.replace(day=29))
    item = sent_item(world, ctx)
    patch(authed, item, {"due_date": "2026-12-31"})
    world.db.expire_all()
    assert world.db.get(type(link), link.id).expires_at >= datetime(2027, 1, 30, tzinfo=UTC)


def test_new_due_in_past_is_422(authed: TestClient, world: World) -> None:
    item = sent_item(world, make_ctx(world))
    resp = patch(authed, item, {"due_date": "2026-10-14"})
    assert resp.status_code == 422
    assert problem_code(resp) == "DUE_IN_PAST"


def test_assignee_change_after_send_is_422_use_reassign(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    resp = patch(authed, item, {"assignee_person_id": str(ctx.anna.id)})
    assert resp.status_code == 422
    assert problem_code(resp) == "USE_REASSIGN"
    assert reload(world, item).assignee_person_id == ctx.ivan.id


def test_two_edits_before_sending_give_one_letter(authed: TestClient, world: World) -> None:
    item = sent_item(world, make_ctx(world))
    first = patch(authed, item, {"what": "Новое-1"})
    patch(authed, item, {"what": "Новое-2"}, version=first.json()["version"])
    queued = [
        n
        for n in notifications(world.db, NotificationType.ITEM_CHANGED, item.id)
        if n.status == NotificationStatus.QUEUED
    ]
    assert len(queued) == 1
    payload = json.dumps(queued[0].payload, ensure_ascii=False)
    assert "Старое" in payload
    assert "Новое-2" in payload


@pytest.mark.parametrize("status", [ItemStatus.DONE, ItemStatus.CANCELLED])
def test_edit_of_closed_item_is_409_invalid_transition(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    ctx = make_ctx(world)
    extra = {"done_at": SENT} if status == ItemStatus.DONE else {"cancelled_at": SENT}
    item = sent_item(world, ctx, status, **extra)
    resp = patch(authed, item, {"what": "Новое"})
    assert resp.status_code == 409
    assert problem_code(resp) == "INVALID_TRANSITION"


def test_item_without_assignee_is_edited_without_letter(authed: TestClient, world: World) -> None:
    item = sent_item(world, make_ctx(world), assignee_person_id=None)
    resp = patch(authed, item, {"what": "Новое"})
    assert resp.status_code == 200
    assert notifications(world.db, NotificationType.ITEM_CHANGED, item.id) == []


def test_edit_writes_item_event_without_text(authed: TestClient, world: World) -> None:
    item = sent_item(world, make_ctx(world))
    patch(authed, item, {"what": "Секретный текст правки"})
    world.db.expire_all()
    rows = world.db.query(ItemEvent).filter_by(item_id=item.id, action=ItemAction.UPDATED).all()
    assert len(rows) == 1
    assert "Секретный текст правки" not in json.dumps(rows[0].data, ensure_ascii=False)


def test_edit_writes_analytics_event(authed: TestClient, world: World) -> None:
    item = sent_item(world, make_ctx(world))
    before = world.db.query(AnalyticsEvent).filter_by(item_id=item.id).count()
    patch(authed, item, {"what": "Новое"})
    assert world.db.query(AnalyticsEvent).filter_by(item_id=item.id).count() > before
