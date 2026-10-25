# ruff: noqa: F811
""" "Разослать", встреча после рассылки и "Скопировать итог" (FR-28, FR-29, FR-36).

Письма проверяются по таблице notifications.
"""

import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AccessLink
from app.modules.items.models import ItemAction, ItemEvent, ItemStatus
from app.modules.notifications.models import NotificationStatus
from app.modules.series.models import MeetingStatus
from tests.acceptance.helpers_mailing import (  # noqa: F401
    API,
    TUESDAY,
    Scenario,
    events,
    frozen,
    host,
    link_for,
    mailbox,
    mails,
    sc,
    send,
    token_of,
    tokens_in,
)
from tests.factories import World, make_access_link, make_gap


def item_headers(item) -> dict[str, str]:
    return {"If-Match": str(item.version)}


@pytest.mark.fr("FR-28")
def test_send_moves_meeting_to_sent(host, sc: Scenario) -> None:
    assert send(host, sc.world).status_code == 200
    meeting = sc.world.meeting
    sc.world.db.refresh(meeting)
    assert meeting.status == MeetingStatus.SENT
    assert meeting.summary_sent_at is not None


@pytest.mark.fr("FR-28")
def test_send_with_open_gaps_requires_confirmation(host, sc: Scenario) -> None:
    make_gap(sc.world.db, sc.pending)
    make_gap(sc.world.db, sc.unassigned)
    refused = send(host, sc.world, confirm=False, expect=None)
    assert refused.status_code == 422
    assert refused.json()["code"] == "GAPS_OPEN"
    sc.world.db.refresh(sc.world.meeting)
    assert sc.world.meeting.status == MeetingStatus.CLOSING
    assert send(host, sc.world, confirm=True).status_code == 200


@pytest.mark.fr("FR-28")
def test_send_accepts_agreed_item_and_sends_it(host, sc: Scenario) -> None:
    send(host, sc.world)
    sc.world.db.refresh(sc.agreed)
    assert sc.agreed.status == ItemStatus.ACCEPTED
    assert sc.agreed.sent_at is not None
    assert sc.agreed.accepted_at is not None


@pytest.mark.fr("FR-28")
def test_send_makes_unagreed_item_pending(host, sc: Scenario) -> None:
    send(host, sc.world)
    sc.world.db.refresh(sc.pending)
    assert sc.pending.status == ItemStatus.PENDING
    assert sc.pending.sent_at is not None
    assert sc.pending.accepted_at is None


@pytest.mark.fr("FR-28")
def test_send_unassigned_item_is_pending_without_link_or_mail(host, sc: Scenario) -> None:
    send(host, sc.world)
    sc.world.db.refresh(sc.unassigned)
    assert sc.unassigned.status == ItemStatus.PENDING
    assert mails(sc.world.db, item_id=sc.unassigned.id) == []
    assert len(link_for(sc.world.db, sc.ivan, sc.world.meeting)) == 1
    links = sc.world.db.scalars(
        select(AccessLink).where(AccessLink.meeting_id == sc.world.meeting.id)
    )
    assert len(links.all()) == 2


@pytest.mark.fr("FR-28")
def test_send_queues_summary_for_every_participant_within_five_minutes(host, sc: Scenario) -> None:
    send(host, sc.world)
    summaries = mails(sc.world.db, "SUMMARY", meeting=sc.world.meeting)
    assert {m.recipient_person_id for m in summaries} == {sc.ivan.id, sc.anna.id, sc.oleg.id}
    assert all(m.status == NotificationStatus.QUEUED for m in summaries)
    assert all(m.scheduled_for <= TUESDAY + timedelta(minutes=5) for m in summaries)
    meeting_id = sc.world.meeting.id
    assert {m.dedupe_key for m in summaries} == {
        f"{meeting_id}:SUMMARY:{p.id}" for p in (sc.ivan, sc.anna, sc.oleg)
    }


