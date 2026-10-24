"""Блокировка входа, гонки, keep_changes, лимит писем сброса, url, чистка login_attempts."""

import inspect
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.config import get_settings
from app.core.errors import AppError, Unauthorized
from app.modules.auth.models import LoginAttempt
from app.modules.notifications.models import Notification, NotificationType
from tests.conftest import PASSWORD

LOGIN = "/api/v1/auth/login"
RESET = "/api/v1/auth/password-reset"
START = "2030-01-01T10:00:00+00:00"
MIN = timedelta(minutes=1)


def attempt(client: TestClient, email: str, password: str = "wrong-pass"):
    return client.post(LOGIN, json={"email": email, "password": password})


def fail_times(client: TestClient, email: str, n: int) -> None:
    for _ in range(n):
        assert attempt(client, email).status_code == 401


def reset_mails(db: Session, user) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(
                Notification.type == NotificationType.PASSWORD_RESET,
                Notification.recipient_user_id == user.id,
            )
            .order_by(Notification.scheduled_for)
        )
    )


# ---- Блокировка отсчитывается от 10-й неудачи ---


@pytest.mark.fr("FR-02")
def test_lock_counts_from_tenth_failure_not_from_oldest(client: TestClient, world) -> None:
    email = world.user.email
    with freeze_time(START) as clock:
        fail_times(client, email, 5)
        clock.tick(10 * MIN)
        fail_times(client, email, 5)
        # T+10м: 10-я неудача, блок до T+25м
        resp = attempt(client, email, PASSWORD)
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) == 15 * 60
        clock.tick(6 * MIN)  # T+16м
        resp = attempt(client, email, PASSWORD)
        assert resp.status_code == 429
        assert 8 * 60 <= int(resp.headers["Retry-After"]) <= 9 * 60
        clock.tick(8 * MIN + timedelta(seconds=30))  # T+24м30с
        assert attempt(client, email, PASSWORD).status_code == 429
        clock.tick(MIN)  # T+25м30с
        assert attempt(client, email, PASSWORD).status_code == 200


@pytest.mark.fr("FR-02")
def test_retry_after_is_seconds_to_end_of_lock(client: TestClient, world) -> None:
    email = world.user.email
    with freeze_time(START) as clock:
        fail_times(client, email, 10)
        clock.tick(5 * MIN)
        resp = attempt(client, email, PASSWORD)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) == 10 * 60


# ---- Гонки: не больше 10 проверок пароля до блокировки ---


@pytest.fixture
def committed_email(migrated_db: None, settings_reset: None) -> Iterator[str]:
    """Уникальный e-mail без пользователя, чистка после теста (потоки видят только committed)."""
    from app.core.db import get_engine

    email = f"race-{uuid.uuid4().hex}@example.com"
    try:
        yield email
    finally:
        with get_engine().begin() as conn:
            conn.execute(delete(LoginAttempt).where(LoginAttempt.email == email))


def _attempts_count(email: str) -> int:
    from app.core.db import get_engine

    with get_engine().connect() as conn:
        return conn.execute(
            select(func.count()).select_from(LoginAttempt).where(LoginAttempt.email == email)
        ).scalar_one()


