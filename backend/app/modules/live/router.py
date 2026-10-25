import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
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


def _authorize(websocket: WebSocket, meeting_id: uuid.UUID) -> int:
    """Сессия БД короткая и закрывается до цикла, чтобы соединение не висело всё время WS."""
    provider = websocket.app.dependency_overrides.get(get_db, get_db)
    with contextlib.contextmanager(provider)() as db:
        return service.authorize(db, websocket.cookies.get(SESSION_COOKIE), meeting_id)


@router.websocket("/ws/meetings/{meeting_id}")
async def meeting_ws(websocket: WebSocket, meeting_id: uuid.UUID) -> None:
    """WebSocket встречи (FR-14). Только cookie-сессия: браузер не умеет ставить Authorization в
    WebSocket.
    """
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in get_settings().cors_origins:
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
            code = await _serve(websocket, events, exp)
            if code:
                with contextlib.suppress(Exception):
                    await websocket.close(code=code)
    except bus.BusError:
        with contextlib.suppress(Exception):
            if websocket.application_state == WebSocketState.CONNECTING:
                await websocket.accept()
            await websocket.close(code=CLOSE_INTERNAL)


async def _serve(websocket: WebSocket, events: AsyncIterator[dict], exp: int) -> int | None:
    """Шлёт события, пока клиент не закроется; возвращает код закрытия со стороны сервера."""
    pump = asyncio.create_task(_forward(websocket, events))
    recv = asyncio.create_task(websocket.receive_text())  # входящие кадры игнорируем
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
            recv.result()
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


async def _forward(websocket: WebSocket, events: AsyncIterator[dict]) -> None:
    async for event in events:
        await websocket.send_json(event)
