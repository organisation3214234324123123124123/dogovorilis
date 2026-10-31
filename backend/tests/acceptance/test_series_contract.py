from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

from app.models import SeriesMember
from app.modules.items.models import ItemStatus
from app.modules.series.models import SeriesStatus
from tests.conftest import login_as
from tests.factories import (
    World,
    add_member,
    events_named,
    make_item,
    make_meeting,
    make_organization,
    make_person,
    make_series,
)

API = "/api/v1"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)
SENT = NOW - timedelta(days=5)
CREATE = {"title": "Планёрка", "timezone": "Europe/Moscow", "planned_duration_min": 60}


def member_body(**kw) -> dict:
    return {
        "full_name": "Иван Петров",
        "email": "ivan@example.com",
        "organization_name": "ООО Ромашка",
        **kw,
    }


@pytest.fixture
def archived(world: World):
    other = make_series(world.db, world.user, title="Архивная")
    other.status = SeriesStatus.ARCHIVED
    world.db.flush()
    return other


# --- GET /series?status ---


@pytest.mark.fr("FR-09")
def test_series_list_default_is_active_only(authed: TestClient, world: World, archived) -> None:
    ids = {s["id"] for s in authed.get(f"{API}/series").json()}
    assert ids == {str(world.series.id)}


@pytest.mark.fr("FR-09")
def test_series_list_status_archived(authed: TestClient, world: World, archived) -> None:
    ids = {s["id"] for s in authed.get(f"{API}/series", params={"status": "archived"}).json()}
    assert ids == {str(archived.id)}


@pytest.mark.fr("FR-09")
def test_series_list_status_active_explicit(authed: TestClient, world: World, archived) -> None:
    resp = authed.get(f"{API}/series", params={"status": "active"})
    assert {s["id"] for s in resp.json()} == {str(world.series.id)}


