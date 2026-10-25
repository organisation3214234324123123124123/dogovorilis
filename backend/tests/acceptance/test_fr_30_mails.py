# ruff: noqa: F811
"""Письма и отправщик (``helpers_mailing``): FR-30, FR-32 (кнопки писем), FR-33, FR-35, FR-40."""

import base64
import logging
import re
from datetime import UTC, date, datetime, timedelta

import pytest

from app.models import AccessLink, Notification
from app.modules.items.models import ItemStatus
from app.modules.notifications.models import NotificationStatus
from tests.acceptance.helpers_mailing import (  # noqa: F401
    FRIDAY_ISO,
    TUESDAY,
    Scenario,
    actions_in,
    digest,
    events,
    frozen,
    hash_is_stored,
    host,
    link_for,
    mailbox,
    mails,
    payload_text,
    raw_token_stored_anywhere,
    sc,
    send,
    stored_payload,
    token_of,
    tokens_in,
)
from tests.factories import make_person


def queue(sc: Scenario, kind: str, *, item=None, person=None, when=None, key=None, **kw):
    mail = Notification(
        type=kind,
        recipient_person_id=(person or sc.ivan).id if kind != "HOST_RESPONSE" else None,
        recipient_user_id=sc.world.user.id if kind == "HOST_RESPONSE" else None,
        meeting_id=sc.world.meeting.id,
        item_id=item.id if item else None,
        payload=kw.pop("payload", {"what": item.what if item else "x"}),
        dedupe_key=key
        or f"{sc.world.meeting.id}:{kind}:{len(mails(sc.world.db))}:{kw.get('n', 0)}",
        scheduled_for=when or TUESDAY,
        status=NotificationStatus.QUEUED,
    )
    sc.world.db.add(mail)
    sc.world.db.flush()
    return mail


@pytest.mark.fr("FR-30")
def test_confirm_request_payload_has_item_meeting_and_working_link(host, sc: Scenario) -> None:
    send(host, sc.world)
    (request,) = mails(sc.world.db, "CONFIRM_REQUEST", meeting=sc.world.meeting)
    text = payload_text(request)
    assert sc.pending.what in text
    (token,) = set(tokens_in(text))
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    assert len(base64.urlsafe_b64decode(token + "=")) == 32
    assert hash_is_stored(sc.world.db, token)
    assert len(link_for(sc.world.db, sc.ivan, sc.world.meeting)) == 1


@pytest.mark.fr("FR-30")
def test_confirm_request_has_three_buttons(host, sc: Scenario) -> None:
    send(host, sc.world)
    (request,) = mails(sc.world.db, "CONFIRM_REQUEST", meeting=sc.world.meeting)
    assert actions_in(payload_text(request)) == {"accept", "reschedule", "decline"}


@pytest.mark.fr("FR-30")
def test_agreed_item_gets_no_confirm_request(host, sc: Scenario) -> None:
    send(host, sc.world)
    assert mails(sc.world.db, "CONFIRM_REQUEST", item_id=sc.agreed.id) == []
    assert len(mails(sc.world.db, "CONFIRM_REQUEST", meeting=sc.world.meeting)) == 1


@pytest.mark.fr("FR-30")
def test_link_expires_in_14_days_when_open_due_dates_are_old(host, sc: Scenario) -> None:
    # BR-18: срок + 30 дней меньше выдачи + 14, поэтому действует выдача + 14 дней
    sc.pending.due_date = date(2029, 12, 1)
    sc.world.db.flush()
    send(host, sc.world)
    (link,) = link_for(sc.world.db, sc.ivan, sc.world.meeting)
    assert abs(link.expires_at - (TUESDAY + timedelta(days=14))) < timedelta(days=1)


