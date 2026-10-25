import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.fr("NFR-13")

TOKEN = "test-link-token-xxxxxxxxxxxxxxxxxxxxxxxxxxx"
ITEM_ID = str(uuid.uuid4())
CLIENT_LOGGERS = ("httpx", "httpcore")


def _server_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        r.getMessage() + " " + str(getattr(r, "path", ""))
        for r in caplog.records
        if r.name.split(".")[0] not in CLIENT_LOGGERS
    )


def _paths(caplog: pytest.LogCaptureFixture, message: str) -> list[str]:
    return [
        r.path
        for r in caplog.records
        if r.name == "app.request" and r.getMessage() == message and hasattr(r, "path")
    ]


def test_request_log_masks_token_on_page_get(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        client.get(f"/api/v1/p/{TOKEN}")
    assert _paths(caplog, "request") == ["/api/v1/p/***"]
    assert TOKEN not in _server_text(caplog)


def test_request_log_masks_token_and_keeps_rest_of_path_on_accept(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        client.post(f"/api/v1/p/{TOKEN}/items/{ITEM_ID}/accept")
    assert _paths(caplog, "request") == [f"/api/v1/p/***/items/{ITEM_ID}/accept"]
    assert TOKEN not in _server_text(caplog)


def test_unhandled_error_log_masks_token(
    caplog: pytest.LogCaptureFixture, client: TestClient
) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]

    @app.get("/api/v1/p/{token}/boom")
    def boom(token: str) -> None:
        raise RuntimeError("boom")

    quiet = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="app.request"):
        quiet.get(f"/api/v1/p/{TOKEN}/boom")
    assert _paths(caplog, "unhandled error") == ["/api/v1/p/***/boom"]
    assert TOKEN not in _server_text(caplog)


def test_request_log_keeps_other_paths_intact(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    series_id = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger="app.request"):
        client.get(f"/api/v1/series/{series_id}")
    assert _paths(caplog, "request") == [f"/api/v1/series/{series_id}"]
