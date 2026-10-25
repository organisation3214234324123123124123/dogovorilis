from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.models import AccessLink, Item, MeetingParticipant
from app.modules.gaps.models import GapType
from app.modules.items.models import ItemStatus
from tests.acceptance.helpers_items import (
    API,
    clock,  # noqa: F401
    events,
    if_match,
    make_ctx,
    problem_code,
)
from tests.factories import World, make_gap

pytestmark = [
    pytest.mark.fr("FR-25"),
    pytest.mark.usefixtures("clock"),
]

DUE = date(2026, 10, 20)


def accept(client: TestClient, item, value: bool = True):
    return client.post(
        f"{API}/items/{item.id}/accept-in-meeting", json={"value": value}, headers=if_match(item)
    )


@pytest.fixture
def ready(world: World):
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=DUE)
    return ctx, item


def reload(world: World, item: Item) -> Item:
    world.db.expire_all()
    return world.db.get(Item, item.id)


def test_accept_sets_flag_and_keeps_draft(authed: TestClient, world: World, ready) -> None:
    _, item = ready
    resp = accept(authed, item)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["accepted_in_meeting"], body["status"]) == (True, "draft")


def test_accept_writes_event_with_due_changed(authed: TestClient, world: World, ready) -> None:
    _, item = ready
    accept(authed, item)
    [event] = events(world.db, "accepted_in_meeting", item_id=item.id)
    assert "due_changed" in event.props


def test_absent_assignee_is_422(authed: TestClient, world: World, ready) -> None:
    ctx, item = ready
    row = world.db.get(MeetingParticipant, (world.meeting.id, ctx.ivan.id))
    row.present = False
    world.db.flush()
    resp = accept(authed, item)
    assert resp.status_code == 422
    assert problem_code(resp) == "GAPS_OPEN"
    assert reload(world, item).accepted_in_meeting is False


def test_no_assignee_is_422(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item = world.item(due_date=DUE)
    resp = accept(authed, item)
    assert resp.status_code == 422
    assert problem_code(resp) in ("GAPS_OPEN", "VALIDATION_ERROR")


@pytest.mark.parametrize("due_text", [None, "на днях"])
def test_no_due_date_is_422(authed: TestClient, world: World, due_text) -> None:
    ctx = make_ctx(world)
    item = world.item(assignee_person_id=ctx.ivan.id, due_text=due_text)
    resp = accept(authed, item)
    assert resp.status_code == 422
    assert problem_code(resp) in ("GAPS_OPEN", "VALIDATION_ERROR")


@pytest.mark.parametrize(
    "gap_type",
    [GapType.NO_ASSIGNEE, GapType.NO_DUE, GapType.DUE_VAGUE, GapType.DUE_PAST],
)
def test_blocking_open_gap_is_422(authed: TestClient, world: World, ready, gap_type) -> None:
    _, item = ready
    make_gap(world.db, item, type=gap_type)
    resp = accept(authed, item)
    assert resp.status_code == 422
    assert problem_code(resp) == "GAPS_OPEN"


def test_vague_action_does_not_block(authed: TestClient, world: World, ready) -> None:
    _, item = ready
    make_gap(world.db, item, type=GapType.VAGUE_ACTION)
    resp = accept(authed, item)
    assert resp.status_code == 200
    assert resp.json()["accepted_in_meeting"] is True


def test_value_false_clears_flag(authed: TestClient, world: World, ready) -> None:
    _, item = ready
    first = accept(authed, item)
    item.version = first.json()["version"]
    resp = accept(authed, item, False)
    assert resp.status_code == 200
    assert resp.json()["accepted_in_meeting"] is False


def test_assignee_change_resets_flag(authed: TestClient, world: World, ready) -> None:
    ctx, item = ready
    first = accept(authed, item)
    resp = authed.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(ctx.anna.id)},
        headers={"If-Match": str(first.json()["version"])},
    )
    assert resp.status_code == 200
    assert resp.json()["accepted_in_meeting"] is False


def test_date_change_before_send_keeps_flag_and_reschedule_count(
    authed: TestClient, world: World, ready
) -> None:
    _, item = ready
    first = accept(authed, item)
    resp = authed.patch(
        f"{API}/items/{item.id}",
        json={"due_date": "2026-10-27"},
        headers={"If-Match": str(first.json()["version"])},
    )
    body = resp.json()
    assert (body["accepted_in_meeting"], body["due_date"], body["reschedule_count"]) == (
        True,
        "2026-10-27",
        0,
    )


def test_after_send_item_is_accepted_with_link(authed: TestClient, world: World, ready) -> None:
    ctx, item = ready
    accept(authed, item)
    assert authed.post(f"{API}/meetings/{world.meeting.id}/closure").status_code == 200
    sent = authed.post(f"{API}/meetings/{world.meeting.id}/send", json={"confirm_open_gaps": True})
    assert sent.status_code == 200
    stored = reload(world, item)
    assert stored.status == ItemStatus.ACCEPTED
    assert stored.accepted_at is not None
    links = world.db.query(AccessLink).filter_by(person_id=ctx.ivan.id).count()
    assert links == 1


def test_accept_in_sent_meeting_is_409_already_sent(authed: TestClient, world: World) -> None:
    from app.modules.series.models import MeetingStatus

    ctx = make_ctx(world, MeetingStatus.SENT)
    item = world.item(assignee_person_id=ctx.ivan.id, due_date=DUE)
    resp = accept(authed, item)
    assert resp.status_code == 409
    assert problem_code(resp) == "ALREADY_SENT"