@pytest.mark.fr("FR-30")
def test_link_expires_30_days_after_latest_open_due_date(host, sc: Scenario) -> None:
    sc.pending.due_date = date(2030, 3, 29)
    sc.world.db.flush()
    send(host, sc.world)
    (link,) = link_for(sc.world.db, sc.ivan, sc.world.meeting)
    expected = datetime(2030, 4, 28, tzinfo=UTC)
    assert abs(link.expires_at - expected) < timedelta(days=1)


@pytest.mark.fr("FR-30")
def test_token_never_reaches_logs(host, sc: Scenario, caplog) -> None:
    from app.modules.notifications.sender import send_due

    caplog.set_level(logging.DEBUG)
    send(host, sc.world)
    send_due(sc.world.db, TUESDAY + timedelta(minutes=5))
    tokens = {token_of(sc.world.db, p, sc.world.meeting) for p in (sc.ivan, sc.anna)}
    assert not any(token in caplog.text for token in tokens)


@pytest.mark.fr("FR-32")
def test_email_buttons_point_to_page_with_action_parameter(host, sc: Scenario) -> None:
    send(host, sc.world)
    (summary,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.ivan.id)
    assert actions_in(payload_text(summary)) == {"accept", "reschedule", "decline"}
    assert re.search(r"/p/[A-Za-z0-9_-]{43}\?action=accept", payload_text(summary))


@pytest.mark.fr("FR-32")
def test_opening_email_links_changes_nothing(host, client, sc: Scenario) -> None:
    send(host, sc.world)
    token = token_of(sc.world.db, sc.ivan, sc.world.meeting)
    before = {m.dedupe_key: m.status for m in mails(sc.world.db)}
    for action in ("accept", "reschedule", "decline", "done", "accept"):
        assert client.get(f"/api/v1/p/{token}?action={action}").status_code == 200
    sc.world.db.refresh(sc.pending)
    assert sc.pending.status == ItemStatus.PENDING
    assert sc.pending.version == 1
    assert {m.dedupe_key: m.status for m in mails(sc.world.db)} == before


@pytest.mark.fr("FR-35")
def test_agreed_item_summary_has_link_without_accept_button(host, sc: Scenario) -> None:
    send(host, sc.world)
    (summary,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.anna.id)
    text = payload_text(summary)
    assert actions_in(text) == {"reschedule", "decline"}
    assert tokens_in(text)
    assert mails(sc.world.db, "CONFIRM_REQUEST", recipient_person_id=sc.anna.id) == []


@pytest.mark.fr("FR-35")
def test_pending_item_summary_has_three_buttons(host, sc: Scenario) -> None:
    send(host, sc.world)
    (summary,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.ivan.id)
    assert actions_in(payload_text(summary)) == {"accept", "reschedule", "decline"}


@pytest.mark.fr("FR-35")
def test_participant_without_items_gets_summary_without_link(host, sc: Scenario) -> None:
    send(host, sc.world)
    (summary,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.oleg.id)
    assert not tokens_in(payload_text(summary))
    assert link_for(sc.world.db, sc.oleg, sc.world.meeting, active=False) == []


@pytest.mark.fr("FR-35")
def test_summary_lists_all_meeting_decisions(host, sc: Scenario) -> None:
    send(host, sc.world)
    (summary,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.oleg.id)
    text = payload_text(summary)
    assert sc.pending.what in text
    assert sc.agreed.what in text
    assert "не назначен" in text


def mail_key(sc: Scenario, kind: str) -> str:
    return mails(sc.world.db, kind, meeting=sc.world.meeting)[0].scheduled_for.isoformat()


@pytest.mark.fr("FR-33")
def test_confirm_repeat_is_scheduled_next_working_day_after_tuesday(host, sc: Scenario) -> None:
    send(host, sc.world)
    (repeat,) = mails(sc.world.db, "CONFIRM_REPEAT", meeting=sc.world.meeting)
    assert repeat.scheduled_for == datetime(2030, 1, 16, 12, 0, tzinfo=UTC)


