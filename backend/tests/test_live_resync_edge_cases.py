import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.core.db import SessionLocal, get_engine
from app.core.security import SESSION_COOKIE, create_access_token
from app.main import create_app
from app.modules.live import service as live_service
from app.modules.series.models import MeetingStatus
from tests.factories import (
    World,
    add_member,
    make_item,
    make_meeting,
    make_organization,
    make_participant,
    make_person,
    make_series,
    make_user,
)

pytestmark = pytest.mark.fr("FR-14")

API = "/api/v1"
CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.yaml"
LOCK_WAIT_S = 3.0


def ws_url(meeting_id) -> str:
    return f"/api/v1/ws/meetings/{meeting_id}"


def events_version(client: TestClient, meeting_id) -> int:
    resp = client.get(f"{API}/meetings/{meeting_id}")
    assert resp.status_code == 200
    return resp.json()["events_version"]


def add_item(client: TestClient, meeting_id, raw: str = "Иван подготовит отчёт к пятнице") -> dict:
    resp = client.post(f"{API}/meetings/{meeting_id}/items", json={"raw_text": raw})
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
def live_world(world: World) -> World:
    world.meeting.status = MeetingStatus.LIVE
    world.db.flush()
    return world


def test_resync_reset_frame_carries_meeting_id(authed: TestClient, live_world: World) -> None:
    add_item(authed, live_world.meeting.id)
    with authed.websocket_connect(ws_url(live_world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": 10_000})
        frame = ws.receive_json()
    assert frame["event"] == "resync.reset"
    assert frame.get("meeting_id") == str(live_world.meeting.id)


def test_resync_reset_frame_has_every_required_live_event_field(
    authed: TestClient, live_world: World
) -> None:
    schema = yaml.safe_load(CONTRACT.read_text())["components"]["schemas"]["LiveEvent"]
    with authed.websocket_connect(ws_url(live_world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": "abc"})
        frame = ws.receive_json()
    assert frame["event"] == "resync.reset"
    assert set(schema["required"]) <= set(frame)


def test_live_frame_without_number_does_not_break_connection(
    authed: TestClient, live_world: World
) -> None:
    meeting_id = live_world.meeting.id
    add_item(authed, meeting_id)
    current = events_version(authed, meeting_id)
    with authed.websocket_connect(ws_url(meeting_id)) as ws:
        ws.send_json({"type": "resync", "since_version": current - 1})
        assert ws.receive_json()["version"] == current
        live_service.publish(
            meeting_id,
            {"type": "agenda.updated", "meeting_id": str(meeting_id), "version": None, "data": {}},
        )
        add_item(authed, meeting_id, "Анна согласует бюджет до среды")
        target = events_version(authed, meeting_id)
        numbered: list[int] = []
        while not numbered or numbered[-1] < target:
            frame = ws.receive_json()
            if isinstance(frame.get("version"), int):
                numbered.append(frame["version"])
    assert numbered == list(range(current + 1, target + 1))


# Взаимоблокировка: PATCH пункта держит строку пункта и затем берёт строку встречи (номер
# события, FK журнала), рассылка итога держит строку встречи и затем берёт строки пунктов.
# Правку останавливаем сразу после блокировки пункта, пока рассылка не встанет в очередь.


def _seed_meeting() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, int]:
    with SessionLocal(bind=get_engine()) as s:
        user = make_user(s)
        org = make_organization(s, user, is_own=True)
        person = make_person(s, user, org)
        series = make_series(s, user)
        add_member(s, series, person)
        meeting = make_meeting(s, series, status=MeetingStatus.LIVE, started_at=datetime.now(UTC))
        make_participant(s, meeting, person)
        item = make_item(
            s,
            meeting,
            what="Подготовить отчёт",
            assignee_person_id=person.id,
            due_date=date.today() + timedelta(days=7),
        )
        s.commit()
        return user.id, meeting.id, item.id, item.version


def _lock_waiters() -> int:
    with get_engine().connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                "WHERE NOT l.granted AND a.datname = current_database()"
            )
        ).scalar_one()


def _drop_user(user_id: uuid.UUID) -> None:
    with SessionLocal(bind=get_engine()) as s:
        s.execute(text("SET LOCAL app.retention_cleanup = 'on'"))
        s.execute(
            text(
                "DELETE FROM notifications WHERE meeting_id IN (SELECT m.id FROM meetings m "
                "JOIN series r ON r.id = m.series_id WHERE r.owner_user_id = :u)"
            ),
            {"u": user_id},
        )
        s.execute(text("DELETE FROM analytics_events WHERE user_id = :u"), {"u": user_id})
        s.execute(text("DELETE FROM series WHERE owner_user_id = :u"), {"u": user_id})
        s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        s.commit()


@pytest.fixture
def paused_edit() -> Iterator[threading.Event]:
    """Первый SELECT ... FOR UPDATE по items ждёт, пока кто-то встанет в очередь (до 3 с)."""
    locked = threading.Event()

    def pause(conn, cursor, statement, parameters, context, executemany) -> None:
        if locked.is_set() or "FOR UPDATE" not in statement or "FROM items" not in statement:
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


def _edit_and_send_together(locked: threading.Event) -> tuple[int, int]:
    user_id, meeting_id, item_id, version = _seed_meeting()
    statuses: dict[str, int] = {}
    errors: list[BaseException] = []
    token = create_access_token(user_id)

    def call(name: str, method: str, url: str, **kw) -> None:
        try:
            with TestClient(create_app(), raise_server_exceptions=False) as c:
                c.cookies.set(SESSION_COOKIE, token)
                statuses[name] = c.request(method, url, **kw).status_code
        except BaseException as exc:
            errors.append(exc)

    edit = threading.Thread(
        target=call,
        args=("edit", "PATCH", f"{API}/items/{item_id}"),
        kwargs={
            "json": {"what": "Подготовить отчёт к пятнице"},
            "headers": {"If-Match": str(version)},
        },
    )
    send = threading.Thread(
        target=call,
        args=("send", "POST", f"{API}/meetings/{meeting_id}/send"),
        kwargs={"json": {"confirm_open_gaps": True}},
    )
    try:
        edit.start()
        assert locked.wait(15)
        send.start()
        edit.join(30)
        send.join(30)
        assert not errors
        return statuses["edit"], statuses["send"]
    finally:
        _drop_user(user_id)


@pytest.mark.parametrize("attempt", range(3))
def test_item_edit_and_summary_send_in_parallel_both_succeed(
    db, paused_edit: threading.Event, attempt: int
) -> None:
    assert _edit_and_send_together(paused_edit) == (200, 200)
