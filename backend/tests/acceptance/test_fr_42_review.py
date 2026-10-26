from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.modules.items.models import ItemStatus
from app.modules.notifications.models import NotificationType
from app.modules.series.models import MeetingStatus
from tests.acceptance.helpers import act, frozen
from tests.factories import (
    World,
    add_member,
    make_item,
    make_meeting,
    make_series,
    make_user,
    notifications_for,
)

pytestmark = pytest.mark.fr("FR-42")

THURSDAY = "2026-10-15 09:00:00+00:00"
MONDAY = "2026-10-19 09:00:00+00:00"
FUTURE = date(2099, 1, 1)
PAST = date(2026, 10, 1)


def review(client: TestClient, meeting):
    resp = client.get(f"/api/v1/meetings/{meeting.id}/review")
    assert resp.status_code == 200
    return resp.json()


def ids(rows: list[dict]) -> set[str]:
    return {r["id"] for r in rows}


def sent(world: World, when: str, **kw):
    return world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.fromisoformat(when),
        due_date=FUTURE,
        **kw,
    )


def test_start_response_and_review_endpoint_return_same_list(
    authed: TestClient, world: World
) -> None:
    world.item(assignee_person_id=world.person.id, status=ItemStatus.ACCEPTED, due_date=PAST)
    started = authed.post(f"/api/v1/series/{world.series.id}/meetings", json={})
    assert started.status_code == 201
    again = authed.get(f"/api/v1/meetings/{started.json()['meeting']['id']}/review")
    assert again.status_code == 200
    assert again.json() == started.json()["review"]


def test_groups_open_items_by_reason(authed: TestClient, world: World) -> None:
    unassigned = world.item(
        status=ItemStatus.PENDING, sent_at=datetime(2026, 10, 15, tzinfo=UTC), due_date=FUTURE
    )
    overdue = world.item(
        assignee_person_id=world.person.id, status=ItemStatus.ACCEPTED, due_date=PAST
    )
    disputed = world.item(
        assignee_person_id=world.person.id, status=ItemStatus.DISPUTED, due_date=FUTURE
    )
    stale = sent(world, "2026-10-12 09:00:00+00:00")
    with frozen(authed, world.user, THURSDAY):
        data = review(authed, world.meeting)
    assert ids(data["unassigned"]) == {str(unassigned.id)}
    assert ids(data["overdue"]) == {str(overdue.id)}
    assert ids(data["disputed"]) == {str(disputed.id)}
    assert ids(data["stale_pending"]) == {str(stale.id)}


def test_pending_sent_one_business_day_ago_is_not_listed(authed: TestClient, world: World) -> None:
    fresh = sent(world, "2026-10-14 09:00:00+00:00")
    with frozen(authed, world.user, THURSDAY):
        data = review(authed, world.meeting)
    assert str(fresh.id) not in ids(data["stale_pending"])


def test_pending_sent_three_business_days_ago_is_listed(authed: TestClient, world: World) -> None:
    old = sent(world, "2026-10-12 09:00:00+00:00")
    with frozen(authed, world.user, THURSDAY):
        data = review(authed, world.meeting)
    assert str(old.id) in ids(data["stale_pending"])


def test_weekend_does_not_count_as_business_days(authed: TestClient, world: World) -> None:
    friday = sent(world, "2026-10-16 15:00:00+00:00")
    wednesday = sent(world, "2026-10-14 09:00:00+00:00")
    with frozen(authed, world.user, MONDAY):
        data = review(authed, world.meeting)
    listed = ids(data["stale_pending"])
    assert str(friday.id) not in listed
    assert str(wednesday.id) in listed


def test_stale_pending_is_counted_from_sent_at(authed: TestClient, world: World) -> None:
    item = sent(world, "2026-10-14 09:00:00+00:00")
    item.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    world.db.flush()
    with frozen(authed, world.user, THURSDAY):
        data = review(authed, world.meeting)
    assert str(item.id) not in ids(data["stale_pending"])


def test_done_since_last_meeting_are_listed_together(authed: TestClient, world: World) -> None:
    previous = world.meeting
    previous.status = MeetingStatus.SENT
    previous.planned_start = datetime.now(UTC) - timedelta(days=7)
    done_at = datetime.now(UTC) - timedelta(days=2)
    fresh = [
        world.item(
            assignee_person_id=world.person.id,
            status=ItemStatus.DONE,
            done_at=done_at,
            done_by="assignee",
        )
        for _ in range(2)
    ]
    long_ago = world.item(
        status=ItemStatus.DONE, done_at=datetime.now(UTC) - timedelta(days=30), done_by="host"
    )
    started = authed.post(f"/api/v1/series/{world.series.id}/meetings", json={})
    assert started.status_code == 201
    listed = ids(started.json()["review"]["done_since_last"])
    assert listed == {str(i.id) for i in fresh}
    assert str(long_ago.id) not in listed