@pytest.mark.fr("FR-33")
def test_confirm_repeat_after_friday_goes_out_on_monday(frozen, host, sc: Scenario) -> None:
    frozen.move_to(FRIDAY_ISO)
    from tests.conftest import login_as

    login_as(host, sc.world.user)
    send(host, sc.world)
    (repeat,) = mails(sc.world.db, "CONFIRM_REPEAT", meeting=sc.world.meeting)
    assert repeat.scheduled_for == datetime(2030, 1, 21, 12, 0, tzinfo=UTC)


@pytest.mark.fr("FR-33")
def test_confirm_repeat_lists_only_unconfirmed_items(host, sc: Scenario) -> None:
    from app.modules.notifications.sender import send_due

    second = sc.world.item(
        what="Второй пункт Ивана", assignee_person_id=sc.ivan.id, due_date=date(2030, 1, 29)
    )
    send(host, sc.world)
    sc.world.db.refresh(second)
    second.status = ItemStatus.ACCEPTED
    sc.world.db.flush()
    send_due(sc.world.db, datetime(2030, 1, 16, 12, 0, tzinfo=UTC))
    (repeat,) = mails(sc.world.db, "CONFIRM_REPEAT", meeting=sc.world.meeting)
    assert repeat.status == NotificationStatus.SENT
    assert sc.pending.what in payload_text(repeat)
    assert second.what not in payload_text(repeat)


@pytest.mark.fr("FR-33")
def test_confirm_repeat_is_cancelled_when_nothing_is_pending(host, sc: Scenario) -> None:
    from app.modules.notifications.sender import send_due

    send(host, sc.world)
    sc.world.db.refresh(sc.pending)
    sc.pending.status = ItemStatus.ACCEPTED
    sc.world.db.flush()
    send_due(sc.world.db, datetime(2030, 1, 16, 12, 0, tzinfo=UTC))
    (repeat,) = mails(sc.world.db, "CONFIRM_REPEAT", meeting=sc.world.meeting)
    assert repeat.status == NotificationStatus.CANCELLED


@pytest.mark.fr("FR-33")
def test_only_one_repeat_is_ever_sent(host, sc: Scenario) -> None:
    from app.modules.notifications.sender import send_due

    send(host, sc.world)
    for day in (16, 17, 18, 21):
        send_due(sc.world.db, datetime(2030, 1, day, 12, 0, tzinfo=UTC))
    repeats = mails(sc.world.db, "CONFIRM_REPEAT", meeting=sc.world.meeting)
    assert [m.status for m in repeats] == [NotificationStatus.SENT]
    assert len(mails(sc.world.db, "CONFIRM_REQUEST", meeting=sc.world.meeting)) == 1


@pytest.mark.fr("FR-40")
def test_due_tomorrow_is_planned_for_previous_working_day_0900_local(host, sc: Scenario) -> None:
    send(host, sc.world)
    (reminder,) = mails(sc.world.db, "DUE_TOMORROW", item_id=sc.pending.id)
    assert reminder.scheduled_for == datetime(2030, 1, 24, 6, 0, tzinfo=UTC)
    assert reminder.recipient_person_id == sc.ivan.id
    assert re.fullmatch(rf"{sc.pending.id}:DUE_TOMORROW:2030-01-25:\d+", reminder.dedupe_key)


@pytest.mark.fr("FR-40")
def test_due_tomorrow_before_monday_goes_out_on_friday(host, sc: Scenario) -> None:
    send(host, sc.world)
    (reminder,) = mails(sc.world.db, "DUE_TOMORROW", item_id=sc.agreed.id)
    assert reminder.scheduled_for == datetime(2030, 1, 18, 6, 0, tzinfo=UTC)


@pytest.mark.fr("FR-40")
def test_due_tomorrow_is_not_created_when_due_is_less_than_a_working_day_away(
    frozen, host, sc: Scenario
) -> None:
    from tests.conftest import login_as

    frozen.move_to(FRIDAY_ISO)
    login_as(host, sc.world.user)
    sc.pending.due_date = date(2030, 1, 18)
    sc.world.db.flush()
    send(host, sc.world)
    assert mails(sc.world.db, "DUE_TOMORROW", item_id=sc.pending.id) == []