@pytest.mark.fr("FR-28")
def test_send_queues_confirm_request_and_repeat_for_pending_item(host, sc: Scenario) -> None:
    send(host, sc.world)
    (request,) = mails(sc.world.db, "CONFIRM_REQUEST", meeting=sc.world.meeting)
    assert request.recipient_person_id == sc.ivan.id
    assert re.fullmatch(rf"{sc.pending.id}:CONFIRM_REQUEST:{sc.ivan.id}:\d+", request.dedupe_key)
    (repeat,) = mails(sc.world.db, "CONFIRM_REPEAT", meeting=sc.world.meeting)
    assert repeat.dedupe_key == f"{sc.world.meeting.id}:CONFIRM_REPEAT:{sc.ivan.id}"


@pytest.mark.fr("FR-28")
def test_second_send_creates_no_new_mails(host, sc: Scenario) -> None:
    send(host, sc.world)
    before = len(mails(sc.world.db))
    again = send(host, sc.world)
    assert again.status_code == 200
    assert again.json()["already_sent"] is True
    assert len(mails(sc.world.db)) == before


@pytest.mark.fr("FR-28")
def test_send_writes_summary_sent_event(host, sc: Scenario) -> None:
    make_gap(sc.world.db, sc.unassigned)
    send(host, sc.world)
    (event,) = events(sc.world.db, "summary_sent", meeting_id=sc.world.meeting.id)
    assert event.props["items_total"] == 3
    assert event.props["items_with_open_gaps"] == 1
    assert event.props["recipients"] == 3


@pytest.mark.fr("FR-28")
def test_send_writes_sent_item_events(host, sc: Scenario) -> None:
    send(host, sc.world)
    actions = {
        e.item_id: e.action
        for e in sc.world.db.scalars(select(ItemEvent).where(ItemEvent.action == ItemAction.SENT))
    }
    assert set(actions) == {sc.pending.id, sc.agreed.id, sc.unassigned.id}


@pytest.mark.fr("FR-28")
def test_item_added_after_send_goes_out_as_separate_confirm_request(host, sc: Scenario) -> None:
    send(host, sc.world)
    summaries = len(mails(sc.world.db, "SUMMARY"))
    late = sc.world.item(
        what="Опоздавший пункт", assignee_person_id=sc.oleg.id, status=ItemStatus.DRAFT
    )
    send(host, sc.world)
    (request,) = mails(sc.world.db, "CONFIRM_REQUEST", item_id=late.id)
    assert request.recipient_person_id == sc.oleg.id
    assert len(mails(sc.world.db, "SUMMARY")) == summaries


@pytest.mark.fr("FR-29")
def test_delete_of_sent_item_is_409_invalid_transition(host, sc: Scenario) -> None:
    send(host, sc.world)
    sc.world.db.refresh(sc.pending)
    resp = host.delete(f"{API}/items/{sc.pending.id}", headers=item_headers(sc.pending))
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_TRANSITION"


def cancel(host, item):
    return host.post(
        f"{API}/items/{item.id}/actions", json={"action": "cancel"}, headers=item_headers(item)
    )


@pytest.mark.fr("FR-29")
@pytest.mark.parametrize("status", [ItemStatus.PENDING, ItemStatus.ACCEPTED, ItemStatus.DISPUTED])
def test_cancel_of_open_item_sets_cancelled(host, sc: Scenario, status: ItemStatus) -> None:
    send(host, sc.world)
    sc.pending.status = status
    sc.world.db.flush()
    sc.world.db.refresh(sc.pending)
    assert cancel(host, sc.pending).status_code == 200
    sc.world.db.refresh(sc.pending)
    assert sc.pending.status == ItemStatus.CANCELLED
    assert sc.pending.cancelled_at is not None


