import pytest
from fastapi.testclient import TestClient

from tests.factories import World, events_named, make_series, make_user

pytestmark = pytest.mark.fr("FR-04")

BODY = {"title": "Планёрка", "timezone": "Europe/Moscow", "planned_duration_min": 60}


def test_create_defaults(authed: TestClient) -> None:
    resp = authed.post("/api/v1/series", json=BODY)
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "active"
    assert data["capture_mode"] == "manual"
    assert data["planned_duration_min"] == 60


@pytest.mark.parametrize("minutes", [15, 240])
def test_duration_bounds_accepted(authed: TestClient, minutes: int) -> None:
    resp = authed.post("/api/v1/series", json={**BODY, "planned_duration_min": minutes})
    assert resp.status_code == 201


@pytest.mark.parametrize("minutes", [14, 241])
def test_duration_out_of_bounds_rejected(authed: TestClient, minutes: int) -> None:
    resp = authed.post("/api/v1/series", json={**BODY, "planned_duration_min": minutes})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("title", ["", "x" * 201])
def test_bad_title_rejected(authed: TestClient, title: str) -> None:
    resp = authed.post("/api/v1/series", json={**BODY, "title": title})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_unknown_timezone_rejected(authed: TestClient) -> None:
    resp = authed.post("/api/v1/series", json={**BODY, "timezone": "Mars/Base"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_audio_capture_mode_rejected_before_stage_3(authed: TestClient) -> None:
    resp = authed.post("/api/v1/series", json={**BODY, "capture_mode": "audio"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_list_returns_only_own_series(authed: TestClient, world: World) -> None:
    stranger = make_user(world.db)
    foreign = make_series(world.db, stranger)
    resp = authed.get("/api/v1/series")
    assert resp.status_code == 200
    found = {s["id"] for s in resp.json()}
    assert str(world.series.id) in found
    assert str(foreign.id) not in found


def test_get_foreign_series_is_404(authed: TestClient, world: World) -> None:
    foreign = make_series(world.db, make_user(world.db))
    resp = authed.get(f"/api/v1/series/{foreign.id}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_patch_foreign_series_is_404(authed: TestClient, world: World) -> None:
    foreign = make_series(world.db, make_user(world.db))
    resp = authed.patch(f"/api/v1/series/{foreign.id}", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_patch_changes_fields(authed: TestClient, world: World) -> None:
    resp = authed.patch(
        f"/api/v1/series/{world.series.id}",
        json={"title": "Новое", "timezone": "Asia/Yekaterinburg", "planned_duration_min": 45},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert (data["title"], data["timezone"], data["planned_duration_min"]) == (
        "Новое",
        "Asia/Yekaterinburg",
        45,
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"planned_duration_min": 14},
        {"planned_duration_min": 241},
        {"title": ""},
        {"title": "x" * 201},
        {"timezone": "Mars/Base"},
    ],
    ids=str,
)
def test_patch_validation(authed: TestClient, world: World, patch: dict) -> None:
    resp = authed.patch(f"/api/v1/series/{world.series.id}", json=patch)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_series_created_event(authed: TestClient, world: World) -> None:
    members = [
        {"full_name": "Свой", "email": "own@example.com", "organization_name": world.org.name},
        {"full_name": "Чужой", "email": "ext@example.org", "organization_name": "Внешние"},
    ]
    resp = authed.post("/api/v1/series", json={**BODY, "members": members})
    assert resp.status_code == 201
    events = events_named(world.db, "series_created", series_id=resp.json()["id"])
    assert len(events) == 1
    assert events[0].props["members_count"] == 2
    assert events[0].props["has_external"] is True
