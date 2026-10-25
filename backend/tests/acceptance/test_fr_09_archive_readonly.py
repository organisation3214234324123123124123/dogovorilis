"""Архивная серия - только чтение для всех, включая исполнителей по ссылке (FR-09).

Изменение архивной серии даёт 409 SERIES_ARCHIVED. Исполнителю по ссылке тоже 409,
страница по ссылке открывается на чтение.
"""

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.modules.gaps.models import GapType
from app.modules.items.models import Item, ItemStatus
from app.modules.series.models import AgendaItem, MeetingStatus, SeriesStatus
from tests.factories import World, add_member, make_access_link, make_gap, make_participant

pytestmark = pytest.mark.fr("FR-09")

API = "/api/v1"
FUTURE = date(2099, 1, 1)
TOKEN = "tok-" + "b" * 28
ARCHIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def archive_in_db(world: World) -> None:
    world.meeting.status = MeetingStatus.LIVE
    world.series.status = SeriesStatus.ARCHIVED
    world.series.archived_at = ARCHIVED_AT
    world.db.flush()


def assert_archived(resp) -> None:
    assert resp.status_code == 409
    assert resp.json()["code"] == "SERIES_ARCHIVED"


def reload(world: World, obj):
    world.db.expire_all()
    return world.db.get(type(obj), obj.id)


def panel_item(world: World, **kw) -> Item:
    add_member(world.db, world.series, world.person)
    make_participant(world.db, world.meeting, world.person)
    kw.setdefault("assignee_person_id", world.person.id)
    kw.setdefault("due_date", FUTURE)
    kw.setdefault("status", ItemStatus.ACCEPTED)
    kw.setdefault("sent_at", datetime.now(UTC) - timedelta(days=1))
    return world.item(**kw)


def test_archived_agenda_patch_rejected(authed: TestClient, world: World) -> None:
    row = AgendaItem(meeting_id=world.meeting.id, position=1, title="Тема")
    world.db.add(row)
    world.db.flush()
    archive_in_db(world)
    assert_archived(authed.patch(f"{API}/agenda/{row.id}", json={"title": "Другая"}))
    assert reload(world, row).title == "Тема"


def test_archived_item_patch_rejected(authed: TestClient, world: World) -> None:
    item = panel_item(world, what="Исходно")
    archive_in_db(world)
    resp = authed.patch(
        f"{API}/items/{item.id}", json={"what": "Новое"}, headers={"If-Match": str(item.version)}
    )
    assert_archived(resp)
    assert reload(world, item).what == "Исходно"


def test_archived_item_delete_rejected(authed: TestClient, world: World) -> None:
    item = panel_item(world, status=ItemStatus.DRAFT, sent_at=None)
    archive_in_db(world)
    resp = authed.delete(f"{API}/items/{item.id}", headers={"If-Match": str(item.version)})
    assert_archived(resp)
    assert reload(world, item) is not None


def test_archived_item_action_rejected(authed: TestClient, world: World) -> None:
    item = panel_item(world, status=ItemStatus.ACCEPTED)
    archive_in_db(world)
    resp = authed.post(
        f"{API}/items/{item.id}/actions",
        json={"action": "done"},
        headers={"If-Match": str(item.version)},
    )
    assert_archived(resp)
    assert reload(world, item).status == ItemStatus.ACCEPTED


def test_archived_accept_in_meeting_rejected(authed: TestClient, world: World) -> None:
    item = panel_item(world, status=ItemStatus.DRAFT, sent_at=None, what="Подготовить отчёт")
    archive_in_db(world)
    resp = authed.post(
        f"{API}/items/{item.id}/accept-in-meeting",
        json={"value": True},
        headers={"If-Match": str(item.version)},
    )
    assert_archived(resp)
    assert reload(world, item).accepted_in_meeting is not True


def test_archived_gap_dismiss_rejected(authed: TestClient, world: World) -> None:
    item = panel_item(world, status=ItemStatus.DRAFT, sent_at=None)
    gap = make_gap(world.db, item, type=GapType.NO_DUE)
    archive_in_db(world)
    assert_archived(authed.post(f"{API}/items/{item.id}/gaps/{gap.id}/dismiss"))
    assert reload(world, gap).dismissed_at is None


