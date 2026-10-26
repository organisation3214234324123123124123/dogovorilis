import hashlib
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AccessLink, ItemEvent
from app.modules.items.models import ActorType, DoneBy, Item, ItemAction, ItemStatus
from app.modules.notifications.models import NotificationStatus, NotificationType
from tests.acceptance.helpers import act, frozen
from tests.factories import (
    World,
    add_member,
    events_named,
    make_access_link,
    make_notification,
    make_person,
    notifications_for,
)

pytestmark = pytest.mark.fr("FR-43")

NOW = "2026-10-15 09:00:00+00:00"
DUE = date(2026, 11, 20)
NEW_DUE = "2026-11-27"
TOKEN = "tok-" + "c" * 28
OPEN = [ItemStatus.PENDING, ItemStatus.ACCEPTED]
OPEN_OR_DISPUTED = [ItemStatus.PENDING, ItemStatus.ACCEPTED, ItemStatus.DISPUTED]


def make(world: World, status: ItemStatus = ItemStatus.ACCEPTED, **kw) -> Item:
    add_member(world.db, world.series, world.person)
    return world.item(
        assignee_person_id=world.person.id,
        status=status,
        due_date=DUE,
        sent_at=datetime(2026, 10, 10, tzinfo=UTC),
        **kw,
    )


def reminder(world: World, item: Item):
    return make_notification(
        world.db,
        type=NotificationType.DUE_TOMORROW,
        recipient_person_id=world.person.id,
        item_id=item.id,
        scheduled_for=datetime(2026, 11, 19, 6, tzinfo=UTC),
    )


def second_person(world: World):
    person = make_person(world.db, world.user, world.org, full_name="Новиков Новый")
    add_member(world.db, world.series, person)
    return person


def journal(world: World, item: Item, action: ItemAction) -> list[ItemEvent]:
    return list(
        world.db.scalars(
            select(ItemEvent).where(ItemEvent.item_id == item.id, ItemEvent.action == action)
        )
    )


@pytest.mark.parametrize("status", OPEN)
def test_done_marks_item_done_by_host(authed: TestClient, world: World, status: ItemStatus) -> None:
    item = make(world, status)
    resp = act(authed, item, "done")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["done_at"] is not None
    assert body["done_by"] == DoneBy.HOST


def test_done_cancels_reminder(authed: TestClient, world: World) -> None:
    item = make(world)
    note = reminder(world, item)
    assert act(authed, item, "done").status_code == 200
    world.db.refresh(note)
    assert note.status == NotificationStatus.CANCELLED


def test_done_writes_item_done_event(authed: TestClient, world: World) -> None:
    item = make(world)
    with frozen(authed, world.user, NOW):
        assert act(authed, item, "done").status_code == 200
    events = events_named(world.db, "item_done", item_id=item.id)
    assert len(events) == 1
    assert events[0].props["on_time"] is True
    assert events[0].props["by"] == "host"


def test_done_from_disputed_is_invalid_transition(authed: TestClient, world: World) -> None:
    item = make(world, ItemStatus.DISPUTED)
    resp = act(authed, item, "done")
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_TRANSITION"


