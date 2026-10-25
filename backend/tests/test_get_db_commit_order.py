import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.db import SessionDep
from app.core.security import SESSION_COOKIE, create_access_token
from app.main import create_app
from tests.factories import World

pytestmark = pytest.mark.fr("infra")

SERIES = {"title": "Планёрка", "timezone": "Europe/Moscow", "planned_duration_min": 60}


class RecordingSession:
    def __init__(self, events: list[str], fail_commit: bool, inner: Session | None = None) -> None:
        self.events = events
        self.fail_commit = fail_commit
        self.inner = inner

    def __enter__(self) -> "RecordingSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.events.append("close")

    def __getattr__(self, name: str) -> Any:
        if self.inner is None:
            raise AttributeError(name)
        return getattr(self.inner, name)

    def commit(self) -> None:
        if self.fail_commit:
            self.events.append("commit_failed")
            raise RuntimeError("commit failed")
        self.events.append("commit")
        if self.inner is not None:
            self.inner.commit()

    def rollback(self) -> None:
        self.events.append("rollback")
        if self.inner is not None:
            self.inner.rollback()


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/things", status_code=201)
    def create_thing(db: SessionDep) -> dict[str, bool]:
        return {"ok": True}

    return app


def _patch_session(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    fail_commit: bool = False,
    inner: Session | None = None,
) -> None:
    session = RecordingSession(events, fail_commit, inner)
    monkeypatch.setattr(db_module, "SessionLocal", lambda *a, **k: session)
    monkeypatch.setattr(db_module, "get_engine", lambda: None)


async def _post(
    app: Any, path: str, events: list[str], body: dict[str, Any], cookie: str = ""
) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json")]
    if cookie:
        headers.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            events.append("response_start")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "server": ("test", 80),
        "client": ("test", 1),
    }
    await app(scope, receive, send)


def test_commit_happens_before_response_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    _patch_session(monkeypatch, events)
    asyncio.run(_post(_app(), "/things", events, {}))
    assert events.index("commit") < events.index("response_start")


def test_failed_commit_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    _patch_session(monkeypatch, events, fail_commit=True)
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.post("/things")
    assert response.status_code >= 500


def test_real_app_commits_series_before_response_is_sent(
    monkeypatch: pytest.MonkeyPatch, world: World, db: Session
) -> None:
    events: list[str] = []
    _patch_session(monkeypatch, events, inner=db)
    token = create_access_token(world.user.id)
    asyncio.run(_post(create_app(), "/api/v1/series", events, SERIES, cookie=token))
    assert "commit" in events
    assert events.index("commit") < events.index("response_start")


def test_real_app_failed_commit_on_series_is_not_201(
    monkeypatch: pytest.MonkeyPatch, world: World, db: Session
) -> None:
    events: list[str] = []
    _patch_session(monkeypatch, events, inner=db)
    monkeypatch.setattr(RecordingSession, "commit", _fail_on_write(events), raising=True)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        client.cookies.set(SESSION_COOKIE, create_access_token(world.user.id))
        response = client.post("/api/v1/series", json=SERIES)
    assert response.status_code >= 500


def _fail_on_write(events: list[str]) -> Any:
    def commit(self: RecordingSession) -> None:
        events.append("commit_failed")
        raise RuntimeError("commit failed")

    return commit