def link_headers(client: TestClient) -> dict[str, str]:
    csrf = client.get(f"{API}/p/{TOKEN}").json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


@pytest.fixture
def linked(world: World):
    make_access_link(
        world.db,
        world.person,
        world.meeting,
        token_hash=hashlib.sha256(TOKEN.encode()).digest(),
    )


@pytest.mark.parametrize(
    ("action", "status", "body"),
    [
        ("accept", ItemStatus.PENDING, None),
        ("reschedule", ItemStatus.PENDING, {"due_date": "2099-01-02"}),
        ("decline", ItemStatus.PENDING, {"comment": "не моё"}),
        ("done", ItemStatus.ACCEPTED, None),
    ],
)
def test_archived_assignee_link_actions_rejected(
    client: TestClient, world: World, linked, action: str, status: ItemStatus, body
) -> None:
    item = panel_item(world, status=status)
    archive_in_db(world)
    resp = client.post(
        f"{API}/p/{TOKEN}/items/{item.id}/{action}", json=body, headers=link_headers(client)
    )
    assert_archived(resp)
    assert reload(world, item).status == status


def test_archived_link_page_stays_readable(client: TestClient, world: World, linked) -> None:
    item = panel_item(world, status=ItemStatus.PENDING)
    archive_in_db(world)
    resp = client.get(f"{API}/p/{TOKEN}")
    assert resp.status_code == 200
    assert str(item.id) in [i["id"] for i in resp.json()["items"]]


def test_archive_during_live_meeting_rejected(authed: TestClient, world: World) -> None:
    world.meeting.status = MeetingStatus.LIVE
    world.db.flush()
    resp = authed.post(f"{API}/series/{world.series.id}/archive")
    assert resp.status_code == 409
    assert resp.json()["code"] == "MEETING_ALREADY_LIVE"
    assert reload(world, world.series).status == SeriesStatus.ACTIVE


def test_repeated_archive_keeps_archived_at(authed: TestClient, world: World) -> None:
    world.series.status = SeriesStatus.ARCHIVED
    world.series.archived_at = ARCHIVED_AT
    world.db.flush()
    resp = authed.post(f"{API}/series/{world.series.id}/archive")
    assert resp.status_code == 200
    assert reload(world, world.series).archived_at == ARCHIVED_AT


# Спека: у каждой операции над архивной серией описан 409.

SPEC = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.yaml"
GUARDED = [
    ("delete", "/api/v1/series/{series_id}/members/{person_id}"),
    ("post", "/api/v1/meetings/{meeting_id}/agenda"),
    ("patch", "/api/v1/meetings/{meeting_id}"),
    ("post", "/api/v1/meetings/{meeting_id}/items"),
    ("patch", "/api/v1/agenda/{agenda_id}"),
    ("patch", "/api/v1/items/{item_id}"),
    ("delete", "/api/v1/items/{item_id}"),
    ("post", "/api/v1/items/{item_id}/actions"),
    ("post", "/api/v1/items/{item_id}/accept-in-meeting"),
    ("post", "/api/v1/items/{item_id}/gaps/{gap_id}/dismiss"),
    ("post", "/api/v1/p/{token}/items/{item_id}/accept"),
    ("post", "/api/v1/p/{token}/items/{item_id}/reschedule"),
    ("post", "/api/v1/p/{token}/items/{item_id}/decline"),
    ("post", "/api/v1/p/{token}/items/{item_id}/done"),
    ("post", "/api/v1/series/{series_id}/archive"),
]


@pytest.mark.parametrize(("method", "path"), GUARDED, ids=lambda v: v)
def test_contract_lists_409_for_archived_series_edits(method: str, path: str) -> None:
    paths = yaml.safe_load(SPEC.read_text(encoding="utf-8"))["paths"]
    assert "409" in paths[path][method]["responses"]
