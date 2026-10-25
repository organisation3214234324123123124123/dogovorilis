from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.models import Item
from app.modules.items.models import ItemStatus
from tests.acceptance.helpers_items import (
    API,
    clock,  # noqa: F401
    foreign_host,
    if_match,
    make_ctx,
    problem_code,
)
from tests.conftest import login_as
from tests.factories import World, make_item, make_meeting, make_user

pytestmark = [pytest.mark.fr("FR-15"), pytest.mark.usefixtures("clock")]


NOT_DRAFT = [
    (ItemStatus.PENDING, {"sent_at": datetime(2026, 10, 14, tzinfo=UTC)}),
    (ItemStatus.ACCEPTED, {"sent_at": datetime(2026, 10, 14, tzinfo=UTC)}),
    (ItemStatus.DISPUTED, {"sent_at": datetime(2026, 10, 14, tzinfo=UTC)}),
    (ItemStatus.DONE, {"done_at": datetime(2026, 10, 14, tzinfo=UTC)}),
    (ItemStatus.CANCELLED, {"cancelled_at": datetime(2026, 10, 14, tzinfo=UTC)}),
]


def delete(client: TestClient, item: Item):
    return client.delete(f"{API}/items/{item.id}", headers=if_match(item))


def count(world: World, model, **where) -> int:
    world.db.expire_all()
    return world.db.query(model).filter_by(**where).count()


def test_delete_foreign_host_item_is_404(client: TestClient, world: World) -> None:
    make_ctx(world)
    item = world.item()
    login_as(client, make_user(world.db))
    resp = delete(client, item)
    assert resp.status_code == 404
    assert problem_code(resp) == "NOT_FOUND"
    assert count(world, Item, id=item.id) == 1


def test_delete_item_in_foreign_series_is_404(authed: TestClient, world: World) -> None:
    _, _, _, series = foreign_host(world)
    item = make_item(world.db, make_meeting(world.db, series))
    assert delete(authed, item).status_code == 404


def test_delete_unknown_item_is_404(authed: TestClient, world: World) -> None:
    import uuid

    resp = authed.delete(f"{API}/items/{uuid.uuid4()}", headers={"If-Match": "1"})
    assert resp.status_code == 404