@pytest.mark.fr("FR-29")
@pytest.mark.fr("FR-40")
def test_cancel_cancels_reminder_and_sends_nothing(host, sc: Scenario) -> None:
    send(host, sc.world)
    before = {m.dedupe_key for m in mails(sc.world.db)}
    sc.world.db.refresh(sc.pending)
    cancel(host, sc.pending)
    (reminder,) = mails(sc.world.db, "DUE_TOMORROW", item_id=sc.pending.id)
    assert reminder.status == NotificationStatus.CANCELLED
    assert {m.dedupe_key for m in mails(sc.world.db)} == before


@pytest.mark.fr("FR-29")
@pytest.mark.parametrize("status", [ItemStatus.DONE, ItemStatus.CANCELLED])
def test_cancel_of_closed_item_is_409(host, sc: Scenario, status: ItemStatus) -> None:
    send(host, sc.world)
    sc.pending.status = status
    stamp = TUESDAY
    if status == ItemStatus.DONE:
        sc.pending.done_at = stamp
    else:
        sc.pending.cancelled_at = stamp
    sc.world.db.flush()
    sc.world.db.refresh(sc.pending)
    resp = cancel(host, sc.pending)
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_TRANSITION"


def summary_text(host, world: World):
    return host.post(f"{API}/meetings/{world.meeting.id}/summary-text")


@pytest.mark.fr("FR-36")
def test_summary_text_lists_decisions_with_assignee_and_due(host, sc: Scenario) -> None:
    resp = summary_text(host, sc.world)
    assert resp.status_code == 200
    body = resp.json()
    assert body["items_total"] == 3
    text = body["text"]
    for needle in ("Выгрузка отчёта по оттоку", "Иван Петров", "Согласовать договор", "Анна"):
        assert needle in text


@pytest.mark.fr("FR-36")
def test_summary_text_marks_unassigned_item(host, sc: Scenario) -> None:
    text = summary_text(host, sc.world).json()["text"]
    line = next(line for line in text.splitlines() if "Найти подрядчика" in line)
    assert "не назначен" in line
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}", line)


@pytest.mark.fr("FR-36")
def test_summary_text_has_no_links_or_tokens_after_send(host, sc: Scenario) -> None:
    send(host, sc.world)
    tokens = {token_of(sc.world.db, person, sc.world.meeting) for person in (sc.ivan, sc.anna)}
    text = summary_text(host, sc.world).json()["text"]
    assert "/p/" not in text
    assert not tokens_in(text)
    assert not any(token in text for token in tokens)


@pytest.mark.fr("FR-36")
def test_summary_text_of_live_meeting_is_409(host, sc: Scenario) -> None:
    sc.world.meeting.status = MeetingStatus.LIVE
    sc.world.db.flush()
    resp = summary_text(host, sc.world)
    assert resp.status_code == 409
    assert resp.json()["code"] == "SUMMARY_NOT_READY"


@pytest.mark.fr("FR-36")
def test_summary_text_writes_summary_copied_event(host, sc: Scenario) -> None:
    summary_text(host, sc.world)
    (event,) = events(sc.world.db, "summary_copied", meeting_id=sc.world.meeting.id)
    assert event.props["items_total"] == 3
    assert any(value is False for value in event.props.values())


@pytest.mark.fr("FR-30")
def test_send_revokes_expired_active_link_before_issuing_new(host, sc: Scenario) -> None:
    stale = make_access_link(
        sc.world.db, sc.ivan, sc.world.meeting, expires_at=TUESDAY - timedelta(days=1)
    )
    send(host, sc.world)
    sc.world.db.refresh(stale)
    assert stale.revoked_at is not None
    (fresh,) = link_for(sc.world.db, sc.ivan, sc.world.meeting)
    assert fresh.id != stale.id
    assert fresh.expires_at > TUESDAY


@pytest.mark.fr("FR-30")
def test_one_link_per_person_for_two_items(host, sc: Scenario) -> None:
    sc.world.item(what="Второй пункт Ивана", assignee_person_id=sc.ivan.id)
    send(host, sc.world)
    assert len(link_for(sc.world.db, sc.ivan, sc.world.meeting, active=False)) == 1
