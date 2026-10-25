import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.security import SESSION_COOKIE, create_access_token
from app.main import create_app
from tests.factories import World

pytestmark = pytest.mark.fr("infra")

SECRET = "boom-secret-detail"
SERIES = {"title": "Планёрка", "timezone": "Europe/Moscow", "planned_duration_min": 60}


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def request_logs() -> Any:
    logger = logging.getLogger("app.request")
    collector = _Collector()
    logger.addHandler(collector)
    yield collector.records
    logger.removeHandler(collector)


class _FailingCommit:
    def __init__(self, inner: Session) -> None:
        self.inner = inner

    def __enter__(self) -> "_FailingCommit":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def commit(self) -> None:
        raise RuntimeError(SECRET)


def _assert_problem_500(response: Any) -> None:
    assert response.status_code == 500
    assert response.headers["content-type"].split(";")[0] == "application/problem+json"
    body = response.json()
    assert body["status"] == 500
    assert body["code"] == "INTERNAL_ERROR"
    assert body["title"]
    assert SECRET not in response.text
    assert "Traceback" not in response.text


def test_unhandled_exception_returns_problem_json() -> None:
    app = create_app()

    def broken() -> None:
        raise RuntimeError(SECRET)

    app.add_api_route("/api/v1/_boom", broken, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/_boom")
    _assert_problem_500(response)


def test_commit_failure_returns_problem_json_and_logs_unhandled_error(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    db: Session,
    request_logs: list[logging.LogRecord],
) -> None:
    monkeypatch.setattr(db_module, "SessionLocal", lambda *a, **k: _FailingCommit(db))
    monkeypatch.setattr(db_module, "get_engine", lambda: None)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        client.cookies.set(SESSION_COOKIE, create_access_token(world.user.id))
        response = client.post("/api/v1/series", json=SERIES)
    _assert_problem_500(response)
    unhandled = [r for r in request_logs if r.getMessage() == "unhandled error"]
    assert len(unhandled) == 1
    assert unhandled[0].path == "/api/v1/series"


def test_unhandled_error_log_masks_link_token(request_logs: list[logging.LogRecord]) -> None:
    app = create_app()

    def broken(token: str) -> None:
        raise RuntimeError(SECRET)

    app.add_api_route("/api/v1/p/{token}/_boom", broken, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/p/sometoken123/_boom")
    _assert_problem_500(response)
    unhandled = [r for r in request_logs if r.getMessage() == "unhandled error"]
    assert len(unhandled) == 1
    assert unhandled[0].path.startswith("/api/v1/p/***")
    assert "sometoken123" not in unhandled[0].path
