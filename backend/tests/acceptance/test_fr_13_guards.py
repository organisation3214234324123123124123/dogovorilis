import pytest
from fastapi.testclient import TestClient

from app.models import Item
from tests.acceptance.helpers_items import (
    API,
    clock,  # noqa: F401
    foreign_host,
    if_match,
    make_ctx,
    problem_code,
)
from tests.conftest import login_as
from tests.factories import World, make_meeting, make_user

pytestmark = [pytest.mark.fr("FR-13"), pytest.mark.usefixtures("clock")]


@pytest.fixture
def draft(world: World):
    ctx = make_ctx(world)
    item = world.item(what="Старое", raw_text="Старое")
    return ctx, item


def patch(client: TestClient, item: Item, body: dict, headers: dict | None = None):
    return client.patch(
        f"{API}/items/{item.id}", json=body, headers=if_match(item) if headers is None else headers
    )


def reload(world: World, item: Item) -> Item:
    world.db.expire_all()
    return world.db.get(Item, item.id)


def make_item_for(world: World, meeting) -> Item:
    from tests.factories import make_item

    return make_item(world.db, meeting)


def test_missing_if_match_is_428(authed: TestClient, world: World, draft) -> None:
    _, item = draft
    resp = patch(authed, item, {"what": "Новое"}, headers={})
    assert resp.status_code == 428
    assert problem_code(resp) == "PRECONDITION_REQUIRED"


@pytest.mark.parametrize(
    ("field", "limit"), [("what", 500), ("done_criteria", 500), ("due_text", 100)]
)
def test_field_over_limit_is_422_validation_error(
    authed: TestClient, world: World, draft, field: str, limit: int
) -> None:
    _, item = draft
    resp = patch(authed, item, {field: "а" * (limit + 1)})
    assert resp.status_code == 422
    assert problem_code(resp) == "VALIDATION_ERROR"


def test_foreign_host_item_is_404(client: TestClient, world: World, draft) -> None:
    _, item = draft
    login_as(client, make_user(world.db))
    resp = patch(client, item, {"what": "Чужое"})
    assert resp.status_code == 404
    assert problem_code(resp) == "NOT_FOUND"


def test_foreign_series_item_is_404(authed: TestClient, world: World) -> None:
    _, _, _, series = foreign_host(world)
    meeting = make_meeting(world.db, series)
    item = make_item_for(world, meeting)
    assert patch(authed, item, {"what": "Чужое"}).status_code == 404
