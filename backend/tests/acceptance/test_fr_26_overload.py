from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import SeriesMember
from app.modules.items.models import ItemStatus
from tests.acceptance.helpers_items import (
    API,
    TODAY,
    clock,  # noqa: F401
    events,
    if_match,
    make_ctx,
)
from tests.factories import (
    World,
    make_item,
    make_meeting,
    make_series,
    make_user,
)

pytestmark = [
    pytest.mark.fr("FR-26"),
    pytest.mark.usefixtures("clock"),
]

SOON = TODAY + timedelta(days=3)
SENT = datetime(2026, 10, 14, tzinfo=UTC)


def load(world: World, person, n: int, *, meeting=None, status=ItemStatus.PENDING, due=SOON):
    extra = {}
    if status in (ItemStatus.DONE,):
        extra["done_at"] = SENT
    if status == ItemStatus.CANCELLED:
        extra["cancelled_at"] = SENT
    if status != ItemStatus.DRAFT:
        extra["sent_at"] = SENT
    return [
        make_item(
            world.db,
            meeting or world.meeting,
            assignee_person_id=person.id,
            status=status,
            due_date=due,
            **extra,
        )
        for _ in range(n)
    ]


def assign(client: TestClient, world: World, person):
    item = world.item(what="Новая задача")
    resp = client.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(person.id)},
        headers=if_match(item),
    )
    return resp


def assign_ok(client: TestClient, world: World, person) -> None:
    assert assign(client, world, person).status_code == 200


def shown(world: World):
    return events(world.db, "overload_warning_shown")


def test_five_open_items_show_warning_with_count(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 3)
    load(world, ctx.ivan, 2, status=ItemStatus.ACCEPTED)
    assert assign(authed, world, ctx.ivan).status_code == 200
    [event] = shown(world)
    assert event.props["open_items"] == 5


def test_four_open_items_show_no_warning(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 4)
    assert assign(authed, world, ctx.ivan).status_code == 200
    assert shown(world) == []


@pytest.mark.parametrize(
    ("status", "due"),
    [
        (ItemStatus.PENDING, TODAY + timedelta(days=8)),
        (ItemStatus.PENDING, TODAY + timedelta(days=30)),
        (ItemStatus.DONE, SOON),
        (ItemStatus.CANCELLED, SOON),
        (ItemStatus.DISPUTED, SOON),
        (ItemStatus.DRAFT, SOON),
    ],
)
def test_items_outside_the_rule_are_not_counted(
    authed: TestClient, world: World, status: ItemStatus, due: date
) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 4)
    load(world, ctx.ivan, 1, status=status, due=due)
    assign_ok(authed, world, ctx.ivan)
    assert shown(world) == []


def test_other_hosts_items_are_not_counted(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 4)
    other = make_user(world.db)
    other_series = make_series(world.db, other)
    other_meeting = make_meeting(world.db, other_series)
    load(world, ctx.ivan, 5, meeting=other_meeting)
    assign_ok(authed, world, ctx.ivan)
    assert shown(world) == []


def test_items_are_counted_across_all_series_of_host(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    second = make_series(world.db, world.user)
    world.db.add(SeriesMember(series_id=second.id, person_id=ctx.ivan.id))
    world.db.flush()
    load(world, ctx.ivan, 3)
    load(world, ctx.ivan, 2, meeting=make_meeting(world.db, second))
    assign(authed, world, ctx.ivan)
    [event] = shown(world)
    assert event.props["open_items"] == 5


def test_assignment_is_not_blocked(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 9)
    resp = assign(authed, world, ctx.ivan)
    assert resp.status_code == 200
    assert resp.json()["assignee"]["id"] == str(ctx.ivan.id)


def test_other_executor_load_is_independent(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    load(world, ctx.ivan, 6)
    assign_ok(authed, world, ctx.anna)
    assert shown(world) == []


def test_series_threshold_lowers_the_limit(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    world.series.load_warning_threshold = 3
    world.db.flush()
    load(world, ctx.ivan, 3)
    assign(authed, world, ctx.ivan)
    [event] = shown(world)
    assert event.props["open_items"] == 3


def test_series_threshold_raises_the_limit(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    world.series.load_warning_threshold = 7
    world.db.flush()
    load(world, ctx.ivan, 5)
    assign_ok(authed, world, ctx.ivan)
    assert shown(world) == []


def test_default_threshold_is_five_when_null(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    assert world.series.load_warning_threshold is None
    load(world, ctx.ivan, 5)
    assign(authed, world, ctx.ivan)
    assert len(shown(world)) == 1


def test_threshold_is_changed_in_series_settings(authed: TestClient, world: World) -> None:
    resp = authed.patch(f"{API}/series/{world.series.id}", json={"load_warning_threshold": 3})
    assert resp.status_code == 200
    assert resp.json()["load_warning_threshold"] == 3
