from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis
from fastapi.testclient import TestClient
from freezegun import freeze_time

from app.core.config import get_settings
from app.core.db import get_db
from app.main import create_app
from app.modules.notifications.models import NotificationStatus, NotificationType
from app.modules.notifications.tasks import send_due_mails
from tests.factories import World, make_notification

pytestmark = pytest.mark.fr("NFR-11")

HEALTH = "/api/v1/health"
REQUIRED = {"status", "db", "email_queue", "llm", "worker_last_run_at"}


@pytest.fixture(autouse=True)
def redis_db(monkeypatch: pytest.MonkeyPatch, settings_reset: None) -> Iterator[None]:
    parts = urlsplit(get_settings().redis_url)
    url = urlunsplit(parts._replace(path="/15"))
    try:
        redis.Redis.from_url(url).flushdb()
    except redis.RedisError as exc:
        pytest.skip(f"Redis недоступен: {exc}")
    monkeypatch.setenv("REDIS_URL", url)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def set_llm(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setenv("LLM_API_KEY", key)
    get_settings.cache_clear()


def get_health(client: TestClient) -> Any:
    return client.get(HEALTH)


def queue_mail(world: World, age: timedelta) -> None:
    make_notification(
        world.db,
        type=NotificationType.SUMMARY,
        recipient_person_id=world.person.id,
        status=NotificationStatus.QUEUED,
        scheduled_for=datetime.now(UTC) - age,
    )


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_health_default_version(client: TestClient) -> None:
    r = get_health(client)
    assert r.status_code == 200
    assert r.json()["version"] == "dev"


def test_health_version_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    get_settings.cache_clear()
    r = TestClient(create_app()).get(HEALTH)
    assert r.status_code == 200
    assert r.json()["version"] == "1.2.3"


def test_health_is_ok_when_everything_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_llm(monkeypatch, "k" * 20)
    send_due_mails()
    r = get_health(client)
    assert (r.status_code, r.json()["status"], r.json()["db"]) == (200, "ok", "ok")


def test_health_is_degraded_with_200_when_llm_is_stub(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_llm(monkeypatch, "")
    send_due_mails()
    r = get_health(client)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["llm"]["available"]) == ("degraded", False)


def test_health_is_degraded_when_oldest_queued_mail_is_older_than_5_minutes(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_llm(monkeypatch, "k" * 20)
    send_due_mails()
    queue_mail(world, timedelta(minutes=6))
    r = get_health(client)
    assert (r.status_code, r.json()["status"]) == (200, "degraded")


def test_health_is_ok_when_oldest_queued_mail_is_younger_than_5_minutes(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_llm(monkeypatch, "k" * 20)
    send_due_mails()
    queue_mail(world, timedelta(minutes=4))
    assert get_health(client).json()["status"] == "ok"


def test_health_is_degraded_when_worker_silent_longer_than_3_minutes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_llm(monkeypatch, "k" * 20)
    start = datetime.now(UTC)
    with freeze_time(start):
        send_due_mails()
    with freeze_time(start + timedelta(minutes=4)):
        r = get_health(client)
    assert (r.status_code, r.json()["status"]) == (200, "degraded")


def test_health_is_ok_when_worker_ran_2_minutes_ago(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_llm(monkeypatch, "k" * 20)
    start = datetime.now(UTC)
    with freeze_time(start):
        send_due_mails()
    with freeze_time(start + timedelta(minutes=2)):
        assert get_health(client).json()["status"] == "ok"


def test_health_is_503_down_when_redis_is_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    get_settings.cache_clear()
    r = get_health(client)
    assert r.status_code == 503
    body = r.json()
    assert (body["status"], body["db"]) == ("down", "ok")
    assert set(body) >= REQUIRED


def test_health_is_503_down_when_database_is_unavailable(client: TestClient) -> None:
    broken = MagicMock()
    broken.execute.side_effect = RuntimeError("db down")

    def broken_db() -> Iterator[Any]:
        yield broken

    client.app.dependency_overrides[get_db] = broken_db
    r = get_health(client)
    assert r.status_code == 503
    body = r.json()
    assert (body["status"], body["db"]) == ("down", "down")
    assert set(body) >= REQUIRED


def test_worker_tick_sets_worker_last_run_at_without_any_mail(client: TestClient) -> None:
    before = datetime.now(UTC)
    assert send_due_mails() == 0
    value = get_health(client).json()["worker_last_run_at"]
    assert value is not None
    assert before - timedelta(seconds=2) <= parse(value) <= datetime.now(UTC) + timedelta(seconds=2)


def test_worker_last_run_at_is_time_of_latest_tick(client: TestClient) -> None:
    first = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    second = first + timedelta(minutes=1)
    with freeze_time(first):
        send_due_mails()
    with freeze_time(second):
        send_due_mails()
        value = get_health(client).json()["worker_last_run_at"]
    assert parse(value) == second


def test_worker_last_run_at_is_not_time_of_last_sent_mail(client: TestClient, world: World) -> None:
    sent = make_notification(
        world.db,
        type=NotificationType.SUMMARY,
        recipient_person_id=world.person.id,
        status=NotificationStatus.SENT,
    )
    sent.sent_at = datetime.now(UTC)
    world.db.flush()
    assert get_health(client).json()["worker_last_run_at"] is None


def test_health_schema_requires_worker_last_run_at() -> None:
    schema = create_app().openapi()["components"]["schemas"]["Health"]
    assert set(schema["required"]) >= REQUIRED


def test_heartbeat_reuses_redis_client() -> None:
    from app.modules.health import heartbeat

    assert heartbeat._client() is heartbeat._client()


def test_redis_error_on_heartbeat_does_not_break_mail_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.notifications import sender

    def broken_set(self: Any, *args: Any, **kwargs: Any) -> None:
        raise redis.ConnectionError("redis down")

    monkeypatch.setattr(redis.Redis, "set", broken_set)
    monkeypatch.setattr(sender, "send_due", lambda db, now: 2)
    assert send_due_mails() == 2
