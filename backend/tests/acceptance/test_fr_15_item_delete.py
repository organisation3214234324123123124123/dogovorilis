from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.models import Gap, Item
from app.modules.gaps.models import GapType
from app.modules.items.models import ItemStatus
from tests.acceptance.helpers_items import (
    API,
    clock,  # noqa: F401
    if_match,
    make_ctx,
    problem_code,
    ws_events,
)
from tests.factories import World, make_gap

pytestmark = [
    pytest.mark.fr("FR-15"),
    pytest.mark.usefixtures("clock"),
]

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


def test_delete_draft_returns_204_and_removes_item_with_gaps(
    authed: TestClient, world: World
) -> None:
    make_ctx(world)
    item = world.item()
    make_gap(world.db, item, type=GapType.NO_DUE)
    make_gap(world.db, item, type=GapType.NO_ASSIGNEE)
    resp = delete(authed, item)
    assert resp.status_code == 204
    assert count(world, Item, id=item.id) == 0
    assert count(world, Gap, item_id=item.id) == 0


def test_delete_leaves_other_items_alone(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item, other = world.item(), world.item()
    make_gap(world.db, other)
    assert delete(authed, item).status_code == 204
    assert count(world, Item, id=other.id) == 1
    assert count(world, Gap, item_id=other.id) == 1


@pytest.mark.parametrize(("status", "extra"), NOT_DRAFT, ids=[s.value for s, _ in NOT_DRAFT])
def test_delete_not_draft_is_409_invalid_transition(
    authed: TestClient, world: World, status: ItemStatus, extra: dict
) -> None:
    make_ctx(world)
    item = world.item(status=status, **extra)
    resp = delete(authed, item)
    assert resp.status_code == 409
    assert problem_code(resp) == "INVALID_TRANSITION"
    assert count(world, Item, id=item.id) == 1


def test_panel_receives_item_deleted_event(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item = world.item()
    frames = ws_events(authed, world.meeting.id, lambda: delete(authed, item), ("item.deleted",))
    assert "item.deleted" in {f.get("type") for f in frames}
