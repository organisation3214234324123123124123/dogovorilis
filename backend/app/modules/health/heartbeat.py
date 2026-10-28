"""Пульс воркера (NFR-11): время последнего запуска `send_due_mails` лежит в Redis."""

from contextlib import suppress
from datetime import UTC, datetime
from functools import lru_cache

import redis

from app.core.config import get_settings

KEY = "health:worker_last_run_at"


@lru_cache
def _cached(url: str) -> redis.Redis:
    # один клиент с пулом на процесс и адрес; потокобезопасен
    return redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)


def _client() -> redis.Redis:
    return _cached(get_settings().redis_url)


def beat() -> None:
    """Отметить запуск воркера. Redis недоступен - письма важнее пульса."""
    with suppress(redis.RedisError):
        _client().set(KEY, datetime.now(UTC).isoformat())


def last_run() -> datetime | None:
    """Время последнего запуска; RedisError - Redis недоступен (решает вызывающий)."""
    raw = _client().get(KEY)
    return datetime.fromisoformat(raw.decode()) if raw else None