@pytest.mark.fr("FR-09")
def test_series_list_unknown_status_is_422(authed: TestClient, world: World) -> None:
    resp = authed.get(f"{API}/series", params={"status": "deleted"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


# --- Участник: organization ---


def listed_members(authed: TestClient, world: World) -> dict:
    other_org = make_organization(world.db, world.user, name="Партнёры")
    outsider = make_person(world.db, world.user, other_org, full_name="Пётр")
    add_member(world.db, world.series, world.person)
    add_member(world.db, world.series, outsider)
    rows = authed.get(f"{API}/series/{world.series.id}/members").json()
    return {
        "own": rows[0] if rows[0]["person_id"] == str(world.person.id) else rows[1],
        "other": rows[0] if rows[0]["person_id"] == str(outsider.id) else rows[1],
        "org": other_org,
    }


@pytest.mark.fr("FR-05")
def test_member_has_nested_organization(authed: TestClient, world: World) -> None:
    got = listed_members(authed, world)
    assert got["own"]["organization"] == {
        "id": str(world.org.id),
        "name": world.org.name,
        "is_own": True,
    }


@pytest.mark.fr("FR-05")
def test_member_of_other_organization_is_not_own(authed: TestClient, world: World) -> None:
    got = listed_members(authed, world)
    assert got["other"]["organization"] == {
        "id": str(got["org"].id),
        "name": "Партнёры",
        "is_own": False,
    }


@pytest.mark.fr("FR-05")
def test_member_has_no_flat_organization_fields(authed: TestClient, world: World) -> None:
    got = listed_members(authed, world)
    assert {"organization_id", "organization_name", "is_own_side"}.isdisjoint(got["own"])


@pytest.mark.fr("FR-05")
def test_member_has_no_open_items(authed: TestClient, world: World) -> None:
    got = listed_members(authed, world)
    assert "open_items" not in got["own"]


@pytest.mark.fr("FR-05")
def test_added_member_response_has_organization(authed: TestClient, world: World) -> None:
    resp = authed.post(f"{API}/series/{world.series.id}/members", json=member_body())
    assert resp.status_code == 201
    body = resp.json()
    assert body["organization"]["name"] == "ООО Ромашка"
    assert body["organization"]["is_own"] is False
    assert "is_own_side" not in body


# --- open_items_count: pending + accepted + disputed ---


def seed(world: World, statuses: list[ItemStatus]) -> None:
    meeting = make_meeting(world.db, world.series)
    for status in statuses:
        extra = {}
        if status == ItemStatus.DONE:
            extra["done_at"] = SENT
        if status == ItemStatus.CANCELLED:
            extra["cancelled_at"] = SENT
        if status != ItemStatus.DRAFT:
            extra["sent_at"] = SENT
        make_item(world.db, meeting, status=status, **extra)


ALL = [
    ItemStatus.PENDING,
    ItemStatus.ACCEPTED,
    ItemStatus.DISPUTED,
    ItemStatus.DRAFT,
    ItemStatus.DONE,
    ItemStatus.CANCELLED,
]


@pytest.mark.fr("FR-04")
def test_open_items_count_includes_disputed_in_detail(authed: TestClient, world: World) -> None:
    seed(world, ALL)
    assert authed.get(f"{API}/series/{world.series.id}").json()["open_items_count"] == 3


@pytest.mark.fr("FR-04")
def test_open_items_count_includes_disputed_in_list(authed: TestClient, world: World) -> None:
    seed(world, [ItemStatus.DISPUTED, ItemStatus.DISPUTED, ItemStatus.PENDING])
    row = authed.get(f"{API}/series").json()[0]
    assert row["open_items_count"] == 3


# --- PATCH capture_mode, timezone обязателен, состава при создании нет ---


@pytest.mark.fr("FR-04")
def test_patch_accepts_capture_mode(authed: TestClient, world: World) -> None:
    resp = authed.patch(f"{API}/series/{world.series.id}", json={"capture_mode": "manual"})
    assert resp.status_code == 200
    assert resp.json()["capture_mode"] == "manual"


@pytest.mark.fr("FR-04")
def test_patch_unknown_capture_mode_is_422(authed: TestClient, world: World) -> None:
    resp = authed.patch(f"{API}/series/{world.series.id}", json={"capture_mode": "telepathy"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.fr("FR-04")
def test_create_requires_timezone(authed: TestClient) -> None:
    body = {k: v for k, v in CREATE.items() if k != "timezone"}
    resp = authed.post(f"{API}/series", json=body)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.fr("FR-04")
def test_create_ignores_members_and_adds_none(authed: TestClient) -> None:
    members = [{"full_name": "Свой", "email": "own@x.ru", "organization_name": "Мы"}]
    resp = authed.post(f"{API}/series", json={**CREATE, "members": members})
    assert resp.status_code == 201
    listed = authed.get(f"{API}/series/{resp.json()['id']}/members").json()
    assert listed == []


@pytest.mark.fr("FR-04")
def test_series_created_event_has_no_members(authed: TestClient, world: World) -> None:
    members = [{"full_name": "Свой", "email": "own@x.ru", "organization_name": "Мы"}]
    resp = authed.post(f"{API}/series", json={**CREATE, "members": members})
    assert resp.status_code == 201
    [event] = events_named(world.db, "series_created", series_id=resp.json()["id"])
    assert event.props["members_count"] == 0


# --- GET /series/{id}/items: страницы, без черновиков ---


@pytest.fixture
def w(world: World, client: TestClient):
    world.db.add(SeriesMember(series_id=world.series.id, person_id=world.person.id))
    world.db.flush()
    with freeze_time(NOW):
        login_as(client, world.user)
        yield world


def items_url(w: World) -> str:
    return f"{API}/series/{w.series.id}/items"


def sent_items(w: World, count: int) -> list:
    return [w.item(status=ItemStatus.PENDING, sent_at=SENT) for _ in range(count)]


@pytest.mark.fr("FR-44")
def test_items_page_size_limits_rows_and_total_stays(client: TestClient, w: World) -> None:
    sent_items(w, 3)
    body = client.get(items_url(w), params={"page_size": 2}).json()
    assert len(body["items"]) == 2
    assert body["total"] == 3


@pytest.mark.fr("FR-44")
def test_items_second_page_has_remainder(client: TestClient, w: World) -> None:
    sent_items(w, 3)
    first = client.get(items_url(w), params={"page": 1, "page_size": 2}).json()
    second = client.get(items_url(w), params={"page": 2, "page_size": 2}).json()
    assert len(second["items"]) == 1
    assert {i["id"] for i in first["items"]}.isdisjoint(i["id"] for i in second["items"])


@pytest.mark.fr("FR-44")
def test_items_page_beyond_end_is_empty(client: TestClient, w: World) -> None:
    sent_items(w, 2)
    body = client.get(items_url(w), params={"page": 5, "page_size": 2}).json()
    assert body["items"] == []
    assert body["total"] == 2


@pytest.mark.fr("FR-44")
def test_items_default_page_size_is_50(client: TestClient, w: World) -> None:
    sent_items(w, 51)
    body = client.get(items_url(w)).json()
    assert len(body["items"]) == 50
    assert body["total"] == 51


@pytest.mark.fr("FR-44")
def test_items_page_size_100_accepted(client: TestClient, w: World) -> None:
    assert client.get(items_url(w), params={"page_size": 100}).status_code == 200


@pytest.mark.fr("FR-44")
@pytest.mark.parametrize("params", [{"page_size": 101}, {"page_size": 0}, {"page": 0}], ids=str)
def test_items_paging_bounds_rejected(client: TestClient, w: World, params: dict) -> None:
    resp = client.get(items_url(w), params=params)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.fr("FR-44")
def test_items_response_has_on_time_rate_30d(client: TestClient, w: World) -> None:
    w.item(
        status=ItemStatus.DONE,
        sent_at=SENT,
        due_date=TODAY - timedelta(days=2),
        done_at=NOW - timedelta(days=3),
    )
    w.item(
        status=ItemStatus.DONE,
        sent_at=SENT,
        due_date=TODAY - timedelta(days=4),
        done_at=NOW - timedelta(days=1),
    )
    body = client.get(items_url(w)).json()
    assert body["on_time_rate_30d"] == pytest.approx(0.5)
    assert "on_time_share_30d" not in body


@pytest.mark.fr("FR-44")
def test_on_time_rate_is_null_without_data(client: TestClient, w: World) -> None:
    body = client.get(items_url(w)).json()
    assert "on_time_rate_30d" in body
    assert body["on_time_rate_30d"] is None


@pytest.mark.fr("FR-44")
def test_items_list_has_no_drafts(client: TestClient, w: World) -> None:
    sent = sent_items(w, 1)[0]
    w.item(status=ItemStatus.DRAFT)
    body = client.get(items_url(w)).json()
    assert [i["id"] for i in body["items"]] == [str(sent.id)]
    assert body["total"] == 1


@pytest.mark.fr("FR-44")
def test_items_meeting_id_filter_is_gone(client: TestClient, w: World) -> None:
    other = make_meeting(w.db, w.series, planned_start=NOW)
    make_item(w.db, other, status=ItemStatus.PENDING, sent_at=SENT)
    w.item(status=ItemStatus.PENDING, sent_at=SENT)
    resp = client.get(items_url(w), params={"meeting_id": str(w.meeting.id)})
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


def rate(client: TestClient, w: World, **params) -> float | None:
    resp = client.get(items_url(w), params=params)
    assert resp.status_code == 200
    return resp.json().get("on_time_rate_30d", "missing")


@pytest.mark.fr("FR-44")
def test_rate_window_excludes_day_30_ago(client: TestClient, w: World) -> None:
    kw = {"status": ItemStatus.DONE, "sent_at": NOW - timedelta(days=60)}
    w.item(due_date=TODAY - timedelta(days=29), done_at=NOW - timedelta(days=29), **kw)
    w.item(due_date=TODAY - timedelta(days=30), done_at=NOW - timedelta(days=29), **kw)
    assert rate(client, w) == 1.0


@pytest.mark.fr("FR-44")
def test_rate_compares_done_date_in_series_timezone(client: TestClient, w: World) -> None:
    w.series.timezone = "Pacific/Auckland"
    w.db.flush()
    due = TODAY - timedelta(days=3)
    w.item(
        status=ItemStatus.DONE,
        sent_at=SENT,
        due_date=due,
        done_at=datetime(2026, 10, 17, 12, 30, tzinfo=UTC),
    )
    assert rate(client, w) == 0.0


@pytest.mark.fr("FR-44")
def test_rate_ignores_cancelled_and_future_due(client: TestClient, w: World) -> None:
    w.item(
        status=ItemStatus.DONE,
        sent_at=SENT,
        due_date=TODAY - timedelta(days=2),
        done_at=NOW - timedelta(days=3),
    )
    w.item(
        status=ItemStatus.CANCELLED,
        sent_at=SENT,
        due_date=TODAY - timedelta(days=2),
        cancelled_at=NOW - timedelta(days=2),
    )
    w.item(status=ItemStatus.PENDING, sent_at=SENT, due_date=TODAY + timedelta(days=3))
    assert rate(client, w) == 1.0


@pytest.mark.fr("FR-04")
def test_patch_audio_capture_mode_is_422(authed: TestClient, world: World) -> None:
    resp = authed.patch(f"{API}/series/{world.series.id}", json={"capture_mode": "audio"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.fr("FR-44")
def test_items_status_draft_filter_returns_no_drafts(client: TestClient, w: World) -> None:
    w.item(status=ItemStatus.DRAFT)
    body = client.get(items_url(w), params={"status": "draft"}).json()
    assert body["items"] == []
    assert body["total"] == 0
