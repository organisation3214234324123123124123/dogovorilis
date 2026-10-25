import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import login_as
from tests.factories import World, make_user

pytestmark = pytest.mark.fr("FR-14")


def connect_code(client: TestClient, meeting_id) -> int:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws,
    ):
        ws.receive_text()
    return exc.value.code


def test_ws_without_cookie_closes_4401(client: TestClient, world: World) -> None:
    assert connect_code(client, world.meeting.id) == 4401


def test_ws_with_garbage_cookie_closes_4401(client: TestClient, world: World) -> None:
    client.cookies.set("access_token", "garbage")
    assert connect_code(client, world.meeting.id) == 4401


def test_ws_foreign_meeting_closes_4404(client: TestClient, world: World) -> None:
    login_as(client, make_user(world.db))
    assert connect_code(client, world.meeting.id) == 4404


def test_ws_unknown_meeting_closes_4404(authed: TestClient) -> None:
    assert connect_code(authed, uuid.uuid4()) == 4404


# publish() проверяется на внутрипроцессной шине (app.modules.live.bus), без Redis.


def _event(meeting_id, type_: str = "item.updated", **extra) -> dict:
    return {"type": type_, "meeting_id": str(meeting_id), **extra}


def test_ws_owner_connects_and_stays_open(authed: TestClient, world: World) -> None:
    with authed.websocket_connect(f"/ws/meetings/{world.meeting.id}") as ws:
        # закрытие сервером (раньше 4501) дало бы WebSocketDisconnect на receive
        from app.modules.live.service import publish

        publish(world.meeting.id, _event(world.meeting.id, "item.created"))
        assert ws.receive_json()["type"] == "item.created"


@pytest.mark.parametrize(
    "type_", ["item.created", "item.updated", "item.deleted", "gaps.updated", "closure.opened"]
)
def test_publish_delivers_frame_with_type(authed: TestClient, world: World, type_: str) -> None:
    from app.modules.live.service import publish

    with authed.websocket_connect(f"/ws/meetings/{world.meeting.id}") as ws:
        publish(world.meeting.id, _event(world.meeting.id, type_, version=2))
        frame = ws.receive_json()
    assert frame["type"] == type_
    assert frame["meeting_id"] == str(world.meeting.id)
    assert frame["version"] == 2


@pytest.mark.parametrize("type_", ["item.created", "gaps.updated", "meeting.sent"])
def test_publish_frame_carries_event_equal_to_type(
    authed: TestClient, world: World, type_: str
) -> None:
    from app.modules.live.service import publish

    with authed.websocket_connect(f"/ws/meetings/{world.meeting.id}") as ws:
        publish(world.meeting.id, _event(world.meeting.id, type_))
        frame = ws.receive_json()
    assert frame["event"] == type_
    assert frame["event"] == frame["type"]


def test_publish_reaches_all_subscribers_of_meeting(authed: TestClient, world: World) -> None:
    from app.modules.live.service import publish

    url = f"/ws/meetings/{world.meeting.id}"
    with authed.websocket_connect(url) as ws1, authed.websocket_connect(url) as ws2:
        publish(world.meeting.id, _event(world.meeting.id, "gaps.updated"))
        assert ws1.receive_json()["type"] == "gaps.updated"
        assert ws2.receive_json()["type"] == "gaps.updated"


def test_publish_not_delivered_to_other_meeting(authed: TestClient, world: World) -> None:
    from app.modules.live.service import publish
    from tests.factories import make_meeting

    other = make_meeting(world.db, world.series)
    with (
        authed.websocket_connect(f"/ws/meetings/{world.meeting.id}") as ws_a,
        authed.websocket_connect(f"/ws/meetings/{other.id}") as ws_b,
    ):
        publish(other.id, _event(other.id, "closure.opened"))
        assert ws_b.receive_json()["type"] == "closure.opened"
        publish(world.meeting.id, _event(world.meeting.id, "item.created"))
        # первый кадр у A -- только про его встречу
        assert ws_a.receive_json()["meeting_id"] == str(world.meeting.id)


def _ws_route():
    from app.modules.live.router import router

    return next(r for r in router.routes if getattr(r, "path", "") == "/ws/meetings/{meeting_id}")


