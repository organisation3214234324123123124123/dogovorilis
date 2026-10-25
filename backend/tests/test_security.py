import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import Unauthorized, register_error_handlers
from app.core.security import CurrentUser, get_current_user

pytestmark = pytest.mark.fr("infra")


def test_get_current_user_raises_unauthorized() -> None:
    with pytest.raises(Unauthorized):
        get_current_user()


def test_route_with_current_user_returns_401_problem_json() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/_t/me")
    def me(user: CurrentUser) -> dict[str, str]:
        return {"id": "x"}

    response = TestClient(app).get("/_t/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 401
