import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

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


def test_login_sets_httponly_cookie_and_returns_token(client: TestClient, world) -> None:
    resp = client.post("/api/v1/auth/login", json={"email": world.user.email, "password": PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["user"]["email"] == world.user.email
    cookie = resp.headers["set-cookie"].lower()
    assert "access_token=" in cookie
    assert "httponly" in cookie


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


def test_me_with_bearer_token(client: TestClient, world) -> None:
    token = client.post(
        "/api/v1/auth/login", json={"email": world.user.email, "password": PASSWORD}
    ).json()["access_token"]
    client.cookies.clear()
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_me_without_cookie_is_401(client: TestClient) -> None:
    assert_problem(client.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")


def test_me_with_garbage_cookie_is_401(client: TestClient) -> None:
    client.cookies.set("access_token", "garbage")
    assert_problem(client.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")


def test_logout_clears_cookie(client: TestClient, world) -> None:
    client.post("/api/v1/auth/login", json={"email": world.user.email, "password": PASSWORD})
    resp = client.post("/api/v1/auth/logout")
    assert resp.status_code == 204
    assert "access_token=" in resp.headers["set-cookie"]
    assert_problem(client.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")