@pytest.mark.fr("FR-02")
def test_parallel_wrong_passwords_do_not_exceed_ten_checks(
    committed_email: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import create_app
    from app.modules.auth import service

    real_verify = service.verify_password
    checks = 0
    counter_lock = threading.Lock()

    def counting_verify(password: str, password_hash: str) -> bool:
        nonlocal checks
        with counter_lock:
            checks += 1
        return real_verify(password, password_hash)

    monkeypatch.setattr(service, "verify_password", counting_verify)

    workers = 25
    barrier = threading.Barrier(workers)
    statuses: list[int] = []

    app = create_app()  # настоящий get_db: у каждого запроса своя сессия и транзакция

    def worker() -> None:
        with TestClient(app) as c:
            barrier.wait(timeout=30)
            statuses.append(attempt(c, committed_email).status_code)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert len(statuses) == workers
    assert checks <= 10
    assert statuses.count(401) == 10
    assert statuses.count(429) == workers - 10
    assert _attempts_count(committed_email) == 10


# ---- Запись о неудаче переживает 401 (keep_changes) ---


class _Spy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> "_Spy":
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("close")

    def commit(self) -> None:
        self.calls.append("commit")

    def rollback(self) -> None:
        self.calls.append("rollback")


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    s = _Spy()
    monkeypatch.setattr(db_module, "SessionLocal", lambda *a, **k: s)
    monkeypatch.setattr(db_module, "get_engine", lambda: None)
    return s


@pytest.mark.fr("FR-02")
def test_app_error_keep_changes_defaults_to_false() -> None:
    assert AppError.keep_changes is False
    assert Unauthorized("x").keep_changes is False


@pytest.mark.fr("FR-02")
def test_get_db_commits_on_app_error_with_keep_changes(spy: _Spy) -> None:
    exc = Unauthorized("x")
    exc.keep_changes = True
    gen = db_module.get_db()
    next(gen)
    with pytest.raises(Unauthorized):
        gen.throw(exc)
    assert "commit" in spy.calls
    assert "rollback" not in spy.calls
    assert spy.calls[-1] == "close"


@pytest.mark.fr("FR-02")
def test_get_db_rolls_back_on_app_error_without_keep_changes(spy: _Spy) -> None:
    gen = db_module.get_db()
    next(gen)
    with pytest.raises(Unauthorized):
        gen.throw(Unauthorized("x"))
    assert "rollback" in spy.calls
    assert "commit" not in spy.calls


@pytest.mark.fr("FR-02")
def test_failed_login_survives_401_through_real_get_db(committed_email: str) -> None:
    from app.main import create_app

    app = create_app()  # без override get_db
    with TestClient(app) as c:
        assert attempt(c, committed_email).status_code == 401
    assert _attempts_count(committed_email) == 1


@pytest.mark.fr("FR-02")
def test_login_service_has_no_explicit_commit() -> None:
    from app.modules.auth import service

    assert "commit(" not in inspect.getsource(service.login)


# ---- Не больше 3 писем сброса в час на e-mail ---


@pytest.mark.fr("FR-03")
def test_fourth_reset_request_in_hour_is_202_without_new_mail(
    client: TestClient, world, db: Session
) -> None:
    with freeze_time(START) as clock:
        first = None
        for _ in range(3):
            resp = client.post(RESET, json={"email": world.user.email})
            first = first or resp
            assert resp.status_code == 202
            clock.tick(MIN)
        fourth = client.post(RESET, json={"email": world.user.email})
    assert fourth.status_code == 202
    assert fourth.content == first.content
    assert len(reset_mails(db, world.user)) == 3


@pytest.mark.fr("FR-03")
def test_reset_limit_counts_email_case_insensitively(
    client: TestClient, world, db: Session
) -> None:
    with freeze_time(START):
        for email in (world.user.email, world.user.email.upper(), world.user.email.title()):
            assert client.post(RESET, json={"email": email}).status_code == 202
        assert client.post(RESET, json={"email": world.user.email}).status_code == 202
    assert len(reset_mails(db, world.user)) == 3


@pytest.mark.fr("FR-03")
def test_reset_limit_is_per_email(client: TestClient, world, db: Session) -> None:
    from tests.factories import make_user

    other = make_user(db)
    with freeze_time(START):
        for _ in range(3):
            client.post(RESET, json={"email": world.user.email})
        assert client.post(RESET, json={"email": other.email}).status_code == 202
    assert len(reset_mails(db, other)) == 1


@pytest.mark.fr("FR-03")
def test_reset_mail_allowed_again_after_an_hour(client: TestClient, world, db: Session) -> None:
    with freeze_time(START) as clock:
        for _ in range(3):
            assert client.post(RESET, json={"email": world.user.email}).status_code == 202
        clock.tick(timedelta(hours=1, seconds=1))
        assert client.post(RESET, json={"email": world.user.email}).status_code == 202
    assert len(reset_mails(db, world.user)) >= 4


# ---- url в payload письма ---


@pytest.mark.fr("FR-03")
def test_public_base_url_default(monkeypatch: pytest.MonkeyPatch, settings_reset: None) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert get_settings().public_base_url == "http://localhost:8080"


@pytest.mark.fr("FR-03")
def test_reset_mail_payload_has_url_with_token(client: TestClient, world, db: Session) -> None:
    client.post(RESET, json={"email": world.user.email})
    payload = reset_mails(db, world.user)[0].payload
    assert payload["url"] == f"http://localhost:8080/reset-password?token={payload['token']}"


@pytest.mark.fr("FR-03")
def test_reset_mail_url_uses_public_base_url_setting(
    monkeypatch: pytest.MonkeyPatch, settings_reset: None, client: TestClient, world, db: Session
) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.org")
    get_settings.cache_clear()
    client.post(RESET, json={"email": world.user.email})
    payload = reset_mails(db, world.user)[0].payload
    assert payload["url"] == f"https://app.example.org/reset-password?token={payload['token']}"


# ---- Чистка login_attempts ---
# Попытки старше 30 минут удаляются.


@pytest.mark.fr("FR-02")
def test_purge_deletes_only_attempts_older_than_30_minutes(db: Session) -> None:
    from app.modules.auth import service

    now = datetime(2030, 1, 1, 10, 0, tzinfo=UTC)
    email = f"purge-{uuid.uuid4().hex}@example.com"
    for age in (timedelta(minutes=31), timedelta(hours=5), timedelta(minutes=29)):
        db.add(LoginAttempt(email=email, attempted_at=now - age))
    db.flush()

    removed = service.purge_login_attempts(db, now)

    left = db.scalars(select(LoginAttempt.attempted_at).where(LoginAttempt.email == email)).all()
    assert removed >= 2
    assert left == [now - timedelta(minutes=29)]


@pytest.mark.fr("FR-02")
def test_cleanup_task_is_scheduled_hourly() -> None:
    from app.worker import celery_app

    celery_app.loader.import_default_modules()
    entries = [
        e
        for e in celery_app.conf.beat_schedule.values()
        if e["task"] == "app.modules.auth.tasks.cleanup_login_attempts"
    ]
    assert len(entries) == 1
    schedule = entries[0]["schedule"]
    assert (
        schedule if isinstance(schedule, int | float) else schedule.run_every.total_seconds()
    ) == 3600
    assert "app.modules.auth.tasks.cleanup_login_attempts" in celery_app.tasks
