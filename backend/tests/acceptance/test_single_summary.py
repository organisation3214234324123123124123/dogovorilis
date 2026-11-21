# ruff: noqa: F811
"""Одно итоговое письмо на участника: SUMMARY заменяет CONFIRM_REQUEST при «Разослать»."""

from datetime import UTC, datetime

import pytest

from app.modules.items.models import ItemStatus
from app.modules.notifications.models import NotificationStatus
from tests.acceptance.helpers_mailing import (  # noqa: F401
    DUE_FRIDAY,
    TUESDAY,
    Mailbox,
    Scenario,
    actions_in,
    draft,
    frozen,
    host,
    mailbox,
    mails,
    payload_text,
    sc,
    send,
)
from tests.factories import add_member, make_person

REPEAT_AT = datetime(2030, 1, 16, 12, 0, tzinfo=UTC)
REMINDER_AT = datetime(2030, 1, 24, 7, 0, tzinfo=UTC)

pytestmark = pytest.mark.fr("FR-30")


def test_confirm_needed_only_for_assignee_with_pending_items(host, sc: Scenario) -> None:
    send(host, sc.world)
    (ivan,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.ivan.id)
    (anna,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.anna.id)
    (oleg,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.oleg.id)
    assert ivan.payload["confirm_needed"] is True
    assert anna.payload["confirm_needed"] is False
    assert "confirm_needed" not in oleg.payload


def test_summary_lead_asks_to_confirm_only_when_pending(
    host, sc: Scenario, mailbox: Mailbox
) -> None:
    send(host, sc.world)
    mailbox.run(TUESDAY)
    (ivan_letter,) = mailbox.to(sc.ivan)
    (anna_letter,) = mailbox.to(sc.anna)
    assert "Подтвердите, пожалуйста" in mailbox.text_of(ivan_letter)
    assert "Подтвердите, пожалуйста" not in mailbox.text_of(anna_letter)
    assert ivan_letter["subject"] == "Итоги встречи «" + sc.world.series.title + "»"


def test_partial_answer_repeat_contains_only_unanswered_items(
    host, sc: Scenario, mailbox: Mailbox
) -> None:
    second = draft(sc.world, sc.ivan, DUE_FRIDAY, what="Подготовить презентацию")
    sc.world.db.flush()
    send(host, sc.world)
    mailbox.run(TUESDAY)
    sc.world.db.refresh(second)
    second.status = ItemStatus.ACCEPTED
    sc.world.db.flush()
    mailbox.run(REPEAT_AT)
    repeat_letter = mailbox.to(sc.ivan)[-1]
    text = mailbox.text_of(repeat_letter)
    assert sc.pending.what in text
    assert second.what not in text
    assert f"item={second.id}" not in text


def test_repeat_is_cancelled_when_all_pending_answered(
    host, sc: Scenario, mailbox: Mailbox
) -> None:
    send(host, sc.world)
    mailbox.run(TUESDAY)
    sc.world.db.refresh(sc.pending)
    sc.pending.status = ItemStatus.ACCEPTED
    sc.world.db.flush()
    mailbox.run(REPEAT_AT)
    (repeat,) = mails(sc.world.db, "CONFIRM_REPEAT", meeting=sc.world.meeting)
    assert repeat.status == NotificationStatus.CANCELLED
    assert len(mailbox.to(sc.ivan)) == 1


def test_summary_repeat_and_reminder_are_not_postponed_by_weekly_limit(
    host, sc: Scenario, mailbox: Mailbox
) -> None:
    send(host, sc.world)
    for at in (TUESDAY, REPEAT_AT, REMINDER_AT):
        mailbox.run(at)
    kinds = [letter["subject"] for letter in mailbox.to(sc.ivan)]
    assert len(kinds) == 3
    for kind in ("SUMMARY", "CONFIRM_REPEAT", "DUE_TOMORROW"):
        (mail,) = mails(sc.world.db, kind, recipient_person_id=sc.ivan.id)
        assert mail.status == NotificationStatus.SENT


def test_assignee_outside_participants_gets_summary_with_buttons(host, sc: Scenario) -> None:
    outsider = make_person(sc.world.db, sc.world.user, sc.world.org, full_name="Пётр Внешний")
    add_member(sc.world.db, sc.world.series, outsider)
    item = draft(sc.world, outsider, DUE_FRIDAY, what="Согласовать смету")
    sc.world.db.flush()
    resp = send(host, sc.world)
    assert resp.json()["recipients"] == 3  # счётчик считает только участников
    (summary,) = mails(sc.world.db, "SUMMARY", recipient_person_id=outsider.id)
    text = payload_text(summary)
    assert actions_in(text) == {"accept", "reschedule", "decline"}
    assert f"item={item.id}&n=SUMMARY" in text
    assert mails(sc.world.db, "CONFIRM_REQUEST", recipient_person_id=outsider.id) == []


def test_item_added_after_send_still_gets_confirm_request(host, sc: Scenario) -> None:
    from tests.acceptance.test_fr_28_send import add_oleg_to_series, post_late_item

    add_oleg_to_series(sc)
    send(host, sc.world)
    late = post_late_item(host, sc)
    (request,) = mails(sc.world.db, "CONFIRM_REQUEST", item_id=late.id)
    assert request.recipient_person_id == sc.oleg.id
