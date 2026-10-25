from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import SeriesMember
from app.modules.items.models import ItemStatus
from app.modules.series.models import SeriesStatus
from tests.factories import (
    World,
    add_member,
    make_person,
    make_series,
    make_user,
)

pytestmark = pytest.mark.fr("FR-09")


def archive(client: TestClient, series):
    return client.post(f"/api/v1/series/{series.id}/archive")


def test_archive_sets_status(authed: TestClient, world: World) -> None:
    resp = archive(authed, world.series)
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    world.db.refresh(world.series)
    assert world.series.status == SeriesStatus.ARCHIVED


def test_archived_series_cannot_start_meeting(authed: TestClient, world: World) -> None:
    assert archive(authed, world.series).status_code == 200
    resp = authed.post(f"/api/v1/series/{world.series.id}/meetings", json={})
    assert resp.status_code == 409
    assert resp.json()["code"] == "SERIES_ARCHIVED"


def test_archived_series_listed_with_status_and_readable(authed: TestClient, world: World) -> None:
    world.series.status = SeriesStatus.ARCHIVED
    world.db.flush()
    listed = {s["id"]: s for s in authed.get("/api/v1/series").json()}
    assert listed[str(world.series.id)]["status"] == "archived"
    assert authed.get(f"/api/v1/series/{world.series.id}").status_code == 200


def test_removed_member_keeps_items_untouched(authed: TestClient, world: World) -> None:
    add_member(world.db, world.series, world.person)
    item = world.item(
        assignee_person_id=world.person.id, status=ItemStatus.PENDING, due_date="2099-01-01"
    )
    resp = authed.delete(f"/api/v1/series/{world.series.id}/members/{world.person.id}")
    assert resp.status_code == 204
    gone = world.db.scalar(select(SeriesMember).where(SeriesMember.person_id == world.person.id))
    assert gone is None
    world.db.refresh(item)
    assert (item.assignee_person_id, item.status, str(item.due_date)) == (
        world.person.id,
        ItemStatus.PENDING,
        "2099-01-01",
    )


def test_removed_member_cannot_become_assignee(authed: TestClient, world: World) -> None:
    removed = make_person(world.db, world.user, world.org)
    add_member(world.db, world.series, removed)
    current = make_person(world.db, world.user, world.org)
    add_member(world.db, world.series, current)
    item = world.item(assignee_person_id=current.id, status=ItemStatus.PENDING)
    assert (
        authed.delete(f"/api/v1/series/{world.series.id}/members/{removed.id}").status_code == 204
    )
    resp = authed.post(
        f"/api/v1/items/{item.id}/actions",
        json={"action": "reassign", "assignee_person_id": str(removed.id)},
        headers={"If-Match": str(item.version)},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "NOT_A_PARTICIPANT"


def test_archive_foreign_series_is_404(authed: TestClient, world: World) -> None:
    foreign = make_series(world.db, make_user(world.db))
    resp = archive(authed, foreign)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# 409 SERIES_ARCHIVED: изменение архивной серии и новая встреча в ней (FR-09).
MEMBER = {
    "full_name": "Иван Петров",
    "email": "ivan@example.com",
    "organization_name": "Моя команда",
}


def archived(world: World) -> None:
    world.series.status = SeriesStatus.ARCHIVED
    world.db.flush()


def assert_archived_409(resp) -> None:
    assert resp.status_code == 409
    assert resp.json()["code"] == "SERIES_ARCHIVED"


def test_archived_series_cannot_be_patched(authed: TestClient, world: World) -> None:
    archived(world)
    assert_archived_409(authed.patch(f"/api/v1/series/{world.series.id}", json={"title": "Новое"}))
    world.db.refresh(world.series)
    assert world.series.title != "Новое"


def test_archived_series_cannot_get_new_member(authed: TestClient, world: World) -> None:
    archived(world)
    assert_archived_409(authed.post(f"/api/v1/series/{world.series.id}/members", json=MEMBER))
    members = authed.get(f"/api/v1/series/{world.series.id}/members").json()
    assert all(m["email"] != MEMBER["email"] for m in members)


def test_active_series_still_accepts_patch_and_member(authed: TestClient, world: World) -> None:
    assert (
        authed.patch(f"/api/v1/series/{world.series.id}", json={"title": "Новое"}).status_code
        == 200
    )
    assert authed.post(f"/api/v1/series/{world.series.id}/members", json=MEMBER).status_code in (
        200,
        201,
    )


def test_archived_series_agenda_add_rejected(authed: TestClient, world: World) -> None:
    archived(world)
    resp = authed.post(f"/api/v1/meetings/{world.meeting.id}/agenda", json={"title": "Тема"})
    assert_archived_409(resp)


def test_archived_series_meeting_patch_rejected(authed: TestClient, world: World) -> None:
    archived(world)
    end = (world.meeting.planned_end + timedelta(minutes=30)).isoformat()
    assert_archived_409(
        authed.patch(f"/api/v1/meetings/{world.meeting.id}", json={"planned_end": end})
    )


def test_archived_series_item_create_rejected(authed: TestClient, world: World) -> None:
    archived(world)
    resp = authed.post(
        f"/api/v1/meetings/{world.meeting.id}/items", json={"raw_text": "Иван сделает отчёт"}
    )
    assert_archived_409(resp)


def test_archived_series_member_removal_rejected(authed: TestClient, world: World) -> None:
    add_member(world.db, world.series, world.person)
    archived(world)
    resp = authed.delete(f"/api/v1/series/{world.series.id}/members/{world.person.id}")
    assert_archived_409(resp)