@pytest.mark.fr("FR-40")
def test_due_tomorrow_is_not_created_without_assignee_or_due(host, sc: Scenario) -> None:
    sc.world.item(what="Без срока", assignee_person_id=sc.ivan.id, due_date=None)
    send(host, sc.world)
    assert mails(sc.world.db, "DUE_TOMORROW", item_id=sc.unassigned.id) == []
    assert len(mails(sc.world.db, "DUE_TOMORROW", meeting=sc.world.meeting)) == 2


@pytest.mark.fr("FR-40")
def test_due_tomorrow_buttons_are_done_and_reschedule(host, sc: Scenario) -> None:
    send(host, sc.world)
    (reminder,) = mails(sc.world.db, "DUE_TOMORROW", item_id=sc.pending.id)
    assert actions_in(payload_text(reminder)) == {"done", "reschedule"}


@pytest.mark.fr("FR-40")
def test_due_tomorrow_is_not_sent_for_closed_item(host, sc: Scenario) -> None:
    from app.modules.notifications.sender import send_due

    send(host, sc.world)
    sc.world.db.refresh(sc.pending)
    sc.pending.status = ItemStatus.DONE
    sc.pending.done_at = TUESDAY
    sc.world.db.flush()
    send_due(sc.world.db, datetime(2030, 1, 24, 6, 0, tzinfo=UTC))
    (reminder,) = mails(sc.world.db, "DUE_TOMORROW", item_id=sc.pending.id)
    assert reminder.status == NotificationStatus.CANCELLED


@pytest.mark.fr("FR-40")
def test_sent_due_tomorrow_writes_reminder_sent_event(host, sc: Scenario) -> None:
    from app.modules.notifications.sender import send_due

    send(host, sc.world)
    # письма рассылки уходят сразу; иначе накопленные письма недели упёрлись бы в лимит BR-11
    send_due(sc.world.db, TUESDAY + timedelta(minutes=5))
    # отложенные по лимиту письма уходят в свой срок (BR-11), потом приходит черёд напоминания
    send_due(sc.world.db, datetime(2030, 1, 16, 6, 0, tzinfo=UTC))
    send_due(sc.world.db, datetime(2030, 1, 24, 6, 0, tzinfo=UTC))
    (reminder,) = mails(sc.world.db, "DUE_TOMORROW", item_id=sc.pending.id)
    assert reminder.status == NotificationStatus.SENT
    assert len(events(sc.world.db, "reminder_sent", item_id=sc.pending.id)) == 1


@pytest.mark.fr("FR-30")
def test_send_due_sends_only_what_is_due_and_marks_sent(host, sc: Scenario) -> None:
    from app.modules.notifications.sender import send_due

    send(host, sc.world)
    now = TUESDAY + timedelta(minutes=5)
    assert send_due(sc.world.db, now) >= 1
    for mail in mails(sc.world.db, meeting=sc.world.meeting):
        if mail.scheduled_for <= now:
            assert mail.status == NotificationStatus.SENT
            assert mail.sent_at is not None
        else:
            assert mail.status == NotificationStatus.QUEUED


