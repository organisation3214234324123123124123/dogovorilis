from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

from tests.conftest import PASSWORD

pytestmark = pytest.mark.fr("FR-02")

LOGIN = "/api/v1/auth/login"
START = "2030-01-01T10:00:00+00:00"
window = timedelta(minutes=15)


def attempt(client: TestClient, email: str, password: str = "wrong-pass"):
    return client.post(LOGIN, json={"email": email, "password": password})


def fail_times(client: TestClient, email: str, n: int) -> None:
    for _ in range(n):
        assert attempt(client, email).status_code == 401


def test_eleventh_attempt_with_correct_password_is_429_rate_limited(
    client: TestClient, world
) -> None:
    with freeze_time(START):
        fail_times(client, world.user.email, 10)
        resp = attempt(client, world.user.email, PASSWORD)
    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "RATE_LIMITED"


def test_lockout_response_has_retry_after_within_window(client: TestClient, world) -> None:
    with freeze_time(START):
        fail_times(client, world.user.email, 10)
        resp = attempt(client, world.user.email, PASSWORD)
    assert 1 <= int(resp.headers["Retry-After"]) <= 15 * 60


def test_still_locked_just_before_15_minutes_pass(client: TestClient, world) -> None:
    with freeze_time(START) as clock:
        fail_times(client, world.user.email, 10)
        clock.tick(window - timedelta(seconds=30))
        resp = attempt(client, world.user.email, PASSWORD)
    assert resp.status_code == 429


def test_login_works_again_15_minutes_after_lockout(client: TestClient, world) -> None:
    with freeze_time(START) as clock:
        fail_times(client, world.user.email, 10)
        assert attempt(client, world.user.email, PASSWORD).status_code == 429
        clock.tick(window + timedelta(seconds=1))
        resp = attempt(client, world.user.email, PASSWORD)
    assert resp.status_code == 200


def test_nine_failures_then_correct_password_logs_in(client: TestClient, world) -> None:
    with freeze_time(START):
        fail_times(client, world.user.email, 9)
        resp = attempt(client, world.user.email, PASSWORD)
    assert resp.status_code == 200


def test_failures_spread_over_more_than_window_do_not_lock(client: TestClient, world) -> None:
    with freeze_time(START) as clock:
        fail_times(client, world.user.email, 9)
        clock.tick(window + timedelta(minutes=1))
        fail_times(client, world.user.email, 9)
        resp = attempt(client, world.user.email, PASSWORD)
    assert resp.status_code == 200


def test_window_is_sliding_ten_failures_across_the_window_lock(client: TestClient, world) -> None:
    with freeze_time(START) as clock:
        fail_times(client, world.user.email, 5)
        clock.tick(timedelta(minutes=10))
        fail_times(client, world.user.email, 5)
        resp = attempt(client, world.user.email, PASSWORD)
    assert resp.status_code == 429


def test_lockout_of_one_email_does_not_block_another(client: TestClient, world, db) -> None:
    from app.core.security import hash_password
    from tests.factories import make_user

    other = make_user(db, password_hash=hash_password(PASSWORD))
    with freeze_time(START):
        fail_times(client, world.user.email, 10)
        locked = attempt(client, world.user.email, PASSWORD)
        free = attempt(client, other.email, PASSWORD)
    assert locked.status_code == 429
    assert free.status_code == 200


def test_unknown_email_is_counted_and_locked_too(client: TestClient, world) -> None:
    ghost = "ghost@example.com"
    with freeze_time(START):
        fail_times(client, ghost, 10)
        resp = attempt(client, ghost)
    assert resp.status_code == 429
    assert resp.json()["code"] == "RATE_LIMITED"


def test_email_case_does_not_bypass_lockout(client: TestClient, world) -> None:
    with freeze_time(START):
        fail_times(client, world.user.email, 10)
        resp = attempt(client, world.user.email.upper(), PASSWORD)
    assert resp.status_code == 429
