# ruff: noqa: F811
"""Действия исполнителя по ссылке и их письма (FR-31, FR-34, FR-35, FR-40).

Рассылка - вторник 2030-01-15 15:00 МСК; "сегодня" по поясу серии - 2030-01-15.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import ItemComment
from app.modules.items.models import (
    ActorType,
    CommentKind,
    DoneBy,
    ItemAction,
    ItemEvent,
    ItemStatus,
)
from app.modules.notifications.models import NotificationStatus
from tests.acceptance.helpers_mailing import (  # noqa: F401
    TUESDAY,
    Scenario,
    events,
    frozen,
    host,
    link_for,
    mailbox,
    mails,
    payload_text,
    sc,
    send,
    token_of,
)

NEW_DUE = date(2030, 2, 8)


class Page:
    def __init__(self, client, sc: Scenario, person, item) -> None:
        self.client, self.sc, self.item = client, sc, item
        self.token = token_of(sc.world.db, person, sc.world.meeting)

    def act(self, action: str, body: dict | None = None, item=None):
        csrf = self.client.get(f"/api/v1/p/{self.token}").json()["csrf_token"]
        target = item or self.item
        return self.client.post(
            f"/api/v1/p/{self.token}/items/{target.id}/{action}",
            json=body,
            headers={"X-CSRF-Token": csrf},
        )

    def reload(self, item=None):
        self.sc.world.db.refresh(item or self.item)
        return item or self.item


@pytest.fixture
def ivan(host, sc: Scenario) -> Page:
    send(host, sc.world)
    sc.world.db.refresh(sc.pending)
    return Page(host, sc, sc.ivan, sc.pending)


@pytest.fixture
def anna(host, sc: Scenario) -> Page:
    send(host, sc.world)
    sc.world.db.refresh(sc.agreed)
    return Page(host, sc, sc.anna, sc.agreed)


@pytest.mark.fr("FR-31")
def test_accept_moves_pending_to_accepted(ivan: Page) -> None:
    resp = ivan.act("accept")
    assert resp.status_code == 200
    item = ivan.reload()
    assert item.status == ItemStatus.ACCEPTED
    assert item.accepted_at is not None


@pytest.mark.fr("FR-31")
@pytest.mark.fr("FR-33")
def test_accept_cancels_confirm_repeat(ivan: Page) -> None:
    ivan.act("accept")
    (repeat,) = mails(ivan.sc.world.db, "CONFIRM_REPEAT", meeting=ivan.sc.world.meeting)
    assert repeat.status == NotificationStatus.CANCELLED


@pytest.mark.fr("FR-31")
def test_accept_of_accepted_item_is_409(anna: Page) -> None:
    resp = anna.act("accept")
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_TRANSITION"


@pytest.mark.fr("FR-31")
def test_reschedule_accepts_item_with_new_due_date(ivan: Page) -> None:
    resp = ivan.act("reschedule", {"due_date": NEW_DUE.isoformat()})
    assert resp.status_code == 200
    item = ivan.reload()
    assert item.status == ItemStatus.ACCEPTED
    assert item.due_date == NEW_DUE
    assert item.reschedule_count == 1
    assert item.accepted_at is not None


@pytest.mark.fr("FR-31")
def test_reschedule_of_accepted_item_keeps_original_accepted_at(anna: Page) -> None:
    before = anna.reload().accepted_at
    anna.act("reschedule", {"due_date": NEW_DUE.isoformat()})
    assert anna.reload().accepted_at == before


@pytest.mark.fr("FR-31")
def test_reschedule_into_the_past_is_422_due_in_past(ivan: Page) -> None:
    resp = ivan.act("reschedule", {"due_date": "2030-01-14"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "DUE_IN_PAST"


@pytest.mark.fr("FR-31")
def test_reschedule_beyond_60_days_is_422(ivan: Page) -> None:
    too_far = ivan.sc.pending.due_date + timedelta(days=61)
    resp = ivan.act("reschedule", {"due_date": too_far.isoformat()})
    assert resp.status_code == 422
    assert ivan.reload().due_date != too_far


@pytest.mark.fr("FR-31")
def test_reschedule_at_exactly_60_days_is_allowed(ivan: Page) -> None:
    limit = ivan.sc.pending.due_date + timedelta(days=60)
    assert ivan.act("reschedule", {"due_date": limit.isoformat()}).status_code == 200


@pytest.mark.fr("FR-31")
@pytest.mark.fr("FR-40")
def test_reschedule_replans_the_reminder(ivan: Page) -> None:
    ivan.act("reschedule", {"due_date": NEW_DUE.isoformat()})
    reminders = mails(ivan.sc.world.db, "DUE_TOMORROW", item_id=ivan.item.id)
    by_state = {m.status: m for m in reminders}
    assert set(by_state) == {NotificationStatus.CANCELLED, NotificationStatus.QUEUED}
    assert by_state[NotificationStatus.QUEUED].dedupe_key.split(":")[2] == "2030-02-08"


@pytest.mark.fr("FR-31")
def test_reschedule_extends_link_lifetime(ivan: Page) -> None:
    far = ivan.sc.pending.due_date + timedelta(days=60)
    ivan.act("reschedule", {"due_date": far.isoformat()})
    (link,) = link_for(ivan.sc.world.db, ivan.sc.ivan, ivan.sc.world.meeting)
    assert link.expires_at.date() >= far + timedelta(days=29)


@pytest.mark.fr("FR-31")
def test_decline_marks_item_disputed_and_stores_comment(ivan: Page) -> None:
    resp = ivan.act("decline", {"comment": "Этим занимается Анна"})
    assert resp.status_code == 200
    assert ivan.reload().status == ItemStatus.DISPUTED
    (comment,) = ivan.sc.world.db.scalars(
        select(ItemComment).where(ItemComment.item_id == ivan.item.id)
    )
    assert comment.kind == CommentKind.NOT_MINE
    assert comment.text == "Этим занимается Анна"
    assert comment.author_person_id == ivan.sc.ivan.id


@pytest.mark.fr("FR-31")
def test_decline_without_comment_is_allowed(ivan: Page) -> None:
    assert ivan.act("decline", {}).status_code == 200
    assert ivan.reload().status == ItemStatus.DISPUTED


@pytest.mark.fr("FR-31")
def test_decline_comment_of_501_chars_is_422(ivan: Page) -> None:
    assert ivan.act("decline", {"comment": "я" * 501}).status_code == 422
    assert ivan.reload().status == ItemStatus.PENDING


@pytest.mark.fr("FR-31")
def test_decline_keeps_comment_text_out_of_the_event_journal(ivan: Page) -> None:
    ivan.act("decline", {"comment": "секретный комментарий"})
    journal = ivan.sc.world.db.scalars(select(ItemEvent).where(ItemEvent.item_id == ivan.item.id))
    assert all("секретный" not in str(e.data) for e in journal)


@pytest.mark.fr("FR-31")
def test_done_from_accepted_closes_item(anna: Page) -> None:
    resp = anna.act("done")
    assert resp.status_code == 200
    item = anna.reload()
    assert item.status == ItemStatus.DONE
    assert item.done_at is not None
    assert item.done_by == DoneBy.ASSIGNEE


@pytest.mark.fr("FR-31")
@pytest.mark.fr("FR-40")
def test_done_cancels_the_reminder(anna: Page) -> None:
    anna.act("done")
    (reminder,) = mails(anna.sc.world.db, "DUE_TOMORROW", item_id=anna.item.id)
    assert reminder.status == NotificationStatus.CANCELLED


@pytest.mark.fr("FR-31")
def test_done_from_pending_is_409(ivan: Page) -> None:
    resp = ivan.act("done")
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_TRANSITION"


@pytest.mark.fr("FR-31")
def test_answer_is_journaled_by_assignee(ivan: Page) -> None:
    ivan.act("accept")
    journal = ivan.sc.world.db.scalars(
        select(ItemEvent).where(
            ItemEvent.item_id == ivan.item.id, ItemEvent.action == ItemAction.ACCEPTED
        )
    ).all()
    assert [e.actor_type for e in journal] == [ActorType.ASSIGNEE]
    assert journal[0].actor_person_id == ivan.sc.ivan.id


@pytest.mark.fr("FR-31")
def test_answer_writes_link_answered_event(ivan: Page) -> None:
    ivan.act("accept")
    (event,) = events(ivan.sc.world.db, "link_answered", item_id=ivan.item.id)
    assert event.props["answer"] == "accept"
    assert event.props["hours_since_sent"] == 0


@pytest.mark.fr("FR-31")
def test_opening_the_page_writes_link_opened_event(ivan: Page) -> None:
    ivan.client.get(f"/api/v1/p/{ivan.token}")
    (event,) = events(ivan.sc.world.db, "link_opened", meeting_id=ivan.sc.world.meeting.id)
    assert event.props["notification_type"] in {"SUMMARY", "CONFIRM_REQUEST", "CONFIRM_REPEAT"}


@pytest.mark.fr("FR-34")
def test_reschedule_sends_host_response_with_new_due(ivan: Page) -> None:
    ivan.act("reschedule", {"due_date": NEW_DUE.isoformat()})
    (reply,) = mails(ivan.sc.world.db, "HOST_RESPONSE", item_id=ivan.item.id)
    assert reply.recipient_user_id == ivan.sc.world.user.id
    assert reply.recipient_person_id is None
    assert reply.status == NotificationStatus.QUEUED
    assert reply.scheduled_for <= TUESDAY + timedelta(minutes=5)
    text = payload_text(reply)
    assert "Иван Петров" in text
    assert "2030-02-08" in text or "08.02.2030" in text


@pytest.mark.fr("FR-34")
def test_decline_sends_host_response_with_comment(ivan: Page) -> None:
    ivan.act("decline", {"comment": "Это задача Анны"})
    (reply,) = mails(ivan.sc.world.db, "HOST_RESPONSE", item_id=ivan.item.id)
    text = payload_text(reply)
    assert "Иван Петров" in text
    assert "Это задача Анны" in text


@pytest.mark.fr("FR-34")
def test_host_response_button_opens_the_item_card(ivan: Page) -> None:
    ivan.act("decline", {})
    (reply,) = mails(ivan.sc.world.db, "HOST_RESPONSE", item_id=ivan.item.id)
    assert str(ivan.item.id) in payload_text(reply)


@pytest.mark.fr("FR-34")
def test_done_sends_no_host_response(anna: Page) -> None:
    anna.act("done")
    assert mails(anna.sc.world.db, "HOST_RESPONSE") == []


@pytest.mark.fr("FR-34")
def test_accept_sends_no_host_response(ivan: Page) -> None:
    ivan.act("accept")
    assert mails(ivan.sc.world.db, "HOST_RESPONSE") == []


@pytest.mark.fr("FR-34")
def test_each_answer_gets_its_own_host_response(ivan: Page) -> None:
    ivan.act("reschedule", {"due_date": NEW_DUE.isoformat()})
    ivan.act("decline", {})
    replies = mails(ivan.sc.world.db, "HOST_RESPONSE", item_id=ivan.item.id)
    keys = {m.dedupe_key for m in replies}
    assert len(keys) == 2
    assert all(k.startswith(f"{ivan.item.id}:HOST_RESPONSE:") for k in keys)


@pytest.mark.fr("FR-35")
def test_agreed_item_can_be_disputed_by_link(anna: Page) -> None:
    assert anna.act("decline", {"comment": "Не согласен"}).status_code == 200
    assert anna.reload().status == ItemStatus.DISPUTED
    assert len(mails(anna.sc.world.db, "HOST_RESPONSE", item_id=anna.item.id)) == 1


@pytest.mark.fr("FR-35")
def test_agreed_item_can_be_rescheduled_by_link(anna: Page) -> None:
    anna.act("reschedule", {"due_date": NEW_DUE.isoformat()})
    item = anna.reload()
    assert item.status == ItemStatus.ACCEPTED
    assert item.due_date == NEW_DUE


@pytest.mark.fr("FR-31")
def test_reassigned_item_between_link_and_answer_is_403_not_your_item(ivan: Page) -> None:
    ivan.sc.pending.assignee_person_id = ivan.sc.anna.id
    ivan.sc.world.db.flush()
    resp = ivan.act("accept")
    assert resp.status_code == 403
    assert resp.json()["code"] == "NOT_YOUR_ITEM"
    ivan.sc.world.db.refresh(ivan.sc.pending)
    assert ivan.sc.pending.status == ItemStatus.PENDING
