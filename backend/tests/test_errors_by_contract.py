import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.main import create_app

pytestmark = pytest.mark.fr("NFR-13")


class _Body(BaseModel):
    title: str = Field(min_length=1)
    count: int


@pytest.fixture
def app_client(settings_reset: None) -> TestClient:
    app = create_app()

    @app.post("/api/v1/_t/body")
    def _body(body: _Body) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def _errors(resp) -> list[dict[str, str]]:
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    return body["errors"]


def test_validation_error_lists_field_and_message(app_client: TestClient) -> None:
    errors = _errors(app_client.post("/api/v1/_t/body", json={"title": "x", "count": "abc"}))
    assert [e["field"] for e in errors] == ["count"]
    assert isinstance(errors[0]["message"], str) and errors[0]["message"]


def test_validation_error_field_has_no_body_prefix(app_client: TestClient) -> None:
    errors = _errors(app_client.post("/api/v1/_t/body", json={"title": "", "count": 1}))
    assert [e["field"] for e in errors] == ["title"]


def test_validation_error_reports_every_invalid_field(app_client: TestClient) -> None:
    errors = _errors(app_client.post("/api/v1/_t/body", json={"title": ""}))
    assert sorted(e["field"] for e in errors) == ["count", "title"]


def test_validation_error_items_have_only_field_and_message(app_client: TestClient) -> None:
    errors = _errors(app_client.post("/api/v1/_t/body", json={}))
    assert all(set(e) == {"field", "message"} for e in errors)


def test_validation_error_keeps_problem_envelope(app_client: TestClient) -> None:
    resp = app_client.post("/api/v1/_t/body", json={})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert {"type", "title", "status", "code"} <= resp.json().keys()


def test_series_create_without_title_points_at_title(authed: TestClient) -> None:
    resp = authed.post("/api/v1/series", json={"timezone": "Europe/Moscow"})
    assert "title" in [e["field"] for e in _errors(resp)]
