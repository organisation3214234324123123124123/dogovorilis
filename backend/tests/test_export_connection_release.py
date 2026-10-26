"""FR-51: потоковый экспорт на настоящем get_db не должен оставлять соединение в пуле занятым."""

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_engine
from app.main import create_app
from app.models import AnalyticsEvent

pytestmark = pytest.mark.fr("FR-51")

URL = "/api/v1/admin/export/events.csv"
TOKEN = "e" * 32
NAME = "export_probe"
COUNT = 3


@pytest.fixture
def probe_events(migrated_db: None) -> Iterator[None]:
    """События, закоммиченные по-настоящему: настоящий get_db видит их в отдельном соединении."""
    base = datetime(2030, 1, 1, tzinfo=UTC)
    with Session(get_engine()) as setup:
        for n in range(COUNT):
            setup.add(
                AnalyticsEvent(name=NAME, created_at=base + timedelta(minutes=n), props={"n": n})
            )
        setup.commit()
    try:
        yield
    finally:
        with Session(get_engine()) as cleanup:
            cleanup.execute(delete(AnalyticsEvent).where(AnalyticsEvent.name == NAME))
            cleanup.commit()


def _export(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            resp = client.get(URL, headers={"X-Admin-Token": TOKEN})
        assert resp.status_code == 200
        rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8"))))
    finally:
        get_settings.cache_clear()
    return [row for row in rows if row and row[0] == NAME]


def test_export_returns_connection_to_pool(
    monkeypatch: pytest.MonkeyPatch, probe_events: None
) -> None:
    pool: Any = get_engine().pool
    before = pool.checkedout()
    rows = _export(monkeypatch)
    assert len(rows) == COUNT
    assert pool.checkedout() == before


def test_export_releases_connection_after_last_query(
    monkeypatch: pytest.MonkeyPatch, probe_events: None
) -> None:
    engine = get_engine()
    log: list[str] = []

    def on_query(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        if "analytics_events" in statement and statement.lstrip().upper().startswith("SELECT"):
            log.append("query")

    def on_checkin(*_: Any) -> None:
        log.append("checkin")

    event.listen(engine, "before_cursor_execute", on_query)
    event.listen(engine, "checkin", on_checkin)
    try:
        rows = _export(monkeypatch)
    finally:
        event.remove(engine, "before_cursor_execute", on_query)
        event.remove(engine, "checkin", on_checkin)
    assert len(rows) == COUNT
    last_query = max(i for i, e in enumerate(log) if e == "query")
    assert "checkin" in log[last_query:]
