import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.fr("infra")


def test_request_id_generated(client: TestClient) -> None:
    assert client.get("/api/v1/health").headers.get("X-Request-ID")


def test_request_id_echoed(client: TestClient) -> None:
    r = client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"
