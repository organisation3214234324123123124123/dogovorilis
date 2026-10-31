# ruff: noqa: F811
"""Письма разных серий не сливаются (BR-11, BR-16); создание пункта во встрече сериализуется
с рассылкой итога (FR-10, FR-28)."""

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.core.db import SessionLocal, get_engine
from app.core.security import SESSION_COOKIE, create_access_token
from app.main import create_app
from app.models import Notification
from app.modules.items.models import Item, ItemStatus
from app.modules.notifications.models import NotificationStatus
from app.modules.series.models import MeetingStatus
from tests.acceptance.helpers_mailing import TUESDAY, Scenario, frozen, sc  # noqa: F401
from tests.factories import (
    add_member,
    make_item,
    make_meeting,
    make_organization,
    make_participant,
    make_person,
    make_series,
    make_user,
)
from tests.test_live_resync_edge_cases import LOCK_WAIT_S, _drop_user, _lock_waiters

API = "/api/v1"


def _deliver_log(monkeypatch) -> list[tuple[uuid.UUID, int]]:
    from app.modules.notifications import sender

    log: list[tuple[uuid.UUID, int]] = []

    def deliver(mail):
        parts = getattr(mail, "letter_parts", None) or [(mail.type, mail.payload)]
        log.append((mail.meeting_id, len(parts)))

    monkeypatch.setattr(sender, "deliver", deliver)
    return log


def _foreign_series_meeting(sc: Scenario):
    db = sc.world.db
    other_host = make_user(db)
    series = make_series(db, other_host)
    add_member(db, series, sc.ivan)
    return make_meeting(db, series)


def _queue(sc: Scenario, meeting, key: str, kind: str = "ITEM_CHANGED") -> Notification:
    mail = Notification(
        type=kind,
        recipient_person_id=sc.ivan.id,
        meeting_id=meeting.id,
        payload={"what": "x"},
        dedupe_key=key,
        scheduled_for=TUESDAY,
        status=NotificationStatus.QUEUED,
    )
    sc.world.db.add(mail)
    sc.world.db.flush()
    return mail


@pytest.mark.fr("FR-30")
@pytest.mark.fr("BR-16")
def test_same_type_mails_of_one_recipient_from_two_series_are_two_letters(
    sc: Scenario, monkeypatch
) -> None:
    from app.modules.notifications import sender

    foreign = _foreign_series_meeting(sc)
    _queue(sc, sc.world.meeting, "ser-a")
    _queue(sc, foreign, "ser-b")
    log = _deliver_log(monkeypatch)
    sender.send_due(sc.world.db, TUESDAY)
    assert sorted(log) == sorted([(sc.world.meeting.id, 1), (foreign.id, 1)])


@pytest.mark.fr("FR-30")
@pytest.mark.fr("BR-11")
def test_exhausted_limit_of_one_series_does_not_hold_back_the_other(
    sc: Scenario, monkeypatch
) -> None:
    from app.modules.notifications import sender

    foreign = _foreign_series_meeting(sc)
    for n in range(3):
        old = _queue(sc, foreign, f"ser-old-{n}")
        old.status = NotificationStatus.SENT
        old.sent_at = TUESDAY - timedelta(days=1, hours=n)
    own = _queue(sc, sc.world.meeting, "ser-own")
    over = _queue(sc, foreign, "ser-over")
    log = _deliver_log(monkeypatch)
    sender.send_due(sc.world.db, TUESDAY)
    sc.world.db.refresh(own)
    assert own.status == NotificationStatus.SENT
    sc.world.db.refresh(over)
    assert over.status == NotificationStatus.QUEUED
    assert over.postponed
    assert log == [(sc.world.meeting.id, 1)]


@pytest.fixture
def paused_send() -> Iterator[threading.Event]:
    """Первый SELECT ... FOR UPDATE по meetings (рассылка итога) ждёт, пока кто-то встанет в
    очередь за замком встречи (до 3 с)."""
    locked = threading.Event()

    def pause(conn, cursor, statement, parameters, context, executemany) -> None:
        if locked.is_set() or "FOR UPDATE" not in statement or "FROM meetings" not in statement:
            return
        locked.set()
        deadline = time.monotonic() + LOCK_WAIT_S
        while time.monotonic() < deadline and _lock_waiters() == 0:
            time.sleep(0.05)
        time.sleep(0.2)

    event.listen(Engine, "after_cursor_execute", pause)
    try:
        yield locked
    finally:
        event.remove(Engine, "after_cursor_execute", pause)


def _seed_live_meeting() -> tuple[uuid.UUID, uuid.UUID]:
    with SessionLocal(bind=get_engine()) as s:
        user = make_user(s)
        org = make_organization(s, user, is_own=True)
        person = make_person(s, user, org, full_name="Иван Петров")
        series = make_series(s, user)
        add_member(s, series, person)
        meeting = make_meeting(s, series, status=MeetingStatus.LIVE, started_at=datetime.now(UTC))
        make_participant(s, meeting, person)
        make_item(
            s,
            meeting,
            what="Подготовить отчёт",
            assignee_person_id=person.id,
            due_date=date.today() + timedelta(days=7),
        )
        s.commit()
        return user.id, meeting.id


@pytest.mark.fr("FR-10")
@pytest.mark.fr("FR-28")
def test_item_created_while_meeting_is_being_sent_goes_out_as_in_a_sent_meeting(
    db, paused_send: threading.Event
) -> None:
    user_id, meeting_id = _seed_live_meeting()
    token = create_access_token(user_id)
    statuses: dict[str, int] = {}
    errors: list[BaseException] = []

    def call(name: str, url: str, body: dict) -> None:
        try:
            with TestClient(create_app(), raise_server_exceptions=False) as c:
                c.cookies.set(SESSION_COOKIE, token)
                statuses[name] = c.post(url, json=body).status_code
        except BaseException as exc:
            errors.append(exc)

    send = threading.Thread(
        target=call,
        args=("send", f"{API}/meetings/{meeting_id}/send", {"confirm_open_gaps": True}),
    )
    create = threading.Thread(
        target=call,
        args=(
            "create",
            f"{API}/meetings/{meeting_id}/items",
            {"raw_text": "Иван подготовит план до пятницы"},
        ),
    )
    try:
        send.start()
        assert paused_send.wait(15)
        create.start()
        send.join(30)
        create.join(30)
        assert not errors
        assert statuses == {"send": 200, "create": 201}
        with SessionLocal(bind=get_engine()) as s:
            created = s.scalars(
                select(Item).where(Item.meeting_id == meeting_id, Item.raw_text.like("%план%"))
            ).one()
            requests = s.scalars(
                select(Notification).where(
                    Notification.item_id == created.id, Notification.type == "CONFIRM_REQUEST"
                )
            ).all()
            assert created.status == ItemStatus.PENDING
            assert len(requests) == 1
    finally:
        _drop_user(user_id)
