import uuid

import pytest
from fastapi.testclient import TestClient

from app.models import Item
from app.modules.gaps.models import GapType
from tests.acceptance.helpers_items import (
    API,
    NOW,
    clock,  # noqa: F401
    if_match,
    make_ctx,
    outsider,
    problem_code,
    ws_events,
)
from tests.conftest import login_as
from tests.factories import World, make_gap

pytestmark = [
    pytest.mark.fr("FR-13"),
    pytest.mark.usefixtures("clock"),
]


@pytest.fixture
def draft(world: World):
    ctx = make_ctx(world)
    item = world.item(what="Старое", raw_text="Старое")
    return ctx, item


def patch(client: TestClient, item: Item, body: dict, headers: dict | None = None):
    return client.patch(
        f"{API}/items/{item.id}", json=body, headers=if_match(item) if headers is None else headers
    )


def reload(world: World, item: Item) -> Item:
    world.db.expire_all()
    return world.db.get(Item, item.id)


def test_patch_what_bumps_version_and_marks_manual(authed: TestClient, world: World, draft) -> None:
    _, item = draft
    resp = patch(authed, item, {"what": "Новое"})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["what"], body["version"]) == ("Новое", 2)
    assert "what" in body["manual_fields"]


def test_patch_assignee_and_due_mark_manual_fields(authed: TestClient, world: World, draft) -> None:
    ctx, item = draft
    resp = patch(authed, item, {"assignee_person_id": str(ctx.ivan.id), "due_date": "2026-10-30"})
    assert resp.status_code == 200
    assert {"assignee_person_id", "due_date"} <= set(resp.json()["manual_fields"])


def test_stale_if_match_is_409_and_item_unchanged(authed: TestClient, world: World, draft) -> None:
    _, item = draft
    assert patch(authed, item, {"what": "Первое"}).status_code == 200
    resp = patch(authed, item, {"what": "Второе"}, headers={"If-Match": "1"})
    assert resp.status_code == 409
    assert problem_code(resp) == "VERSION_CONFLICT"
    stored = reload(world, item)
    assert (stored.what, stored.version) == ("Первое", 2)


def test_assignee_outside_series_is_422_not_a_participant(
    authed: TestClient, world: World, draft
) -> None:
    _, item = draft
    resp = patch(authed, item, {"assignee_person_id": str(outsider(world).id)})
    assert resp.status_code == 422
    assert problem_code(resp) == "NOT_A_PARTICIPANT"


def test_unknown_assignee_is_422_not_a_participant(authed: TestClient, world: World, draft) -> None:
    _, item = draft
    resp = patch(authed, item, {"assignee_person_id": str(uuid.uuid4())})
    assert resp.status_code == 422
    assert problem_code(resp) == "NOT_A_PARTICIPANT"


def test_due_date_before_today_is_422_due_in_past(authed: TestClient, world: World, draft) -> None:
    _, item = draft
    resp = patch(authed, item, {"due_date": "2026-10-14"})
    assert resp.status_code == 422
    assert problem_code(resp) == "DUE_IN_PAST"


def test_due_date_today_is_accepted(authed: TestClient, world: World, draft) -> None:
    _, item = draft
    resp = patch(authed, item, {"due_date": "2026-10-15"})
    assert resp.status_code == 200
    assert resp.json()["due_date"] == "2026-10-15"


def test_today_is_counted_in_series_timezone(authed: TestClient, world: World, draft) -> None:
    from freezegun import freeze_time

    _, item = draft
    with freeze_time(NOW.replace(day=14, hour=22), tick=True):
        login_as(authed, world.user)
        ok = patch(authed, item, {"due_date": "2026-10-15"})
        past = patch(
            authed,
            item,
            {"due_date": "2026-10-14"},
            headers={"If-Match": str(ok.json()["version"])},
        )
    assert ok.status_code == 200
    assert past.status_code == 422
    assert problem_code(past) == "DUE_IN_PAST"


@pytest.mark.parametrize(
    ("field", "limit"), [("what", 500), ("done_criteria", 500), ("due_text", 100)]
)
def test_field_at_limit_is_accepted(
    authed: TestClient, world: World, draft, field: str, limit: int
) -> None:
    _, item = draft
    assert patch(authed, item, {field: "а" * limit}).status_code == 200


def test_edit_recalculates_gaps(authed: TestClient, world: World, draft) -> None:
    _, item = draft
    make_gap(world.db, item, type=GapType.NO_DUE)
    resp = patch(authed, item, {"due_date": "2026-10-30"})
    assert "no_due" not in {g["type"] for g in resp.json()["gaps"]}


def test_edit_sends_item_updated_and_gaps_updated(authed: TestClient, world: World, draft) -> None:
    _, item = draft
    frames = ws_events(
        authed,
        world.meeting.id,
        lambda: patch(authed, item, {"due_date": "2026-10-30"}),
        ("item.updated", "gaps.updated"),
    )
    assert {"item.updated", "gaps.updated"} <= {f.get("type") for f in frames}


def test_draft_date_change_does_not_count_as_reschedule(
    authed: TestClient, world: World, draft
) -> None:
    _, item = draft
    first = patch(authed, item, {"due_date": "2026-10-20"})
    item.version = first.json()["version"]
    patch(authed, item, {"due_date": "2026-10-27"})
    assert reload(world, item).reschedule_count == 0


def make_item_for(world: World, meeting) -> Item:
    from tests.factories import make_item

    return make_item(world.db, meeting)
