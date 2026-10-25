import hashlib
import re
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Notification
from app.modules.items.models import Item, ItemStatus
from app.modules.notifications.models import NotificationStatus, NotificationType
from app.modules.notifications.sender import send_due
from tests.acceptance.helpers import act, frozen
from tests.acceptance.helpers_mailing import mailbox, payload_text  # noqa: F401
from tests.factories import (
    World,
    add_member,
    make_access_link,
    make_notification,
    make_person,
)

pytestmark = pytest.mark.fr("FR-43")

THURSDAY = "2026-10-15 09:00:00+00:00"
FRIDAY = "2026-10-16 09:00:00+00:00"
LATE_THURSDAY_UTC = "2026-10-15 22:00:00+00:00"
DUE = date(2026, 11, 20)
TOKEN = "tok-" + "r" * 28
REPEAT = NotificationType.CONFIRM_REPEAT
LIVE = (NotificationStatus.QUEUED, NotificationStatus.SENDING)


def utc(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def member(world: World, name: str):
    person = make_person(world.db, world.user, world.org, full_name=name)
    add_member(world.db, world.series, person)
    return person


def make_item(world: World, assignee, status: ItemStatus = ItemStatus.PENDING, **kw) -> Item:
    return world.item(
        assignee_person_id=assignee.id,
        status=status,
        due_date=DUE,
        sent_at=datetime(2026, 10, 10, tzinfo=UTC),
        **kw,
    )


@pytest.fixture
def old(world: World):
    add_member(world.db, world.series, world.person)
    return world.person


@pytest.fixture
def new(world: World):
    return member(world, "Новиков Новый")


@pytest.fixture
def item(world: World, old) -> Item:
    return make_item(world, old, what="Подготовить смету")


def reassign(client: TestClient, world: World, item: Item, person, when: str = THURSDAY):
    with frozen(client, world.user, when):
        resp = act(client, item, "reassign", assignee_person_id=str(person.id))
    assert resp.status_code == 200, resp.text
    world.db.refresh(item)


def repeats(world: World, person, *, statuses=None) -> list[Notification]:
    query = select(Notification).where(
        Notification.type == REPEAT,
        Notification.recipient_person_id == person.id,
        Notification.meeting_id == world.meeting.id,
    )
    rows = list(world.db.scalars(query.order_by(Notification.created_at, Notification.dedupe_key)))
    return [r for r in rows if statuses is None or r.status in statuses]


def repeat_of(world: World, person) -> Notification:
    (one,) = repeats(world, person)
    return one


def link_for_new(world: World, person) -> str:
    make_access_link(
        world.db, person, world.meeting, token_hash=hashlib.sha256(TOKEN.encode()).digest()
    )
    return TOKEN


def link_act(client: TestClient, token: str, item: Item, action: str, body: dict | None = None):
    csrf = client.get(f"/api/v1/p/{token}").json()["csrf_token"]
    return client.post(
        f"/api/v1/p/{token}/items/{item.id}/{action}",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )


def test_reassign_plans_confirm_repeat_for_new_assignee(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new)
    mail = repeat_of(world, new)
    assert mail.status == NotificationStatus.QUEUED
    assert mail.meeting_id == world.meeting.id
    assert mail.recipient_person_id == new.id


def test_reassign_repeat_is_one_business_day_later(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new, THURSDAY)
    assert repeat_of(world, new).scheduled_for == utc(16)


def test_reassign_on_friday_repeat_goes_out_on_monday(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new, FRIDAY)
    assert repeat_of(world, new).scheduled_for == utc(19)


def test_reassign_repeat_counts_days_in_series_timezone(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new, LATE_THURSDAY_UTC)
    assert repeat_of(world, new).scheduled_for == utc(18, 22)


def test_reassign_repeat_has_own_dedupe_key(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new)
    key = repeat_of(world, new).dedupe_key
    meeting_key = f"{world.meeting.id}:CONFIRM_REPEAT:{new.id}"
    assert key != meeting_key
    assert key.startswith(f"{meeting_key}:")


def test_reassign_repeat_does_not_collide_with_meeting_level_key_after_it_was_sent(
    authed: TestClient, world: World, item: Item, new
) -> None:
    make_notification(
        world.db,
        type=REPEAT,
        recipient_person_id=new.id,
        meeting_id=world.meeting.id,
        dedupe_key=f"{world.meeting.id}:CONFIRM_REPEAT:{new.id}",
        status=NotificationStatus.SENT,
        scheduled_for=utc(14),
    )
    reassign(authed, world, item, new)
    assert len(repeats(world, new, statuses=LIVE)) == 1


def test_reassign_back_and_forth_gives_distinct_keys(
    authed: TestClient, world: World, item: Item, new
) -> None:
    other = member(world, "Орлов Другой")
    reassign(authed, world, item, new)
    reassign(authed, world, item, other)
    reassign(authed, world, item, new)
    keys = [m.dedupe_key for m in repeats(world, new)]
    assert len(keys) == len(set(keys)) == 2


def test_no_second_repeat_when_meeting_repeat_is_already_queued(
    authed: TestClient, world: World, item: Item, new
) -> None:
    queued = make_notification(
        world.db,
        type=REPEAT,
        recipient_person_id=new.id,
        meeting_id=world.meeting.id,
        dedupe_key=f"{world.meeting.id}:CONFIRM_REPEAT:{new.id}",
        scheduled_for=utc(20),
    )
    reassign(authed, world, item, new)
    assert [m.id for m in repeats(world, new)] == [queued.id]
    assert queued.scheduled_for == utc(20)


def test_item_reassigned_into_queued_meeting_repeat_is_in_the_letter(
    authed: TestClient, world: World, item: Item, new
) -> None:
    make_notification(
        world.db,
        type=REPEAT,
        recipient_person_id=new.id,
        meeting_id=world.meeting.id,
        dedupe_key=f"{world.meeting.id}:CONFIRM_REPEAT:{new.id}",
        scheduled_for=utc(20),
    )
    reassign(authed, world, item, new)
    send_due(world.db, utc(20))
    mail = repeat_of(world, new)
    assert mail.status == NotificationStatus.SENT
    assert "Подготовить смету" in payload_text(mail)


def test_second_reassign_to_same_person_while_repeat_queued_adds_no_second_repeat(
    authed: TestClient, world: World, item: Item, new
) -> None:
    second = make_item(world, world.person, what="Второй пункт")
    reassign(authed, world, item, new)
    reassign(authed, world, second, new)
    assert len(repeats(world, new, statuses=LIVE)) == 1


def test_repeat_is_sent_when_item_is_still_pending(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new)
    send_due(world.db, utc(16))
    mail = repeat_of(world, new)
    assert mail.status == NotificationStatus.SENT
    assert "Подготовить смету" in payload_text(mail)


def test_repeat_is_not_sent_before_its_time(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new)
    send_due(world.db, utc(15, 23))
    assert repeat_of(world, new).status == NotificationStatus.QUEUED


def test_repeat_lists_only_pending_items_of_recipient_for_this_meeting(
    authed: TestClient, world: World, item: Item, new
) -> None:
    make_item(world, new, ItemStatus.ACCEPTED, what="Уже принятый пункт")
    reassign(authed, world, item, new)
    send_due(world.db, utc(16))
    text = payload_text(repeat_of(world, new))
    assert "Подготовить смету" in text
    assert "Уже принятый пункт" not in text


def test_repeat_is_cancelled_at_send_when_nothing_is_pending(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new)
    item.status = ItemStatus.ACCEPTED
    world.db.flush()
    send_due(world.db, utc(16))
    assert repeat_of(world, new).status == NotificationStatus.CANCELLED


def test_accept_by_new_assignee_cancels_repeat(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new)
    token = link_for_new(world, new)
    assert link_act(authed, token, item, "accept").status_code == 200
    assert repeat_of(world, new).status == NotificationStatus.CANCELLED


def test_decline_by_new_assignee_cancels_repeat(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new)
    token = link_for_new(world, new)
    assert link_act(authed, token, item, "decline", {}).status_code == 200
    send_due(world.db, utc(16))
    assert repeat_of(world, new).status == NotificationStatus.CANCELLED


def test_reschedule_by_new_assignee_cancels_repeat(
    authed: TestClient, world: World, item: Item, new
) -> None:
    reassign(authed, world, item, new)
    token = link_for_new(world, new)
    body = {"due_date": "2026-11-27"}
    assert link_act(authed, token, item, "reschedule", body).status_code == 200
    send_due(world.db, utc(16))
    assert repeat_of(world, new).status == NotificationStatus.CANCELLED


def test_host_cancel_cancels_repeat(authed: TestClient, world: World, item: Item, new) -> None:
    reassign(authed, world, item, new)
    with frozen(authed, world.user, THURSDAY):
        assert act(authed, item, "cancel").status_code == 200
    send_due(world.db, utc(16))
    assert repeat_of(world, new).status == NotificationStatus.CANCELLED


def test_second_reassign_cancels_first_repeat_and_plans_next(
    authed: TestClient, world: World, item: Item, new
) -> None:
    other = member(world, "Орлов Другой")
    reassign(authed, world, item, new)
    reassign(authed, world, item, other, FRIDAY)
    send_due(world.db, utc(16, 10))
    assert repeat_of(world, new).status == NotificationStatus.CANCELLED
    assert repeat_of(world, other).scheduled_for == utc(19)


def test_repeat_survives_cancel_of_other_item_when_another_is_pending(
    authed: TestClient, world: World, item: Item, new
) -> None:
    keeper = make_item(world, new, what="Остаётся в ожидании")
    reassign(authed, world, item, new)
    with frozen(authed, world.user, THURSDAY):
        assert act(authed, item, "cancel").status_code == 200
    send_due(world.db, utc(16))
    mail = repeat_of(world, new)
    assert mail.status == NotificationStatus.SENT
    assert keeper.what in payload_text(mail)


def meeting_repeat(world: World, person) -> Notification:
    return make_notification(
        world.db,
        type=REPEAT,
        recipient_person_id=person.id,
        meeting_id=world.meeting.id,
        dedupe_key=f"{world.meeting.id}:CONFIRM_REPEAT:{person.id}",
        scheduled_for=utc(16),
    )


def test_previous_assignee_repeat_is_cancelled_when_no_pending_left(
    authed: TestClient, world: World, item: Item, old, new
) -> None:
    queued = meeting_repeat(world, old)
    reassign(authed, world, item, new)
    send_due(world.db, utc(16))
    world.db.refresh(queued)
    assert queued.status == NotificationStatus.CANCELLED


def test_previous_assignee_repeat_goes_out_while_another_item_is_pending(
    authed: TestClient, world: World, item: Item, old, new
) -> None:
    make_item(world, old, what="Остаток у прежнего")
    queued = meeting_repeat(world, old)
    reassign(authed, world, item, new)
    send_due(world.db, utc(16))
    world.db.refresh(queued)
    assert queued.status == NotificationStatus.SENT
    text = payload_text(queued)
    assert "Остаток у прежнего" in text
    assert "Подготовить смету" not in text


def test_reassign_adds_no_repeat_for_previous_assignee(
    authed: TestClient, world: World, item: Item, old, new
) -> None:
    reassign(authed, world, item, new)
    assert repeats(world, old) == []


def test_reassign_sends_new_assignee_only_confirm_request_now(
    authed: TestClient, world: World, item: Item, old, new
) -> None:
    reassign(authed, world, item, new)
    kinds = sorted(
        m.type
        for m in world.db.scalars(select(Notification).where(Notification.item_id == item.id))
        if m.type != NotificationType.DUE_TOMORROW
    )
    assert NotificationType.CONFIRM_REQUEST in kinds
    assert NotificationType.ITEM_REASSIGNED in kinds
    assert not re.search("HOST_RESPONSE|SUMMARY", " ".join(kinds))
