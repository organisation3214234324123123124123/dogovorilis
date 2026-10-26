import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.fr("infra")


def test_openapi_json_available(client: TestClient) -> None:
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    assert "/api/v1/health" in r.json()["paths"]


def test_swagger_ui(client: TestClient) -> None:
    r = client.get("/api/docs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_oauth2_security_scheme(client: TestClient) -> None:
    schemes = client.get("/api/openapi.json").json()["components"]["securitySchemes"]
    assert any(s["type"] == "oauth2" for s in schemes.values())
