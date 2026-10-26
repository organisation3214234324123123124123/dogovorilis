import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login_as
from tests.factories import World, make_user

pytestmark = pytest.mark.fr("FR-04")


@pytest.fixture
def intruder_client(client: TestClient, world: World) -> TestClient:
    login_as(client, make_user(world.db))
    return client


def paths(w: World) -> list[str]:
    item = w.item()
    return [
        f"/api/v1/series/{w.series.id}",
        f"/api/v1/series/{w.series.id}/members",
        f"/api/v1/series/{w.series.id}/meetings",
        f"/api/v1/series/{w.series.id}/items",
        f"/api/v1/meetings/{w.meeting.id}",
        f"/api/v1/items/{item.id}",
        f"/api/v1/items/{item.id}/history",
    ]


def test_foreign_objects_are_404_not_found(intruder_client: TestClient, world: World) -> None:
    for path in paths(world):
        resp = intruder_client.get(path)
        assert resp.status_code == 404, path
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["code"] == "NOT_FOUND"


def test_foreign_series_absent_from_list(intruder_client: TestClient, world: World) -> None:
    resp = intruder_client.get("/api/v1/series")
    assert resp.status_code == 200
    assert resp.json() == []


def test_unknown_id_is_404(authed: TestClient) -> None:
    resp = authed.get(f"/api/v1/series/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_anonymous_gets_401_problem_with_code(client: TestClient, world: World) -> None:
    for path in ["/api/v1/series", *paths(world)]:
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["code"] == "UNAUTHORIZED"


def test_bad_uuid_is_422_validation_error(authed: TestClient) -> None:
    resp = authed.get("/api/v1/series/not-a-uuid")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
