from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

from app.models import SeriesMember
from app.modules.items.models import ActorType, ItemAction, ItemStatus
from tests.conftest import login_as
from tests.factories import World, make_event, make_meeting, make_organization, make_person

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)


@pytest.fixture
def clock():
    with freeze_time(NOW) as frozen:
        yield frozen


@pytest.fixture
def authed(clock, client: TestClient, world: World) -> TestClient:
    login_as(client, world.user)
    return client


@pytest.fixture
def w(world: World, authed: TestClient) -> World:
    world.db.add(SeriesMember(series_id=world.series.id, person_id=world.person.id))
    world.db.flush()
    return world


def ids(resp) -> set[str]:
    return {i["id"] for i in resp.json()["items"]}


@pytest.mark.fr("FR-04")
def test_series_list(authed: TestClient, w: World) -> None:
    resp = authed.get("/api/v1/series")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == [str(w.series.id)]
    assert rows[0]["title"] == w.series.title
    assert rows[0]["members_count"] == 1


@pytest.mark.fr("FR-04")
def test_series_detail_includes_members(authed: TestClient, w: World) -> None:
    resp = authed.get(f"/api/v1/series/{w.series.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(w.series.id)
    assert [m["person_id"] for m in body["members"]] == [str(w.person.id)]
    assert body["timezone"] == "Europe/Moscow"


@pytest.mark.fr("FR-05")
def test_members(authed: TestClient, w: World) -> None:
    resp = authed.get(f"/api/v1/series/{w.series.id}/members")
    assert resp.status_code == 200
    (member,) = resp.json()
    assert member["person_id"] == str(w.person.id)
    assert member["full_name"] == w.person.full_name
    assert member["organization_name"] == w.org.name
    assert member["is_own_side"] is True


@pytest.mark.fr("FR-05")
def test_members_of_other_organization(authed: TestClient, w: World) -> None:
    other_org = make_organization(w.db, w.user, name="Partner")
    other = make_person(w.db, w.user, other_org, full_name="Partner Person")
    w.db.add(SeriesMember(series_id=w.series.id, person_id=other.id))
    w.db.flush()
    members = {
        m["full_name"]: m for m in authed.get(f"/api/v1/series/{w.series.id}/members").json()
    }
    assert members["Partner Person"]["is_own_side"] is False


def test_series_meetings(authed: TestClient, w: World) -> None:
    second = make_meeting(w.db, w.series, planned_start=w.meeting.planned_start + timedelta(days=7))
    resp = authed.get(f"/api/v1/series/{w.series.id}/meetings")
    assert resp.status_code == 200
    assert {m["id"] for m in resp.json()} == {str(w.meeting.id), str(second.id)}


@pytest.mark.fr("FR-06")
def test_meeting_detail(authed: TestClient, w: World) -> None:
    item = w.item()
    resp = authed.get(f"/api/v1/meetings/{w.meeting.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(w.meeting.id)
    assert body["series_title"] == w.series.title
    assert body["status"] == "planned"
    assert [i["id"] for i in body["items"]] == [str(item.id)]
    assert body["open_gaps_count"] == 0


@pytest.mark.fr("FR-45")
def test_item_card_has_etag_equal_to_version(authed: TestClient, w: World) -> None:
    item = w.item()
    resp = authed.get(f"/api/v1/items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert resp.headers["etag"].strip('"') == "1"
    assert body["series_title"] == w.series.title
    assert body["raw_text"] == "do it"


@pytest.mark.fr("FR-45")
def test_item_history_lists_events_in_order(authed: TestClient, w: World) -> None:
    item = w.item()
    make_event(w.db, item, action=ItemAction.CREATED, actor_type=ActorType.HOST)
    make_event(w.db, item, action=ItemAction.UPDATED, actor_type=ActorType.HOST)
    resp = authed.get(f"/api/v1/items/{item.id}/history")
    assert resp.status_code == 200
    events = resp.json()
    assert sorted(e["action"] for e in events) == ["created", "updated"]
    assert all(e["actor_type"] == "host" for e in events)


@pytest.mark.fr("FR-44")
def test_series_items_filter_by_status(authed: TestClient, w: World) -> None:
    pending = w.item(status=ItemStatus.PENDING, sent_at=NOW)
    w.item()
    resp = authed.get(f"/api/v1/series/{w.series.id}/items", params={"status": "pending"})
    assert resp.status_code == 200
    assert ids(resp) == {str(pending.id)}
    assert resp.json()["total"] == 1


@pytest.mark.fr("FR-44")
def test_series_items_filter_by_multiple_statuses(authed: TestClient, w: World) -> None:
    a = w.item(status=ItemStatus.PENDING, sent_at=NOW)
    b = w.item()
    resp = authed.get(
        f"/api/v1/series/{w.series.id}/items", params={"status": ["pending", "draft"]}
    )
    assert ids(resp) == {str(a.id), str(b.id)}


@pytest.mark.fr("FR-44")
def test_series_items_filter_by_meeting(authed: TestClient, w: World) -> None:
    other = make_meeting(w.db, w.series, planned_start=NOW)
    mine = w.item()
    from tests.factories import make_item

    make_item(w.db, other)
    resp = authed.get(
        f"/api/v1/series/{w.series.id}/items", params={"meeting_id": str(w.meeting.id)}
    )
    assert ids(resp) == {str(mine.id)}


@pytest.mark.fr("FR-44")
def test_series_items_filter_by_assignee(authed: TestClient, w: World) -> None:
    mine = w.item(assignee_person_id=w.person.id)
    w.item()
    resp = authed.get(
        f"/api/v1/series/{w.series.id}/items", params={"assignee_person_id": str(w.person.id)}
    )
    assert ids(resp) == {str(mine.id)}


@pytest.mark.fr("FR-44")
def test_series_items_filter_overdue(authed: TestClient, w: World) -> None:
    late = w.item(status=ItemStatus.PENDING, sent_at=NOW, due_date=TODAY - timedelta(days=1))
    w.item(status=ItemStatus.PENDING, sent_at=NOW, due_date=TODAY)
    w.item(status=ItemStatus.PENDING, sent_at=NOW, due_date=TODAY + timedelta(days=3))
    resp = authed.get(f"/api/v1/series/{w.series.id}/items", params={"overdue": "true"})
    assert ids(resp) == {str(late.id)}
    assert resp.json()["items"][0]["is_overdue"] is True


@pytest.mark.fr("FR-44")
def test_series_items_filters_combine(authed: TestClient, w: World) -> None:
    hit = w.item(status=ItemStatus.PENDING, sent_at=NOW, assignee_person_id=w.person.id)
    w.item(status=ItemStatus.PENDING, sent_at=NOW)
    w.item(assignee_person_id=w.person.id)
    resp = authed.get(
        f"/api/v1/series/{w.series.id}/items",
        params={"status": "pending", "assignee_person_id": str(w.person.id)},
    )
    assert ids(resp) == {str(hit.id)}


@pytest.mark.fr("FR-44")
def test_series_items_pagination(authed: TestClient, w: World) -> None:
    for _ in range(3):
        w.item()
    body = authed.get(f"/api/v1/series/{w.series.id}/items", params={"limit": 2}).json()
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert body["limit"] == 2


@pytest.mark.fr("FR-44")
def test_on_time_share_30d_matches_srs_example(authed: TestClient, w: World) -> None:
    def done(due_days_ago: int, done_days_ago: int) -> None:
        w.item(
            status=ItemStatus.DONE,
            sent_at=NOW - timedelta(days=40),
            due_date=TODAY - timedelta(days=due_days_ago),
            done_at=NOW - timedelta(days=done_days_ago),
        )

    done(due_days_ago=5, done_days_ago=6)
    done(due_days_ago=10, done_days_ago=10)
    done(due_days_ago=8, done_days_ago=7)
    w.item(
        status=ItemStatus.CANCELLED,
        sent_at=NOW - timedelta(days=40),
        due_date=TODAY - timedelta(days=9),
        cancelled_at=NOW - timedelta(days=9),
    )
    done(due_days_ago=45, done_days_ago=46)
    w.item(due_date=TODAY - timedelta(days=3))
    resp = authed.get(f"/api/v1/series/{w.series.id}/items")
    assert resp.status_code == 200
    assert resp.json()["on_time_share_30d"] == pytest.approx(2 / 3, abs=1e-3)


@pytest.mark.fr("FR-44")
def test_on_time_share_is_null_without_items(authed: TestClient, w: World) -> None:
    resp = authed.get(f"/api/v1/series/{w.series.id}/items")
    assert resp.json()["on_time_share_30d"] is None


@pytest.mark.fr("FR-44")
def test_on_time_share_respects_assignee_filter(authed: TestClient, w: World) -> None:
    other = make_person(w.db, w.user, w.org)
    kw = {"status": ItemStatus.DONE, "sent_at": NOW - timedelta(days=20)}
    due = TODAY - timedelta(days=5)
    w.item(assignee_person_id=w.person.id, due_date=due, done_at=NOW - timedelta(days=6), **kw)
    w.item(assignee_person_id=other.id, due_date=due, done_at=NOW - timedelta(days=1), **kw)
    resp = authed.get(
        f"/api/v1/series/{w.series.id}/items", params={"assignee_person_id": str(w.person.id)}
    )
    assert resp.json()["on_time_share_30d"] == 1.0
