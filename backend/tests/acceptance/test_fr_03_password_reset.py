import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.notifications.models import Notification, NotificationType
from tests.conftest import PASSWORD

pytestmark = pytest.mark.fr("FR-03")

RESET = "/api/v1/auth/password-reset"
CONFIRM = "/api/v1/auth/password-reset/confirm"
LOGIN = "/api/v1/auth/login"
NEW_PASSWORD = "brand-new-pass-1"
START = "2030-01-01T10:00:00+00:00"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def reset_mails(db: Session, user) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification).where(
                Notification.type == NotificationType.PASSWORD_RESET,
                Notification.recipient_user_id == user.id,
            )
        )
    )


def token_from(mail: Notification) -> str:
    found = TOKEN_RE.findall(str(mail.payload))
    assert found, "в payload письма нет токена сброса"
    return found[-1]


def request_token(client: TestClient, db: Session, user) -> str:
    assert client.post(RESET, json={"email": user.email}).status_code == 202
    return token_from(reset_mails(db, user)[-1])


def confirm(client: TestClient, token: str, password: str = NEW_PASSWORD):
    return client.post(CONFIRM, json={"token": token, "new_password": password})


def login(client: TestClient, email: str, password: str):
    return client.post(LOGIN, json={"email": email, "password": password})


def test_reset_request_queues_password_reset_mail_for_host(
    client: TestClient, world, db: Session
) -> None:
    resp = client.post(RESET, json={"email": world.user.email})
    assert resp.status_code == 202
    assert len(reset_mails(db, world.user)) == 1


def test_reset_mail_carries_a_token_not_equal_to_stored_hash(
    client: TestClient, world, db: Session
) -> None:
    client.post(RESET, json={"email": world.user.email})
    token = token_from(reset_mails(db, world.user)[0])
    assert len(token) == 43
    from sqlalchemy import text

    hashes = db.execute(text("SELECT token_hash FROM password_reset_tokens")).scalars().all()
    assert hashes
    assert token not in hashes


def test_reset_request_email_is_case_insensitive(client: TestClient, world, db: Session) -> None:
    client.post(RESET, json={"email": world.user.email.upper()})
    assert len(reset_mails(db, world.user)) == 1


def test_each_reset_request_creates_new_mail_with_distinct_token(
    client: TestClient, world, db: Session
) -> None:
    first = request_token(client, db, world.user)
    second = request_token(client, db, world.user)
    assert first != second
    assert len(reset_mails(db, world.user)) == 2


def test_unknown_email_gets_same_response_as_known(client: TestClient, world) -> None:
    known = client.post(RESET, json={"email": world.user.email})
    unknown = client.post(RESET, json={"email": "ghost@example.com"})
    assert known.status_code == 202
    assert unknown.status_code == known.status_code
    assert unknown.content == known.content


def test_unknown_email_response_is_202_and_sends_no_mail(
    client: TestClient, world, db: Session
) -> None:
    resp = client.post(RESET, json={"email": "ghost@example.com"})
    assert resp.status_code == 202
    assert (
        db.scalars(
            select(Notification).where(Notification.type == NotificationType.PASSWORD_RESET)
        ).first()
        is None
    )


def test_confirm_changes_password_new_works_old_is_401(
    client: TestClient, world, db: Session
) -> None:
    token = request_token(client, db, world.user)
    assert confirm(client, token).status_code == 204
    assert login(client, world.user.email, NEW_PASSWORD).status_code == 200
    assert login(client, world.user.email, PASSWORD).status_code == 401


def test_confirm_stores_new_password_as_argon2id_hash(
    client: TestClient, world, db: Session
) -> None:
    token = request_token(client, db, world.user)
    assert confirm(client, token).status_code == 204
    db.refresh(world.user)
    assert world.user.password_hash.startswith("$argon2id$")
    assert NEW_PASSWORD not in world.user.password_hash


def test_token_is_single_use(client: TestClient, world, db: Session) -> None:
    token = request_token(client, db, world.user)
    assert confirm(client, token).status_code == 204
    again = confirm(client, token, "another-pass-2")
    assert again.status_code == 410
    assert again.json()["code"] == "LINK_EXPIRED"
    assert again.headers["content-type"].startswith("application/problem+json")


def test_reused_token_does_not_change_password_again(
    client: TestClient, world, db: Session
) -> None:
    token = request_token(client, db, world.user)
    assert confirm(client, token).status_code == 204
    assert confirm(client, token, "another-pass-2").status_code == 410
    assert login(client, world.user.email, NEW_PASSWORD).status_code == 200
    assert login(client, world.user.email, "another-pass-2").status_code == 401


def test_token_valid_just_before_one_hour(client: TestClient, world, db: Session) -> None:
    with freeze_time(START) as clock:
        token = request_token(client, db, world.user)
        clock.tick(timedelta(minutes=59, seconds=30))
        resp = confirm(client, token)
    assert resp.status_code == 204


def test_token_older_than_one_hour_is_410_link_expired(
    client: TestClient, world, db: Session
) -> None:
    with freeze_time(START) as clock:
        token = request_token(client, db, world.user)
        clock.tick(timedelta(hours=1, seconds=1))
        resp = confirm(client, token)
    assert resp.status_code == 410
    assert resp.json()["code"] == "LINK_EXPIRED"


def test_expired_token_does_not_change_password(client: TestClient, world, db: Session) -> None:
    with freeze_time(START) as clock:
        token = request_token(client, db, world.user)
        clock.tick(timedelta(hours=2))
        assert confirm(client, token).status_code == 410
    assert login(client, world.user.email, PASSWORD).status_code == 200
    assert login(client, world.user.email, NEW_PASSWORD).status_code == 401


def test_unknown_token_is_410_link_expired(client: TestClient, world) -> None:
    resp = confirm(client, "A" * 43)
    assert resp.status_code == 410
    assert resp.json()["code"] == "LINK_EXPIRED"


def test_token_is_not_stored_in_plaintext_in_users_table(
    client: TestClient, world, db: Session
) -> None:
    from sqlalchemy import text

    token = request_token(client, db, world.user)
    dump = db.execute(text("SELECT row_to_json(u)::text FROM users u")).scalars().all()
    assert not any(token in row for row in dump)


def test_token_and_password_are_not_logged(
    client: TestClient, world, db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    token = request_token(client, db, world.user)
    assert confirm(client, token).status_code == 204
    assert token not in caplog.text
    assert NEW_PASSWORD not in caplog.text


def test_confirm_rejects_too_short_new_password(client: TestClient, world) -> None:
    resp = confirm(client, "A" * 43, "short")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
