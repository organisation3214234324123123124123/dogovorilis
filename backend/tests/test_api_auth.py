from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models import User
from tests.conftest import PASSWORD, login_as

pytestmark = pytest.mark.fr("FR-01")

REG = {
    "email": "new@example.com",
    "password": "password1",
    "full_name": "New Host",
    "pd_consent": True,
}


def assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == code
    assert body["status"] == status
    return body


def test_register_returns_201_and_stores_consent(client: TestClient, db: Session) -> None:
    resp = client.post("/api/v1/auth/register", json=REG)
    assert resp.status_code == 201
    user = db.scalars(select(User).where(User.email == "new@example.com")).one()
    assert user.pd_consent_at is not None
    assert user.password_hash != REG["password"]


def test_register_duplicate_email_any_case_is_409(client: TestClient) -> None:
    client.post("/api/v1/auth/register", json=REG)
    resp = client.post("/api/v1/auth/register", json={**REG, "email": "NEW@Example.com"})
    assert_problem(resp, 409, "EMAIL_TAKEN")


def test_register_without_consent_is_422(client: TestClient, db: Session) -> None:
    resp = client.post("/api/v1/auth/register", json={**REG, "pd_consent": False})
    assert_problem(resp, 422, "VALIDATION_ERROR")
    assert db.scalars(select(User).where(User.email == "new@example.com")).first() is None


def test_register_missing_consent_field_is_422(client: TestClient) -> None:
    body = {k: v for k, v in REG.items() if k != "pd_consent"}
    assert_problem(client.post("/api/v1/auth/register", json=body), 422, "VALIDATION_ERROR")


def login(client: TestClient, world):
    return client.post("/api/v1/auth/login", json={"email": world.user.email, "password": PASSWORD})


def cookie_attrs(resp, name: str) -> str:
    for line in resp.headers.get_list("set-cookie"):
        if line.lower().startswith(f"{name}="):
            return line.lower()
    return ""


V2_TOKEN_KEYS = {"access_token", "token", "token_type", "jwt", "session"}


def test_login_body_is_user_per_v2_schema(client: TestClient, world) -> None:
    resp = login(client, world)
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == world.user.email
    assert body["id"] == str(world.user.id)


def test_login_body_has_no_token_anywhere(client: TestClient, world) -> None:
    resp = login(client, world)
    token = create_access_token(world.user.id)
    assert not V2_TOKEN_KEYS & set(resp.json())
    assert token.split(".")[0] not in resp.text
    jwt_cookie = cookie_attrs(resp, "session").split("=", 1)[-1].split(";")[0]
    assert jwt_cookie
    assert jwt_cookie not in resp.text


def test_login_sets_httponly_session_cookie(client: TestClient, world) -> None:
    cookie = cookie_attrs(login(client, world), "session")
    assert cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "max-age=604800" in cookie


def test_login_does_not_set_access_token_cookie(client: TestClient, world) -> None:
    assert cookie_attrs(login(client, world), "access_token") == ""