@pytest.mark.fr("FR-30")
def test_send_due_does_not_send_twice(host, sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    send(host, sc.world)
    calls: list[object] = []
    monkeypatch.setattr(sender, "deliver", calls.append)
    now = TUESDAY + timedelta(minutes=5)
    sender.send_due(sc.world.db, now)
    first = len(calls)
    sender.send_due(sc.world.db, now)
    assert first > 0
    assert len(calls) == first


@pytest.mark.fr("FR-30")
@pytest.mark.parametrize(("delay_min", "attempt"), [(5, 1), (15, 2), (60, 3)])
def test_transient_mail_error_is_retried_with_backoff(
    sc: Scenario, monkeypatch, delay_min: int, attempt: int
) -> None:
    from app.modules.notifications import sender

    mail = queue(sc, "CONFIRM_REQUEST", item=sc.pending, key="retry-1")
    mail.attempts = attempt - 1
    sc.world.db.flush()

    def boom(_mail):
        raise sender.TransientMailError("timeout")

    monkeypatch.setattr(sender, "deliver", boom)
    sender.send_due(sc.world.db, TUESDAY)
    sc.world.db.refresh(mail)
    assert mail.status == NotificationStatus.QUEUED
    assert mail.attempts == attempt
    assert mail.scheduled_for == TUESDAY + timedelta(minutes=delay_min)


@pytest.mark.fr("FR-30")
def test_transient_error_after_three_retries_fails(sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    mail = queue(sc, "CONFIRM_REQUEST", item=sc.pending, key="retry-4")
    mail.attempts = 3
    sc.world.db.flush()

    def boom(_mail):
        raise sender.TransientMailError("timeout")

    monkeypatch.setattr(sender, "deliver", boom)
    sender.send_due(sc.world.db, TUESDAY)
    sc.world.db.refresh(mail)
    assert mail.status == NotificationStatus.FAILED
    assert mail.last_error


@pytest.mark.fr("FR-30")
def test_permanent_mail_error_fails_immediately(sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    mail = queue(sc, "CONFIRM_REQUEST", item=sc.pending, key="perm-1")

    def boom(_mail):
        raise sender.PermanentMailError("550 no such user")

    monkeypatch.setattr(sender, "deliver", boom)
    sender.send_due(sc.world.db, TUESDAY)
    sc.world.db.refresh(mail)
    assert mail.status == NotificationStatus.FAILED
    assert mail.attempts == 1
    assert "550" in mail.last_error


@pytest.mark.fr("FR-30")
def test_same_type_mails_to_one_recipient_go_out_as_one(sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    second = sc.world.item(
        what="Ещё пункт",
        assignee_person_id=sc.ivan.id,
        due_date=date(2030, 2, 1),
        status=ItemStatus.PENDING,
    )
    sc.pending.status = ItemStatus.PENDING
    queue(sc, "ITEM_CHANGED", item=sc.pending, key="chg-1")
    queue(sc, "ITEM_CHANGED", item=second, key="chg-2")
    calls: list[object] = []
    monkeypatch.setattr(sender, "deliver", calls.append)
    sender.send_due(sc.world.db, TUESDAY)
    assert len(calls) == 1


@pytest.mark.fr("FR-30")
def test_fourth_mail_in_a_week_from_one_series_is_postponed(sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    for n in range(3):
        mail = queue(sc, "ITEM_CHANGED", item=sc.pending, key=f"week-{n}")
        mail.status = NotificationStatus.SENT
        mail.sent_at = TUESDAY - timedelta(days=1, hours=n)
    held = queue(sc, "DUE_TOMORROW", item=sc.pending, key="week-4")
    sc.world.db.flush()
    calls: list[object] = []
    monkeypatch.setattr(sender, "deliver", calls.append)
    sender.send_due(sc.world.db, TUESDAY)
    sc.world.db.refresh(held)
    assert not calls
    assert held.status == NotificationStatus.QUEUED
    assert held.scheduled_for > TUESDAY
    assert held.scheduled_for.astimezone(UTC).weekday() < 5


@pytest.mark.fr("FR-34")
def test_host_response_is_exempt_from_weekly_limit(sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    others = [make_person(sc.world.db, sc.world.user, sc.world.org) for _ in range(4)]
    for n, person in enumerate(others):
        queue(sc, "HOST_RESPONSE", item=sc.pending, person=person, key=f"hr-{n}")
    monkeypatch.setattr(sender, "deliver", lambda _mail: None)
    sender.send_due(sc.world.db, TUESDAY)
    sent = mails(sc.world.db, "HOST_RESPONSE", meeting=sc.world.meeting)
    assert [m.status for m in sent] == [NotificationStatus.SENT] * 4


@pytest.mark.fr("FR-30")
def test_link_rows_are_unique_per_person_and_meeting(host, sc: Scenario) -> None:
    send(host, sc.world)
    send(host, sc.world)
    links = sc.world.db.query(AccessLink).filter(AccessLink.meeting_id == sc.world.meeting.id).all()
    assert len([lnk for lnk in links if lnk.revoked_at is None]) == 2


def _fake_deliver(monkeypatch, fail_on: int | None = None):
    """Заглушка почты: запоминает письма; вызов с номером fail_on падает временной ошибкой."""
    from app.modules.notifications import sender

    sent: list[tuple[str, list[str]]] = []
    state = {"calls": 0}

    def deliver(mail):
        state["calls"] += 1
        parts = [str(t) for t, _ in getattr(mail, "letter_parts", [])] or [str(mail.type)]
        if state["calls"] == fail_on:
            raise sender.TransientMailError("timeout")
        sent.append((str(mail.type), parts))

    monkeypatch.setattr(sender, "deliver", deliver)
    return sent, state


@pytest.mark.fr("FR-30")
def test_no_live_token_in_payload_or_database(host, sc: Scenario, mailbox) -> None:
    send(host, sc.world)
    tokens = {token_of(sc.world.db, p, sc.world.meeting) for p in (sc.ivan, sc.anna)}
    assert len(tokens) == 2
    for mail in mails(sc.world.db, meeting=sc.world.meeting):
        stored = stored_payload(mail)
        assert "/p/" not in stored
        assert not any(token in stored for token in tokens)
    for token in tokens:
        assert not raw_token_stored_anywhere(sc.world.db, token)
        assert hash_is_stored(sc.world.db, token)
    (summary,) = mails(sc.world.db, "SUMMARY", recipient_person_id=sc.ivan.id)
    (summary_token,) = set(mailbox.tokens(mailbox.letter_for(summary)))
    assert summary_token in payload_text(summary)


@pytest.mark.fr("FR-32")
def test_email_buttons_carry_the_item_id(host, sc: Scenario) -> None:
    send(host, sc.world)
    (request,) = mails(sc.world.db, "CONFIRM_REQUEST", meeting=sc.world.meeting)
    assert re.search(
        rf"/p/[A-Za-z0-9_-]{{43}}\?action=accept&item={sc.pending.id}", payload_text(request)
    )
    (reminder,) = mails(sc.world.db, "DUE_TOMORROW", item_id=sc.pending.id)
    assert f"item={sc.pending.id}" in payload_text(reminder)


@pytest.mark.fr("FR-30")
def test_subject_has_no_line_breaks_and_is_short(host, sc: Scenario) -> None:
    from app.modules.notifications.render import render

    long_what = "Очень\nдлинное\r\nрешение " + "слово " * 80
    mail = queue(sc, "CONFIRM_REQUEST", item=sc.pending, key="subj-1", payload={"what": long_what})
    letter = render([(mail.type, mail.payload)])
    assert "\n" not in letter.subject
    assert "\r" not in letter.subject
    assert 0 < len(letter.subject) <= 150


@pytest.mark.fr("FR-30")
def test_smtp_failure_on_second_letter_does_not_resend_the_first(sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    first = queue(sc, "CONFIRM_REQUEST", item=sc.pending, key="iso-1")
    second = queue(sc, "ITEM_CHANGED", item=sc.pending, key="iso-2")
    sent, _ = _fake_deliver(monkeypatch, fail_on=2)
    sender.send_due(sc.world.db, TUESDAY)
    sc.world.db.refresh(first)
    sc.world.db.refresh(second)
    assert len(sent) == 1
    statuses = {first.status, second.status}
    assert statuses == {NotificationStatus.SENT, NotificationStatus.QUEUED}
    failed = first if first.status == NotificationStatus.QUEUED else second
    assert failed.attempts == 1
    sender.send_due(sc.world.db, TUESDAY + timedelta(minutes=6))
    # отправлено ровно по одному разу каждое: первое письмо не ушло повторно
    assert [kind for kind, _ in sent].count("CONFIRM_REQUEST") == 1
    assert [kind for kind, _ in sent].count("ITEM_CHANGED") == 1
    sc.world.db.refresh(failed)
    assert failed.status == NotificationStatus.SENT


@pytest.mark.fr("FR-30")
def test_rows_are_claimed_as_sending_before_smtp(sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    mail = queue(sc, "CONFIRM_REQUEST", item=sc.pending, key="claim-1")
    seen: dict[str, object] = {}

    def deliver(m):
        seen["status"] = m.status
        seen["claimed_at"] = m.claimed_at

    monkeypatch.setattr(sender, "deliver", deliver)
    sender.send_due(sc.world.db, TUESDAY)
    assert seen["status"] == NotificationStatus.SENDING
    assert seen["claimed_at"] is not None
    sc.world.db.refresh(mail)
    assert mail.status == NotificationStatus.SENT


@pytest.mark.fr("FR-30")
def test_stuck_sending_row_returns_to_queue_and_is_sent(sc: Scenario, monkeypatch) -> None:
    from app.modules.notifications import sender

    stuck = queue(sc, "CONFIRM_REQUEST", item=sc.pending, key="stuck-1")
    stuck.status = NotificationStatus.SENDING
    stuck.claimed_at = TUESDAY - timedelta(minutes=11)
    fresh = queue(sc, "ITEM_CHANGED", item=sc.pending, key="stuck-2")
    fresh.status = NotificationStatus.SENDING
    fresh.claimed_at = TUESDAY - timedelta(minutes=5)
    sc.world.db.flush()
    sent, _ = _fake_deliver(monkeypatch)
    sender.send_due(sc.world.db, TUESDAY)
    sc.world.db.refresh(stuck)
    sc.world.db.refresh(fresh)
    assert stuck.status == NotificationStatus.SENT
    assert [kind for kind, _ in sent] == ["CONFIRM_REQUEST"]
    assert fresh.status == NotificationStatus.SENDING  # воркер ещё может его отправлять


@pytest.mark.fr("FR-30")
def test_weekly_limit_counts_letters_of_the_current_run_and_postpones_into_one_digest(
    sc: Scenario, monkeypatch
) -> None:
    from app.modules.notifications import sender

    kinds = ["SUMMARY", "CONFIRM_REQUEST", "DUE_TOMORROW", "ITEM_CHANGED", "ITEM_REASSIGNED"]
    rows = [queue(sc, k, item=sc.pending, key=f"lim-{k}") for k in kinds]
    sent, _ = _fake_deliver(monkeypatch)
    sender.send_due(sc.world.db, TUESDAY)
    assert len(sent) == 3  # в первом запуске уходят три письма недели, не пять
    held = [m for m in rows if m.status == NotificationStatus.QUEUED]
    for mail in rows:
        sc.world.db.refresh(mail)
    held = [m for m in rows if m.status == NotificationStatus.QUEUED]
    assert len(held) == 2
    for mail in held:
        # 09:00 МСК следующего рабочего дня (среда 2030-01-16)
        assert mail.scheduled_for == datetime(2030, 1, 16, 6, 0, tzinfo=UTC)
    sender.send_due(sc.world.db, datetime(2030, 1, 16, 6, 0, tzinfo=UTC))
    assert len(sent) == 4  # отложенные ушли одним письмом (digest)
    assert len(sent[-1][1]) == 2
    for mail in held:
        sc.world.db.refresh(mail)
        assert mail.status == NotificationStatus.SENT
