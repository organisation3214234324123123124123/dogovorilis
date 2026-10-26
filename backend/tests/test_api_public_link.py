import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.modules.items.models import ItemStatus
from tests.factories import World, make_access_link, make_person, make_user

pytestmark = pytest.mark.fr("FR-31")

TOKEN = "tok-" + "a" * 28


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@pytest.fixture
def link(world: World):
    return make_access_link(world.db, world.person, world.meeting, token_hash=digest(TOKEN))


@pytest.fixture
def own_item(world: World, link):
    return world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.now(UTC),
        what="Mine",
    )


def csrf_headers(client: TestClient, token: str = TOKEN) -> dict[str, str]:
    csrf = client.get(f"/api/v1/p/{token}").json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


def test_page_returns_own_items_only(client: TestClient, world: World, own_item) -> None:
    other = make_person(world.db, world.user, world.org)
    world.item(assignee_person_id=other.id, status=ItemStatus.PENDING, sent_at=datetime.now(UTC))
    resp = client.get(f"/api/v1/p/{TOKEN}")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["id"] for i in body["items"]] == [str(own_item.id)]
    assert body["person_name"] == world.person.full_name
    assert body["series_title"] == world.series.title
    assert body["host_name"] == world.user.full_name


@pytest.mark.fr("FR-32")
def test_page_sets_strict_csrf_cookie(client: TestClient, own_item) -> None:
    resp = client.get(f"/api/v1/p/{TOKEN}")
    cookie = resp.headers["set-cookie"].lower()
    assert "csrf_token=" in cookie
    assert "samesite=strict" in cookie
    assert "httponly" in cookie
    assert "path=/api/v1/p" in cookie
    assert resp.json()["csrf_token"]


def test_page_privacy_headers(client: TestClient, own_item) -> None:
    resp = client.get(f"/api/v1/p/{TOKEN}")
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in resp.headers["x-robots-tag"]


def test_page_get_does_not_change_items(client: TestClient, world: World, own_item) -> None:
    client.get(f"/api/v1/p/{TOKEN}")
    world.db.refresh(own_item)
    assert own_item.status == ItemStatus.PENDING
    assert own_item.version == 1


def test_unknown_token_is_404(client: TestClient, link) -> None:
    resp = client.get("/api/v1/p/unknown-token")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_revoked_link_is_410_without_item_data(client: TestClient, world: World, own_item, link):
    link.revoked_at = datetime.now(UTC)
    world.db.flush()
    resp = client.get(f"/api/v1/p/{TOKEN}")
    assert resp.status_code == 410
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "LINK_EXPIRED"
    assert body["host_name"] == world.user.full_name
    assert "items" not in body


def test_expired_link_is_410(client: TestClient, world: World, own_item, link) -> None:
    link.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    world.db.flush()
    resp = client.get(f"/api/v1/p/{TOKEN}")
    assert resp.status_code == 410
    assert resp.json()["code"] == "LINK_EXPIRED"


@pytest.mark.fr("FR-32")
def test_post_without_csrf_is_403_csrf_invalid(client: TestClient, own_item) -> None:
    resp = client.post(f"/api/v1/p/{TOKEN}/items/{own_item.id}/accept")
    assert resp.status_code == 403
    assert resp.json()["code"] == "CSRF_INVALID"


@pytest.mark.fr("FR-32")
def test_post_with_wrong_csrf_header_is_403_csrf_invalid(client: TestClient, own_item) -> None:
    client.get(f"/api/v1/p/{TOKEN}")
    resp = client.post(
        f"/api/v1/p/{TOKEN}/items/{own_item.id}/accept", headers={"X-CSRF-Token": "wrong"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "CSRF_INVALID"


@pytest.mark.fr("FR-32")
def test_post_with_header_but_no_cookie_is_403_csrf_invalid(client: TestClient, own_item) -> None:
    headers = csrf_headers(client)
    client.cookies.clear()
    resp = client.post(f"/api/v1/p/{TOKEN}/items/{own_item.id}/accept", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "CSRF_INVALID"


def test_someone_elses_item_is_403_not_your_item(client: TestClient, world: World, link) -> None:
    other = make_person(world.db, world.user, world.org)
    foreign = world.item(
        assignee_person_id=other.id, status=ItemStatus.PENDING, sent_at=datetime.now(UTC)
    )
    headers = csrf_headers(client)
    resp = client.post(f"/api/v1/p/{TOKEN}/items/{foreign.id}/accept", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "NOT_YOUR_ITEM"


def test_item_of_another_host_is_403_not_your_item(client: TestClient, world: World, link) -> None:
    stranger = make_user(world.db)
    from tests.factories import make_meeting, make_series

    foreign_meeting = make_meeting(world.db, make_series(world.db, stranger))
    from tests.factories import make_item

    foreign = make_item(world.db, foreign_meeting)
    headers = csrf_headers(client)
    resp = client.post(f"/api/v1/p/{TOKEN}/items/{foreign.id}/accept", headers=headers)
    assert resp.status_code in (403, 404)
    assert "items" not in resp.json()


def test_post_on_revoked_link_is_410(client: TestClient, world: World, own_item, link) -> None:
    headers = csrf_headers(client)
    link.revoked_at = datetime.now(UTC)
    world.db.flush()
    resp = client.post(f"/api/v1/p/{TOKEN}/items/{own_item.id}/accept", headers=headers)
    assert resp.status_code == 410
    assert resp.json()["code"] == "LINK_EXPIRED"
