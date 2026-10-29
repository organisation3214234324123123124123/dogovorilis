import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketState

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import SESSION_COOKIE
from app.modules.live import bus, service

router = APIRouter(tags=["live"])

CLOSE_INTERNAL = 1011
CLOSE_OVERLOAD = 1013
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN_ORIGIN = 4403
CLOSE_NOT_FOUND = 4404


def _origin_of(url: str, *, strict: bool = False) -> str | None:
    """Origin (схема://хост[:порт]) из URL; None, если URL не абсолютный.

    strict: заголовок Origin обязан быть чистым origin, с путём он не годится.
    """
    try:
        parts = urlsplit(url.strip())
        if not parts.scheme or not parts.hostname:
            return None
        if strict and (parts.path or parts.query or parts.fragment or parts.username):
            return None
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        default = {"http": 80, "https": 443}.get(parts.scheme.lower())
        port = parts.port
        suffix = f":{port}" if port is not None and port != default else ""
        return f"{parts.scheme.lower()}://{host}{suffix}"
    except ValueError:
        return None


def _origin_allowed(origin: str) -> bool:
    settings = get_settings()
    if origin in settings.cors_origins:
        return True
    normalized = _origin_of(origin, strict=True)
    return normalized is not None and normalized == _origin_of(settings.public_base_url)


@contextlib.contextmanager
def _session(websocket: WebSocket) -> Iterator[Session]:
    """Сессия БД короткая и закрывается до цикла, чтобы соединение не висело всё время WS."""
    provider = websocket.app.dependency_overrides.get(get_db, get_db)
    with contextlib.contextmanager(provider)() as db:
        yield db


def _authorize(websocket: WebSocket, meeting_id: uuid.UUID) -> int:
    with _session(websocket) as db:
        return service.authorize(db, websocket.cookies.get(SESSION_COOKIE), meeting_id)


def _resync(websocket: WebSocket, meeting_id: uuid.UUID, since: object) -> tuple[list[dict], int]:
    with _session(websocket) as db:
        return service.resync_frames(db, meeting_id, since)


@dataclass
class _Sync:
    """Общее состояние пересылки: кадры с `version <= floor` клиент уже получил при догрузке."""

    lock: asyncio.Lock
    floor: int | None = None


@router.websocket("/ws/meetings/{meeting_id}")
async def meeting_ws(websocket: WebSocket, meeting_id: uuid.UUID) -> None:
    """WebSocket встречи (FR-14). Только cookie-сессия: браузер не умеет ставить Authorization в
    WebSocket.
    """
    origin = websocket.headers.get("origin")
    if origin is not None and not _origin_allowed(origin):
        await websocket.accept()
        await websocket.close(code=CLOSE_FORBIDDEN_ORIGIN, reason="origin")
        return
    try:
        exp = await run_in_threadpool(_authorize, websocket, meeting_id)
    except AppError as exc:
        await websocket.accept()
        code = CLOSE_UNAUTHORIZED if exc.status == 401 else CLOSE_NOT_FOUND
        await websocket.close(code=code, reason=exc.code)
        return
    try:
        # подписка раньше accept: событие, опубликованное сразу после подключения, не теряется
        async with bus.subscribe(meeting_id) as events:
            await websocket.accept()
            code = await _serve(websocket, meeting_id, events, exp)
            if code:
                with contextlib.suppress(Exception):
                    await websocket.close(code=code)
    except bus.BusError:
        with contextlib.suppress(Exception):
            if websocket.application_state == WebSocketState.CONNECTING:
                await websocket.accept()
            await websocket.close(code=CLOSE_INTERNAL)


async def _serve(
    websocket: WebSocket, meeting_id: uuid.UUID, events: AsyncIterator[dict], exp: int
) -> int | None:
    """Шлёт события, пока клиент не закроется; возвращает код закрытия со стороны сервера."""
    sync = _Sync(asyncio.Lock())
    pump = asyncio.create_task(_forward(websocket, events, sync))
    recv = asyncio.create_task(websocket.receive_text())
    try:
        while True:
            done, _ = await asyncio.wait(
                {pump, recv}, timeout=max(exp - time.time(), 0), return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                return CLOSE_UNAUTHORIZED  # токен истёк
            if pump in done:
                if pump.cancelled():
                    return None
                exc = pump.exception()
                if isinstance(exc, WebSocketDisconnect):
                    return None
                return CLOSE_OVERLOAD if isinstance(exc, bus.Overflow) else CLOSE_INTERNAL
            await _on_message(websocket, meeting_id, sync, recv.result())
            recv = asyncio.create_task(websocket.receive_text())
    except WebSocketDisconnect:
        return None
    finally:
        for task in (pump, recv):
            task.cancel()
        await asyncio.wait({pump, recv})  # gather проглотил бы отмену самого обработчика
        for task in (pump, recv):
            if not task.cancelled():
                task.exception()  # забрать исключение, чтобы asyncio не ругался


async def _on_message(websocket: WebSocket, meeting_id: uuid.UUID, sync: _Sync, text: str) -> None:
    """Кадр `resync`: живые кадры ждут под замком, пока уходит журнал; прочие игнорируем."""
    try:
        message = json.loads(text)
    except ValueError:
        return
    if not isinstance(message, dict) or message.get("type") != "resync":
        return
    async with sync.lock:
        # запрос в потоке, но под sync.lock: догрузка не обгоняется живыми кадрами
        frames, sync.floor = await run_in_threadpool(
            _resync, websocket, meeting_id, message.get("since_version")
        )
        for frame in frames:
            await websocket.send_json(frame)


async def _forward(websocket: WebSocket, events: AsyncIterator[dict], sync: _Sync) -> None:
    async for event in events:
        async with sync.lock:
            number = event.get("version")
            if sync.floor is not None and isinstance(number, int) and number <= sync.floor:
                continue
            await websocket.send_json(event)
