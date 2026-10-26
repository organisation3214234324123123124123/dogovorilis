import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.models import AnalyticsEvent
from tests.conftest import login_as
from tests.factories import World

pytestmark = pytest.mark.fr("FR-51")

URL = "/api/v1/admin/export/events.csv"
TOKEN = "e" * 32
HEADER = ["name", "created_at", "user_id", "series_id", "meeting_id", "item_id", "props"]


@pytest.fixture
def admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    get_settings.cache_clear()
    return {"X-Admin-Token": TOKEN}


def add_event(w: World, name: str, at: datetime, props: dict | None = None, **ids) -> None:
    w.db.add(AnalyticsEvent(name=name, created_at=at, props=props or {}, **ids))
    w.db.flush()


def fetch(client: TestClient, headers: dict[str, str], **params) -> list[list[str]]:
    resp = client.get(URL, headers=headers, params=params)
    assert resp.status_code == 200
    return list(csv.reader(io.StringIO(resp.content.decode("utf-8"))))


def test_without_token_is_admin_forbidden(client: TestClient, admin) -> None:
    resp = client.get(URL)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_wrong_token_is_admin_forbidden(client: TestClient, admin) -> None:
    resp = client.get(URL, headers={"X-Admin-Token": "z" * 32})
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_host_session_is_admin_forbidden(client: TestClient, world: World, admin) -> None:
    login_as(client, world.user)
    resp = client.get(URL)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_empty_admin_token_is_admin_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "")
    get_settings.cache_clear()
    resp = client.get(URL, headers={"X-Admin-Token": TOKEN})
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_DISABLED"


def test_content_type_is_csv_utf8(client: TestClient, admin) -> None:
    resp = client.get(URL, headers=admin)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    resp.content.decode("utf-8")


def test_header_row_is_exact(client: TestClient, admin) -> None:
    resp = client.get(URL, headers=admin)
    assert resp.content.decode("utf-8").splitlines()[0] == ",".join(HEADER)


def test_rows_carry_ids_and_props_as_json(client: TestClient, world: World, admin) -> None:
    item = world.item()
    at = datetime(2030, 1, 5, 9, 30, tzinfo=UTC)
    add_event(
        world,
        "item_done",
        at,
        {"on_time": True, "by": "host"},
        user_id=world.user.id,
        series_id=world.series.id,
        meeting_id=world.meeting.id,
        item_id=item.id,
    )
    rows = [r for r in fetch(client, admin) if r[0] == "item_done"]
    assert len(rows) == 1
    _, created, user_id, series_id, meeting_id, item_id, props = rows[0]
    assert datetime.fromisoformat(created) == at
    assert (user_id, series_id, meeting_id, item_id) == (
        str(world.user.id),
        str(world.series.id),
        str(world.meeting.id),
        str(item.id),
    )
    assert json.loads(props) == {"on_time": True, "by": "host"}


def test_missing_ids_are_empty_cells(client: TestClient, world: World, admin) -> None:
    add_event(world, "reminder_sent", datetime(2030, 1, 5, tzinfo=UTC))
    row = next(r for r in fetch(client, admin) if r[0] == "reminder_sent")
    assert row[2:6] == ["", "", "", ""]
    assert json.loads(row[6]) == {}


def test_date_filter_is_inclusive_on_both_ends(client: TestClient, world: World, admin) -> None:
    for day in (9, 10, 11, 12):
        add_event(world, f"day_{day}", datetime(2030, 1, day, 23, 59, tzinfo=UTC))
    add_event(world, "day_13", datetime(2030, 1, 13, 0, 0, tzinfo=UTC))
    rows = fetch(client, admin, **{"from": "2030-01-10", "to": "2030-01-12"})
    assert sorted(r[0] for r in rows[1:]) == ["day_10", "day_11", "day_12"]


def test_date_from_only_and_date_to_only(client: TestClient, world: World, admin) -> None:
    add_event(world, "early", datetime(2030, 1, 1, tzinfo=UTC))
    add_event(world, "late", datetime(2030, 1, 20, tzinfo=UTC))
    assert [r[0] for r in fetch(client, admin, **{"from": "2030-01-10"})[1:]] == ["late"]
    assert [r[0] for r in fetch(client, admin, to="2030-01-10")[1:]] == ["early"]


def test_rows_are_ordered_by_time(client: TestClient, world: World, admin) -> None:
    add_event(world, "second", datetime(2030, 1, 2, tzinfo=UTC))
    add_event(world, "first", datetime(2030, 1, 1, tzinfo=UTC))
    assert [r[0] for r in fetch(client, admin)[1:]] == ["first", "second"]


def test_invalid_date_is_422(client: TestClient, admin) -> None:
    resp = client.get(URL, headers=admin, params={"from": "not-a-date"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_export_has_no_item_text_or_emails(client: TestClient, world: World, admin) -> None:
    item = world.item(raw_text="SECRET-DECISION-TEXT", what="SECRET-WHAT")
    add_event(
        world,
        "item_done",
        datetime(2030, 1, 5, tzinfo=UTC),
        {"on_time": True, "by": "host"},
        user_id=world.user.id,
        item_id=item.id,
    )
    resp = client.get(URL, headers=admin)
    assert resp.status_code == 200
    body = resp.content.decode("utf-8")
    for secret in ("SECRET-DECISION-TEXT", "SECRET-WHAT", world.user.email, world.person.email):
        assert secret not in body


def test_export_is_streamed_without_content_length(client: TestClient, world: World, admin) -> None:
    add_event(world, "reminder_sent", datetime(2030, 1, 5, tzinfo=UTC))
    resp = client.get(URL, headers=admin)
    assert resp.status_code == 200
    assert "content-length" not in resp.headers


def test_service_returns_lazy_chunks_not_one_string(world: World) -> None:
    from collections.abc import Iterator

    from app.modules.analytics.schemas import EventsExportQuery
    from app.modules.analytics.service import export_events_csv

    for i in range(5):
        add_event(world, f"e{i}", datetime(2030, 1, 5, i, tzinfo=UTC))
    chunks = export_events_csv(world.db, EventsExportQuery())
    assert isinstance(chunks, Iterator)
    parts = list(chunks)
    assert len(parts) >= 6  # заголовок и по куску на строку, а не один блок
    rows = list(csv.reader(io.StringIO("".join(parts))))
    assert rows[0] == HEADER
    assert [r[0] for r in rows[1:]] == [f"e{i}" for i in range(5)]


def test_large_export_is_complete_and_valid_csv(client: TestClient, world: World, admin) -> None:
    from sqlalchemy import insert

    rows = [
        {
            "name": "bulk",
            "created_at": datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=i),
            "props": {"n": i, "note": 'a,"b"'},
        }
        for i in range(2500)
    ]
    world.db.execute(insert(AnalyticsEvent), rows)
    out = fetch(client, admin)
    assert out[0] == HEADER
    assert len(out) == 2501
    assert all(len(r) == 7 for r in out)
    assert json.loads(out[1][6]) == {"n": 0, "note": 'a,"b"'}
    assert json.loads(out[-1][6])["n"] == 2499
