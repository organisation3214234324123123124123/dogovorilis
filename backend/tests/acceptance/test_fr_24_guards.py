import pytest
from fastapi.testclient import TestClient

from app.models import Gap
from app.modules.gaps.models import GapSource, GapType
from tests.acceptance.helpers_items import (
    API,
    clock,  # noqa: F401
    foreign_host,
    make_ctx,
    problem_code,
)
from tests.conftest import login_as
from tests.factories import World, make_gap, make_item, make_meeting, make_user

pytestmark = [pytest.mark.fr("FR-24"), pytest.mark.usefixtures("clock")]


@pytest.fixture
def case(world: World):
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id)
    gap = make_gap(world.db, item, type=GapType.NO_DUE, source=GapSource.RULE)
    return ctx, item, gap


def dismiss(client: TestClient, item, gap_id):
    return client.post(f"{API}/items/{item.id}/gaps/{gap_id}/dismiss")


def reload_gap(world: World, gap) -> Gap:
    world.db.expire_all()
    return world.db.get(Gap, gap.id)


def test_foreign_host_is_404(client: TestClient, world: World, case) -> None:
    _, item, gap = case
    login_as(client, make_user(world.db))
    resp = dismiss(client, item, gap.id)
    assert resp.status_code == 404
    assert problem_code(resp) == "NOT_FOUND"
    assert reload_gap(world, gap).dismissed_at is None


def test_gap_of_foreign_series_is_404(authed: TestClient, world: World) -> None:
    _, _, _, series = foreign_host(world)
    item = make_item(world.db, make_meeting(world.db, series))
    gap = make_gap(world.db, item)
    assert dismiss(authed, item, gap.id).status_code == 404
