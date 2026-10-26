from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.modules.series import service as series_service
from tests.conftest import login_as
from tests.factories import World, events_named

pytestmark = pytest.mark.fr("FR-50")


def test_analytics_event_is_rolled_back_with_failed_action(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-50 п.2: действие упало после записи события, значит события не остаётся."""
    db: Session = world.db

    def get_db_like_prod() -> Iterator[Session]:
        savepoint = db.begin_nested()
        try:
            yield db
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    def boom(*args, **kwargs):
        raise RuntimeError("fails after the event is tracked")

    client.app.dependency_overrides[get_db] = get_db_like_prod
    monkeypatch.setattr(series_service, "_detail", boom)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    login_as(quiet, world.user)
    resp = quiet.post(f"/api/v1/series/{world.series.id}/meetings", json={})
    assert resp.status_code == 500
    assert events_named(db, "meeting_started", series_id=world.series.id) == []