def test_live_bus_setting_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings(live_bus="memory").live_bus == "memory"  # type: ignore[call-arg]
    assert Settings(live_bus="redis").live_bus == "redis"  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(live_bus="kafka")  # type: ignore[call-arg]
    monkeypatch.delenv("LIVE_BUS", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    assert Settings(_env_file=None).live_bus == "redis"  # type: ignore[call-arg]


def test_tests_run_with_memory_bus() -> None:
    from app.core.config import get_settings

    assert get_settings().live_bus == "memory"


def test_ws_route_does_not_hold_db_session_dependency() -> None:
    from app.core.db import get_db

    calls = [d.call for d in _ws_route().dependant.dependencies]
    assert get_db not in calls


def test_ws_foreign_origin_closes_4403(authed: TestClient, world: World) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        authed.websocket_connect(
            f"/ws/meetings/{world.meeting.id}", headers={"origin": "https://evil.example"}
        ) as ws,
    ):
        ws.receive_text()
    assert exc.value.code == 4403


def test_ws_allowed_origin_connects(authed: TestClient, world: World) -> None:
    from app.core.config import get_settings
    from app.modules.live.service import publish

    origin = get_settings().cors_origins[0]
    with authed.websocket_connect(
        f"/ws/meetings/{world.meeting.id}", headers={"origin": origin}
    ) as ws:
        publish(world.meeting.id, _event(world.meeting.id, "item.created"))
        assert ws.receive_json()["type"] == "item.created"


def test_ws_closes_4401_when_token_expires(client: TestClient, world: World) -> None:
    from app.core.security import SESSION_COOKIE, create_access_token

    client.cookies.set(SESSION_COOKIE, create_access_token(world.user.id, ttl=2))
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(f"/ws/meetings/{world.meeting.id}") as ws,
    ):
        ws.receive_text()  # без событий; сервер сам закроет по exp
    assert exc.value.code == 4401


def test_pump_failure_closes_1011(
    authed: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    from app.modules.live import bus

    @contextlib.asynccontextmanager
    async def broken(meeting_id):
        async def events():
            raise RuntimeError("transport down")
            yield {}

        yield events()

    monkeypatch.setattr(bus, "subscribe", broken)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        authed.websocket_connect(f"/ws/meetings/{world.meeting.id}") as ws,
    ):
        ws.receive_text()
    assert exc.value.code == 1011


def test_slow_subscriber_queue_overflow_closes_1013(
    authed: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from starlette.websockets import WebSocket

    from app.modules.live.service import publish

    original = WebSocket.send_json

    async def slow_send(self, data, mode="text"):
        await asyncio.sleep(0.05)
        await original(self, data, mode)

    monkeypatch.setattr(WebSocket, "send_json", slow_send)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        authed.websocket_connect(f"/ws/meetings/{world.meeting.id}") as ws,
    ):
        for _ in range(300):  # очередь подписчика ограничена 100
            publish(world.meeting.id, _event(world.meeting.id, "item.updated", version=1))
        while True:
            ws.receive_json()
    assert exc.value.code == 1013


def _redis_available() -> bool:
    try:
        import redis

        from app.core.config import get_settings

        client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


@pytest.mark.skipif(not _redis_available(), reason="Redis недоступен")
def test_redis_transport_publish_reaches_only_own_meeting(
    monkeypatch: pytest.MonkeyPatch, settings_reset: None
) -> None:
    import asyncio
    import contextlib

    from app.modules.live import bus

    monkeypatch.setenv("LIVE_BUS", "redis")
    mine, other = uuid.uuid4(), uuid.uuid4()

    async def scenario() -> tuple[dict, list[dict]]:
        async with bus.subscribe(mine) as events_mine, bus.subscribe(other) as events_other:
            await asyncio.to_thread(bus.publish, mine, {"type": "item.created", "n": 1})
            got = await asyncio.wait_for(anext(events_mine), 5)
            foreign: list[dict] = []
            with contextlib.suppress(TimeoutError):
                foreign.append(await asyncio.wait_for(anext(events_other), 0.5))
            return got, foreign

    got, foreign = asyncio.run(scenario())
    assert got == {"type": "item.created", "n": 1}
    assert foreign == []