@pytest.mark.parametrize("status", OPEN)
def test_reschedule_sets_due_and_counts(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    item = make(world, status)
    with frozen(authed, world.user, NOW):
        resp = act(authed, item, "reschedule", due_date=NEW_DUE)
    assert resp.status_code == 200
    assert resp.json()["due_date"] == NEW_DUE
    assert resp.json()["reschedule_count"] == 1
    assert resp.json()["status"] == status


def test_reschedule_replans_reminder(authed: TestClient, world: World) -> None:
    item = make(world)
    old = reminder(world, item)
    with frozen(authed, world.user, NOW):
        assert act(authed, item, "reschedule", due_date=NEW_DUE).status_code == 200
    world.db.refresh(old)
    assert old.status == NotificationStatus.CANCELLED
    queued = notifications_for(world.db, item, NotificationType.DUE_TOMORROW)
    assert [n for n in queued if n.status == NotificationStatus.QUEUED]


def test_reschedule_notifies_assignee(authed: TestClient, world: World) -> None:
    item = make(world)
    with frozen(authed, world.user, NOW):
        assert act(authed, item, "reschedule", due_date=NEW_DUE).status_code == 200
    changed = notifications_for(world.db, item, NotificationType.ITEM_CHANGED)
    assert [n.recipient_person_id for n in changed] == [world.person.id]


def test_reschedule_into_past_is_rejected(authed: TestClient, world: World) -> None:
    item = make(world)
    with frozen(authed, world.user, NOW):
        resp = act(authed, item, "reschedule", due_date="2026-10-14")
    assert resp.status_code == 422
    assert resp.json()["code"] == "DUE_IN_PAST"


@pytest.mark.parametrize("status", OPEN_OR_DISPUTED)
def test_cancel_marks_cancelled(authed: TestClient, world: World, status: ItemStatus) -> None:
    item = make(world, status)
    resp = act(authed, item, "cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["cancelled_at"] is not None


def test_cancel_cancels_reminder_and_sends_nothing(authed: TestClient, world: World) -> None:
    item = make(world)
    note = reminder(world, item)
    assert act(authed, item, "cancel").status_code == 200
    world.db.refresh(note)
    assert note.status == NotificationStatus.CANCELLED
    assert [n for n in notifications_for(world.db, item) if n.id != note.id] == []


@pytest.mark.parametrize("status", OPEN_OR_DISPUTED)
def test_reassign_makes_item_pending_for_new_assignee(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    item = make(world, status)
    new = second_person(world)
    resp = act(authed, item, "reassign", assignee_person_id=str(new.id))
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert resp.json()["assignee"]["id"] == str(new.id)


def test_reassign_queues_confirm_request_for_new_assignee(authed: TestClient, world: World) -> None:
    item = make(world)
    new = second_person(world)
    assert act(authed, item, "reassign", assignee_person_id=str(new.id)).status_code == 200
    sent = notifications_for(world.db, item, NotificationType.CONFIRM_REQUEST)
    assert [n.recipient_person_id for n in sent] == [new.id]


def _confirm_request(world: World, item: Item, person):
    sent = notifications_for(world.db, item, NotificationType.CONFIRM_REQUEST)
    return next(n for n in sent if n.recipient_person_id == person.id)


def test_reassign_confirm_request_payload_has_no_token(authed: TestClient, world: World) -> None:
    item = make(world)
    new = second_person(world)
    assert act(authed, item, "reassign", assignee_person_id=str(new.id)).status_code == 200
    payload = _confirm_request(world, item, new).payload or {}
    text = str(payload).lower()
    assert "issue_link" not in payload
    assert "token" not in text
    assert "/p/" not in text
    link_hash_hex = {
        row.token_hash.hex()
        for row in world.db.scalars(select(AccessLink).where(AccessLink.person_id == new.id))
    }
    assert not any(h in text for h in link_hash_hex)


def test_reassign_confirm_request_addresses_item_meeting_person(
    authed: TestClient, world: World
) -> None:
    item = make(world)
    new = second_person(world)
    assert act(authed, item, "reassign", assignee_person_id=str(new.id)).status_code == 200
    note = _confirm_request(world, item, new)
    assert (note.item_id, note.meeting_id, note.recipient_person_id) == (
        item.id,
        item.meeting_id,
        new.id,
    )


def test_reassign_sets_sent_at_to_now(authed: TestClient, world: World) -> None:
    item = make(world)
    new = second_person(world)
    with frozen(authed, world.user, NOW):
        resp = act(authed, item, "reassign", assignee_person_id=str(new.id))
    assert resp.status_code == 200
    world.db.refresh(item)
    assert item.sent_at == datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def test_reassign_tells_previous_assignee_without_link_or_new_name(
    authed: TestClient, world: World
) -> None:
    item = make(world)
    new = second_person(world)
    assert act(authed, item, "reassign", assignee_person_id=str(new.id)).status_code == 200
    told = notifications_for(world.db, item, NotificationType.ITEM_REASSIGNED)
    assert [n.recipient_person_id for n in told] == [world.person.id]
    payload = str(told[0].payload or {})
    assert new.full_name not in payload
    assert "/p/" not in payload
    assert "token" not in payload.lower()


def test_reassign_revokes_previous_link_view(authed: TestClient, world: World) -> None:
    item = make(world)
    new = second_person(world)
    make_access_link(
        world.db,
        world.person,
        world.meeting,
        token_hash=hashlib.sha256(TOKEN.encode()).digest(),
    )
    assert act(authed, item, "reassign", assignee_person_id=str(new.id)).status_code == 200
    page = authed.get(f"/api/v1/p/{TOKEN}")
    assert str(item.id) not in page.text


def test_reassign_to_non_member_is_rejected(authed: TestClient, world: World) -> None:
    item = make(world)
    outsider = make_person(world.db, world.user, world.org)
    resp = act(authed, item, "reassign", assignee_person_id=str(outsider.id))
    assert resp.status_code == 422
    assert resp.json()["code"] == "NOT_A_PARTICIPANT"


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("done", {}),
        ("reschedule", {"due_date": NEW_DUE}),
        ("cancel", {}),
    ],
    ids=["done", "reschedule", "cancel"],
)
@pytest.mark.parametrize("status", [ItemStatus.DONE, ItemStatus.CANCELLED, ItemStatus.DRAFT])
def test_action_from_closed_status_is_invalid_transition(
    authed: TestClient, world: World, action: str, extra: dict, status: ItemStatus
) -> None:
    kw = {}
    if status == ItemStatus.DONE:
        kw = {"done_at": datetime(2026, 10, 1, tzinfo=UTC), "done_by": DoneBy.HOST}
    if status == ItemStatus.CANCELLED:
        kw = {"cancelled_at": datetime(2026, 10, 1, tzinfo=UTC)}
    item = make(world, status, **kw)
    with frozen(authed, world.user, NOW):
        resp = act(authed, item, action, **extra)
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_TRANSITION"


def test_reassign_from_done_is_invalid_transition(authed: TestClient, world: World) -> None:
    item = make(
        world, ItemStatus.DONE, done_at=datetime(2026, 10, 1, tzinfo=UTC), done_by=DoneBy.HOST
    )
    new = second_person(world)
    resp = act(authed, item, "reassign", assignee_person_id=str(new.id))
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_TRANSITION"


def test_stale_if_match_is_version_conflict(authed: TestClient, world: World) -> None:
    item = make(world)
    resp = act(authed, item, "done", version=item.version + 5)
    assert resp.status_code == 409
    assert resp.json()["code"] == "VERSION_CONFLICT"


def test_missing_if_match_is_precondition_required(authed: TestClient, world: World) -> None:
    item = make(world)
    resp = authed.post(f"/api/v1/items/{item.id}/actions", json={"action": "done"})
    assert resp.status_code == 428
    assert resp.json()["code"] == "PRECONDITION_REQUIRED"


@pytest.mark.parametrize(
    ("action", "extra", "logged"),
    [
        ("done", {}, ItemAction.DONE),
        ("reschedule", {"due_date": NEW_DUE}, ItemAction.RESCHEDULED),
        ("cancel", {}, ItemAction.CANCELLED),
        ("reassign", {"assignee_person_id": "NEW"}, ItemAction.REASSIGNED),
    ],
    ids=["done", "reschedule", "cancel", "reassign"],
)
def test_each_action_writes_review_action_and_journal(
    authed: TestClient, world: World, action: str, extra: dict, logged: ItemAction
) -> None:
    item = make(world)
    if extra.get("assignee_person_id") == "NEW":
        extra = {"assignee_person_id": str(second_person(world).id)}
    with frozen(authed, world.user, NOW):
        assert act(authed, item, action, **extra).status_code == 200
    events = events_named(world.db, "review_action", item_id=item.id)
    assert [e.props["action"] for e in events] == [action]
    rows = journal(world, item, logged)
    assert len(rows) == 1
    assert rows[0].actor_type == ActorType.HOST


def test_skip_keeps_items_open_and_writes_event(authed: TestClient, world: World) -> None:
    item = make(world, ItemStatus.PENDING)
    resp = authed.post(f"/api/v1/meetings/{world.meeting.id}/review/skip")
    assert resp.status_code == 204
    world.db.refresh(item)
    assert item.status == ItemStatus.PENDING
    assert len(events_named(world.db, "review_skipped", meeting_id=world.meeting.id)) == 1


def test_skipped_items_show_in_next_review(authed: TestClient, world: World) -> None:
    item = make(world)
    item.due_date = date(2026, 10, 1)
    world.db.flush()
    assert authed.post(f"/api/v1/meetings/{world.meeting.id}/review/skip").status_code == 204
    data = authed.get(f"/api/v1/meetings/{world.meeting.id}/review").json()
    assert [i["id"] for i in data["overdue"]] == [str(item.id)]


def test_no_summary_email_after_review(authed: TestClient, world: World) -> None:
    item = make(world)
    assert act(authed, item, "cancel").status_code == 200
    assert authed.post(f"/api/v1/meetings/{world.meeting.id}/review/skip").status_code == 204
    summaries = notifications_for(world.db, item, NotificationType.SUMMARY)
    assert summaries == []
