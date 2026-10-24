"""Транспорт событий встречи: канал на встречу.

`live_bus=memory` - внутрипроцессный (тесты, один процесс); `redis` - pub/sub, чтобы события
доходили между воркерами API и из Celery. Подписчик получает ограниченную очередь (QUEUE_MAX):
переполнение или сбой транспорта приходят из потока исключением.
"""

import asyncio
import contextlib
import json
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

import redis
import redis.asyncio

from app.core.config import get_settings

QUEUE_MAX = 100


class BusError(Exception):
    """Сбой транспорта: подписчик должен закрыться (1011)."""


class Overflow(Exception):
    """Подписчик не успевает читать: очередь переполнена (1013)."""


Queue = asyncio.Queue["dict | Exception"]
_local: dict[str, set[tuple[asyncio.AbstractEventLoop, Queue]]] = defaultdict(set)


def _channel(meeting_id: uuid.UUID | str) -> str:
    return f"live:meeting:{meeting_id}"


def _offer(queue: Queue, item: "dict | Exception") -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(Overflow())


@lru_cache
def _publisher(url: str) -> redis.Redis:
    return redis.Redis.from_url(url)  # один клиент с пулом на процесс; потокобезопасен


def publish(meeting_id: uuid.UUID, event: dict) -> None:
    if get_settings().live_bus == "redis":
        _publisher(get_settings().redis_url).publish(
            _channel(meeting_id), json.dumps(event, default=str)
        )
        return
    for loop, queue in list(_local[_channel(meeting_id)]):
        loop.call_soon_threadsafe(_offer, queue, event)


class _Hub:
    """Один pubsub на процесс (на цикл событий): раздаёт сообщения локальным очередям."""

    def __init__(self, url: str) -> None:
        self.client = redis.asyncio.Redis.from_url(url)
        self.pubsub = self.client.pubsub()
        self.queues: dict[str, set[Queue]] = {}
        self.reader: asyncio.Task | None = None

    async def _read(self) -> None:
        try:
            while self.queues:
                msg = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    continue
                channel = msg["channel"]
                channel = channel.decode() if isinstance(channel, bytes) else channel
                event = json.loads(msg["data"])
                for queue in list(self.queues.get(channel, ())):
                    _offer(queue, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            for queues in self.queues.values():
                for queue in queues:
                    _offer(queue, BusError("transport failed"))
            _hubs.pop(asyncio.get_running_loop(), None)

    async def add(self, channel: str, queue: Queue) -> None:
        first = channel not in self.queues
        self.queues.setdefault(channel, set()).add(queue)
        if first:
            await self.pubsub.subscribe(channel)
        if self.reader is None or self.reader.done():
            self.reader = asyncio.create_task(self._read())

    async def remove(self, channel: str, queue: Queue) -> None:
        self.queues[channel].discard(queue)
        if self.queues[channel]:
            return
        del self.queues[channel]
        with contextlib.suppress(Exception):
            await self.pubsub.unsubscribe(channel)
        if not self.queues:
            _hubs.pop(asyncio.get_running_loop(), None)
            await self.close()

    async def close(self) -> None:
        if self.reader and not self.reader.done():
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.pubsub.aclose()
            await self.client.aclose()


_hubs: dict[asyncio.AbstractEventLoop, _Hub] = {}


async def _drain(queue: Queue) -> AsyncIterator[dict]:
    while True:
        item = await queue.get()
        if isinstance(item, Exception):
            raise item
        yield item


@asynccontextmanager
async def subscribe(meeting_id: uuid.UUID) -> AsyncIterator[AsyncIterator[dict]]:
    """Подписка живёт с входа в контекст; внутри - поток событий встречи."""
    channel, loop, queue = _channel(meeting_id), asyncio.get_running_loop(), Queue(QUEUE_MAX)
    if get_settings().live_bus == "redis":
        hub = _hubs.get(loop)
        if hub is None:
            hub = _hubs[loop] = _Hub(get_settings().redis_url)
        try:
            await hub.add(channel, queue)
        except Exception as exc:
            await hub.remove(channel, queue)
            raise BusError("subscribe failed") from exc
        try:
            yield _drain(queue)
        finally:
            await hub.remove(channel, queue)
        return
    _local[channel].add((loop, queue))
    try:
        yield _drain(queue)
    finally:
        _local[channel].discard((loop, queue))
        if not _local[channel]:
            del _local[channel]
