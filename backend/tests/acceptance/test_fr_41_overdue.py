from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Notification
from app.modules.items import service as items_service
from app.modules.items.models import Item, ItemStatus
from tests.acceptance.helpers import frozen
from tests.factories import World, events_named, make_item, make_meeting, make_series

pytestmark = pytest.mark.fr("FR-41")

NOW = "2026-10-16 09:00:00+00:00"
TODAY = date(2026, 10, 16)
YESTERDAY = date(2026, 10, 15)


def make(world: World, status: ItemStatus, due: date | None, **kw) -> Item:
    if status == ItemStatus.DONE:
        kw["done_at"] = datetime(2026, 10, 1, tzinfo=UTC)
    if status == ItemStatus.CANCELLED:
        kw["cancelled_at"] = datetime(2026, 10, 1, tzinfo=UTC)
    return world.item(status=status, due_date=due, **kw)


def overdue_flags(client: TestClient, world: World) -> dict[str, bool]:
    resp = client.get(f"/api/v1/series/{world.series.id}/items")
    assert resp.status_code == 200
    return {i["id"]: i["is_overdue"] for i in resp.json()["items"]}


@pytest.mark.parametrize("status", [ItemStatus.ACCEPTED, ItemStatus.PENDING])
def test_open_item_with_past_due_is_overdue(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    item = make(world, status, YESTERDAY)
    with frozen(authed, world.user, NOW):
        assert overdue_flags(authed, world)[str(item.id)] is True


@pytest.mark.parametrize(
    "status", [ItemStatus.DONE, ItemStatus.CANCELLED, ItemStatus.DISPUTED, ItemStatus.DRAFT]
)
def test_other_statuses_are_never_overdue(
    authed: TestClient, world: World, status: ItemStatus
) -> None:
    item = make(world, status, YESTERDAY)
    with frozen(authed, world.user, NOW):
        assert overdue_flags(authed, world)[str(item.id)] is False


def test_due_today_is_not_overdue(authed: TestClient, world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, TODAY)
    with frozen(authed, world.user, NOW):
        assert overdue_flags(authed, world)[str(item.id)] is False


def test_today_is_taken_in_series_timezone(authed: TestClient, world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, TODAY)
    with frozen(authed, world.user, "2026-10-16 22:00:00+00:00"):
        assert overdue_flags(authed, world)[str(item.id)] is True


def test_overdue_does_not_change_status(authed: TestClient, world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, YESTERDAY)
    with frozen(authed, world.user, NOW):
        resp = authed.get(f"/api/v1/series/{world.series.id}/items")
    row = next(i for i in resp.json()["items"] if i["id"] == str(item.id))
    assert row["status"] == "accepted"
    world.db.refresh(item)
    assert item.status == ItemStatus.ACCEPTED


# 21:05 UTC - это 00:05 2026-10-17 в поясе серии: D = 2026-10-17.
JOB_MOMENT = datetime(2026, 10, 16, 21, 5, tzinfo=UTC)
JOB_DAY = date(2026, 10, 17)


def test_daily_job_writes_item_overdue_with_days_overdue(world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, date(2026, 10, 13))
    items_service.emit_overdue_events(world.db, JOB_MOMENT)
    events = events_named(world.db, "item_overdue", item_id=item.id)
    assert len(events) == 1
    assert events[0].props["days_overdue"] == (JOB_DAY - date(2026, 10, 13)).days == 4


def test_daily_job_counts_item_due_the_day_before_d(world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, date(2026, 10, 16))
    items_service.emit_overdue_events(world.db, JOB_MOMENT)
    events = events_named(world.db, "item_overdue", item_id=item.id)
    assert [e.props["days_overdue"] for e in events] == [1]


def test_daily_job_skips_item_due_on_d(world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, JOB_DAY)
    items_service.emit_overdue_events(world.db, JOB_MOMENT)
    assert events_named(world.db, "item_overdue", item_id=item.id) == []


def test_daily_job_agrees_with_api_is_overdue(authed: TestClient, world: World) -> None:
    due_before = make(world, ItemStatus.ACCEPTED, date(2026, 10, 16))
    due_on_d = make(world, ItemStatus.ACCEPTED, JOB_DAY)
    with frozen(authed, world.user, "2026-10-16 21:05:00+00:00"):
        flags = overdue_flags(authed, world)
    assert flags[str(due_before.id)] is True
    assert flags[str(due_on_d.id)] is False


def test_daily_job_skips_items_that_are_not_overdue(world: World) -> None:
    done = make(world, ItemStatus.DONE, YESTERDAY)
    fresh = make(world, ItemStatus.PENDING, JOB_DAY)
    items_service.emit_overdue_events(world.db, JOB_MOMENT)
    assert events_named(world.db, "item_overdue", item_id=done.id) == []
    assert events_named(world.db, "item_overdue", item_id=fresh.id) == []


def test_daily_job_runs_at_00_05_of_series_timezone(world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, YESTERDAY)
    items_service.emit_overdue_events(world.db, datetime(2026, 10, 16, 21, 5, tzinfo=UTC))
    assert len(events_named(world.db, "item_overdue", item_id=item.id)) == 1


def test_daily_job_skips_series_where_it_is_not_00_05(world: World) -> None:
    other = make_series(world.db, world.user, timezone="Asia/Yekaterinburg")
    meeting = make_meeting(world.db, other)
    item = make_item(world.db, meeting, status=ItemStatus.ACCEPTED, due_date=YESTERDAY)
    items_service.emit_overdue_events(world.db, datetime(2026, 10, 16, 21, 5, tzinfo=UTC))
    assert events_named(world.db, "item_overdue", item_id=item.id) == []


def test_daily_job_ignores_series_outside_00_05(world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, YESTERDAY)
    items_service.emit_overdue_events(world.db, datetime(2026, 10, 16, 9, 0, tzinfo=UTC))
    assert events_named(world.db, "item_overdue", item_id=item.id) == []


def test_overdue_creates_no_notifications(world: World) -> None:
    make(world, ItemStatus.ACCEPTED, YESTERDAY)
    before = world.db.scalar(select(func.count()).select_from(Notification))
    items_service.emit_overdue_events(world.db, datetime(2026, 10, 16, 21, 5, tzinfo=UTC))
    assert world.db.scalar(select(func.count()).select_from(Notification)) == before
    assert events_named(world.db, "item_overdue") != []


def test_overdue_item_is_in_next_meeting_review(authed: TestClient, world: World) -> None:
    item = make(world, ItemStatus.ACCEPTED, YESTERDAY)
    with frozen(authed, world.user, NOW):
        resp = authed.get(f"/api/v1/meetings/{world.meeting.id}/review")
    assert resp.status_code == 200
    assert [i["id"] for i in resp.json()["overdue"]] == [str(item.id)]
