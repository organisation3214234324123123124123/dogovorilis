import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

pytestmark = pytest.mark.fr("FR-12")

TOKEN = "t" * 32
URL = "/api/v1/admin/llm/prompts"


@pytest.fixture
def admin_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    get_settings.cache_clear()


def test_disabled_when_token_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "")
    get_settings.cache_clear()
    resp = client.get(URL, headers={"X-Admin-Token": TOKEN})
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "ADMIN_DISABLED"


def test_no_header_is_forbidden(client: TestClient, admin_on) -> None:
    resp = client.get(URL)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_wrong_header_is_forbidden(client: TestClient, admin_on) -> None:
    resp = client.get(URL, headers={"X-Admin-Token": "x" * 32})
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_host_cookie_alone_is_forbidden(authed: TestClient, admin_on) -> None:
    resp = authed.get(URL)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_host_bearer_is_forbidden(client: TestClient, world, admin_on) -> None:
    from app.core.security import create_access_token

    resp = client.get(
        URL, headers={"Authorization": f"Bearer {create_access_token(world.user.id)}"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_correct_token_lists_prompts(client: TestClient, admin_on) -> None:
    resp = client.get(URL, headers={"X-Admin-Token": TOKEN})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