def test_assignee_done_needs_no_host_confirmation(authed: TestClient, world: World) -> None:
    item = world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.DONE,
        done_at=datetime.now(UTC),
        done_by="assignee",
    )
    data = review(authed, world.meeting)
    row = next(r for r in data["done_since_last"] if r["id"] == str(item.id))
    assert row["status"] == "done"


def test_nothing_open_gives_empty_review(authed: TestClient, world: World) -> None:
    data = review(authed, world.meeting)
    assert all(data[key] == [] for key in data)


def test_other_series_and_other_hosts_are_excluded(authed: TestClient, world: World) -> None:
    mine = world.item(assignee_person_id=world.person.id, status=ItemStatus.ACCEPTED, due_date=PAST)
    sibling = make_meeting(world.db, make_series(world.db, world.user))
    foreign = make_meeting(world.db, make_series(world.db, make_user(world.db)))
    other_series = make_item(world.db, sibling, status=ItemStatus.ACCEPTED, due_date=PAST)
    other_host = make_item(world.db, foreign, status=ItemStatus.ACCEPTED, due_date=PAST)
    data = review(authed, world.meeting)
    assert ids(data["overdue"]) == {str(mine.id)}
    assert str(other_series.id) not in ids(data["overdue"])
    assert str(other_host.id) not in ids(data["overdue"])


def test_review_of_foreign_meeting_is_404(authed: TestClient, world: World) -> None:
    foreign = make_meeting(world.db, make_series(world.db, make_user(world.db)))
    resp = authed.get(f"/api/v1/meetings/{foreign.id}/review")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_reopen_returns_done_item_to_accepted(authed: TestClient, world: World) -> None:
    add_member(world.db, world.series, world.person)
    item = world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.DONE,
        done_at=datetime.now(UTC),
        done_by="host",
        due_date=FUTURE,
    )
    resp = act(authed, item, "reopen")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert resp.json()["done_at"] is None


def test_reopen_notifies_assignee_with_item_changed(authed: TestClient, world: World) -> None:
    item = world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.DONE,
        done_at=datetime.now(UTC),
        done_by="host",
        due_date=FUTURE,
    )
    assert act(authed, item, "reopen").status_code == 200
    queued = notifications_for(world.db, item)
    assert [(n.type, n.recipient_person_id) for n in queued] == [
        (NotificationType.ITEM_CHANGED, world.person.id)
    ]


def test_unassigned_item_with_past_due_is_only_in_unassigned(
    authed: TestClient, world: World
) -> None:
    item = world.item(
        status=ItemStatus.PENDING, sent_at=datetime(2026, 10, 15, tzinfo=UTC), due_date=PAST
    )
    with frozen(authed, world.user, THURSDAY):
        data = review(authed, world.meeting)
    assert str(item.id) in ids(data["unassigned"])
    for group in ("overdue", "disputed", "stale_pending"):
        assert str(item.id) not in ids(data[group])


# Граница "2 рабочих дня" (FR-42, BR-7, OQ-13): считаем от sent_at, "2 дня и больше".
@pytest.mark.parametrize(
    ("sent_at", "now", "listed"),
    [
        ("2026-10-14 09:00:00+00:00", "2026-10-15 09:00:00+00:00", False),
        ("2026-10-14 09:00:00+00:00", "2026-10-16 09:00:00+00:00", True),
        ("2026-10-14 09:00:00+00:00", "2026-10-19 09:00:00+00:00", True),
        ("2026-10-15 09:00:00+00:00", "2026-10-16 09:00:00+00:00", False),
        ("2026-10-15 09:00:00+00:00", "2026-10-18 09:00:00+00:00", False),  # чт -> вс: 1 (выходной)
        ("2026-10-15 09:00:00+00:00", "2026-10-19 09:00:00+00:00", True),
        ("2026-10-16 09:00:00+00:00", "2026-10-18 09:00:00+00:00", False),  # пт -> вс: 0
        ("2026-10-16 09:00:00+00:00", "2026-10-19 09:00:00+00:00", False),
        ("2026-10-16 09:00:00+00:00", "2026-10-20 09:00:00+00:00", True),
        ("2026-10-16 09:00:00+00:00", "2026-10-21 09:00:00+00:00", True),
        ("2026-10-17 09:00:00+00:00", "2026-10-19 09:00:00+00:00", False),  # сб -> пн: 1
        ("2026-10-17 09:00:00+00:00", "2026-10-20 09:00:00+00:00", True),  # сб -> вт: 2
    ],
    ids=str,
)
def test_stale_pending_boundary(
    authed: TestClient, world: World, sent_at: str, now: str, listed: bool
) -> None:
    item = sent(world, sent_at)
    with frozen(authed, world.user, now):
        data = review(authed, world.meeting)
    assert (str(item.id) in ids(data["stale_pending"])) is listed
