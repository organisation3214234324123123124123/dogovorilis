import pytest
from fastapi.testclient import TestClient

from app.main import create_app

pytestmark = pytest.mark.fr("infra")


def test_health_default_version(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert (body["status"], body["version"]) == ("ok", "dev")


def test_health_version_from_env(monkeypatch: pytest.MonkeyPatch, settings_reset: None) -> None:
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app.core.config import get_settings

    get_settings.cache_clear()
    r = TestClient(create_app()).get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["version"] == "1.2.3"