def test_login_session_cookie_is_secure_outside_dev(
    client: TestClient, world, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    get_settings.cache_clear()
    try:
        cookie = cookie_attrs(login(client, world), "session")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert "secure" in cookie


def test_login_session_cookie_is_secure_with_dev_version_but_no_flag(
    client: TestClient, world, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_VERSION", "dev")
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    get_settings.cache_clear()
    try:
        cookie = cookie_attrs(login(client, world), "session")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert "secure" in cookie


def test_login_session_cookie_is_not_secure_in_dev_mode(client: TestClient, world) -> None:
    assert get_settings().is_dev
    assert "secure" not in cookie_attrs(login(client, world), "session")


def test_login_then_me_works_via_session_cookie(client: TestClient, world) -> None:
    login(client, world)
    assert "session" in client.cookies
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == world.user.email


def test_session_cookie_authenticates(client: TestClient, world) -> None:
    client.cookies.set("session", create_access_token(world.user.id))
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == world.user.email


def test_old_access_token_cookie_does_not_authenticate(client: TestClient, world) -> None:
    client.cookies.set("access_token", create_access_token(world.user.id))
    assert_problem(client.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")


def test_old_access_token_cookie_does_not_open_protected_endpoint(
    client: TestClient, world
) -> None:
    client.cookies.set("access_token", create_access_token(world.user.id))
    assert_problem(client.get("/api/v1/series"), 401, "UNAUTHORIZED")


def test_session_cookie_opens_protected_endpoint(client: TestClient, world) -> None:
    client.cookies.set("session", create_access_token(world.user.id))
    assert client.get("/api/v1/series").status_code == 200


def test_login_wrong_password_is_401(client: TestClient, world) -> None:
    resp = client.post("/api/v1/auth/login", json={"email": world.user.email, "password": "bad"})
    assert_problem(resp, 401, "UNAUTHORIZED")
    assert "set-cookie" not in resp.headers


def test_login_unknown_email_is_401(client: TestClient, world) -> None:
    resp = client.post("/api/v1/auth/login", json={"email": "no@example.com", "password": "x"})
    assert_problem(resp, 401, "UNAUTHORIZED")


def test_token_form_login(client: TestClient, world) -> None:
    resp = client.post(
        "/api/v1/auth/token", data={"username": world.user.email, "password": PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_token_form_wrong_password_is_401(client: TestClient, world) -> None:
    resp = client.post("/api/v1/auth/token", data={"username": world.user.email, "password": "bad"})
    assert_problem(resp, 401, "UNAUTHORIZED")


def test_me_with_cookie(client: TestClient, world) -> None:
    login_as(client, world.user)
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == world.user.email


def test_me_with_bearer_token_from_swagger_endpoint(client: TestClient, world) -> None:
    token = client.post(
        "/api/v1/auth/token", data={"username": world.user.email, "password": PASSWORD}
    ).json()["access_token"]
    client.cookies.clear()
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_me_without_cookie_is_401(client: TestClient) -> None:
    assert_problem(client.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")


def test_me_with_garbage_cookie_is_401(client: TestClient) -> None:
    client.cookies.set("session", "garbage")
    assert_problem(client.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")


def test_logout_clears_session_cookie(client: TestClient, world) -> None:
    login(client, world)
    resp = client.post("/api/v1/auth/logout")
    assert resp.status_code == 204
    cookie = cookie_attrs(resp, "session")
    assert cookie
    assert "max-age=0" in cookie or "expires=" in cookie
    assert_problem(client.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")


@pytest.fixture
def non_dev(monkeypatch: pytest.MonkeyPatch, settings_reset: None) -> Iterator[None]:
    # Боевой режим: DEV_MODE выключен, даже если APP_VERSION остался "dev" (образ без версии).
    monkeypatch.setenv("APP_VERSION", "dev")
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def test_token_form_is_404_outside_dev(non_dev: None, client: TestClient, world) -> None:
    resp = client.post(
        "/api/v1/auth/token", data={"username": world.user.email, "password": PASSWORD}
    )
    assert_problem(resp, 404, "NOT_FOUND")


def test_token_form_sets_no_cookie_outside_dev(non_dev: None, client: TestClient, world) -> None:
    resp = client.post(
        "/api/v1/auth/token", data={"username": world.user.email, "password": PASSWORD}
    )
    assert cookie_attrs(resp, "session") == ""


def test_bearer_without_cookie_is_401_outside_dev(non_dev: None, client: TestClient, world) -> None:
    token = create_access_token(world.user.id)
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert_problem(resp, 401, "UNAUTHORIZED")


def test_bearer_does_not_open_protected_endpoint_outside_dev(
    non_dev: None, client: TestClient, world
) -> None:
    token = create_access_token(world.user.id)
    resp = client.get("/api/v1/series", headers={"Authorization": f"Bearer {token}"})
    assert_problem(resp, 401, "UNAUTHORIZED")


def test_session_cookie_still_works_outside_dev(non_dev: None, client: TestClient, world) -> None:
    client.cookies.set("session", create_access_token(world.user.id))
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == world.user.email


def test_login_sets_session_cookie_outside_dev(non_dev: None, client: TestClient, world) -> None:
    resp = login(client, world)
    assert resp.status_code == 200
    assert cookie_attrs(resp, "session")


def test_swagger_docs_open_outside_dev(non_dev: None, client: TestClient) -> None:
    assert client.get("/api/docs").status_code == 200


def test_bearer_without_cookie_works_in_dev(client: TestClient, world) -> None:
    assert get_settings().is_dev
    token = create_access_token(world.user.id)
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
