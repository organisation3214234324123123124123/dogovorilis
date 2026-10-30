import pytest
from fastapi.testclient import TestClient

from app.core.errors import Conflict, NotFound
from app.main import create_app

pytestmark = pytest.mark.fr("infra")

PROBLEM = "application/problem+json"


@pytest.fixture
def app_client(settings_reset: None) -> TestClient:
    app = create_app()

    @app.get("/_t/not-found")
    def _nf() -> None:
        raise NotFound("x")

    @app.get("/_t/conflict")
    def _cf() -> None:
        raise Conflict("dup")

    @app.get("/_t/int")
    def _int(n: int) -> dict[str, int]:
        return {"n": n}

    return TestClient(app)


def test_unknown_route_is_problem(client: TestClient) -> None:
    r = client.get("/api/v1/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(PROBLEM)
    body = r.json()
    assert {"type", "title", "status", "detail"} <= body.keys()
    assert body["status"] == 404


def test_not_found_error(app_client: TestClient) -> None:
    r = app_client.get("/_t/not-found")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(PROBLEM)
    assert r.json()["detail"] == "x"


def test_conflict_error(app_client: TestClient) -> None:
    r = app_client.get("/_t/conflict")
    assert r.status_code == 409
    assert r.headers["content-type"].startswith(PROBLEM)
    assert r.json()["title"] == "Conflict"


def test_validation_error_is_422_problem(app_client: TestClient) -> None:
    r = app_client.get("/_t/int", params={"n": "abc"})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith(PROBLEM)
    assert r.json()["status"] == 422


def test_error_body_has_no_instance(client: TestClient) -> None:
    assert "instance" not in client.get("/api/v1/nope").json()


def test_link_error_body_does_not_leak_token(client: TestClient) -> None:
    token = "leaktoken" + "x" * 30
    r = client.get(f"/api/v1/p/{token}")
    assert r.status_code >= 400
    assert token not in r.text


@pytest.mark.parametrize("suffix", ["items/00000000-0000-0000-0000-000000000000/accept", "nope"])
def test_link_post_error_body_does_not_leak_token(client: TestClient, suffix: str) -> None:
    token = "leaktoken" + "y" * 30
    r = client.post(f"/api/v1/p/{token}/{suffix}")
    assert r.status_code >= 400
    assert token not in r.text
