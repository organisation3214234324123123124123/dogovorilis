import uuid

import pytest
from fastapi.testclient import TestClient

from app.models import Gap
from app.modules.gaps.models import GapSource, GapType
from tests.acceptance.helpers_items import (
    API,
    clock,  # noqa: F401
    events,
    if_match,
    make_ctx,
    open_gap_types,
    problem_code,
    ws_events,
)
from tests.factories import World, make_gap

pytestmark = [
    pytest.mark.fr("FR-24"),
    pytest.mark.usefixtures("clock"),
]


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


def test_dismiss_sets_dismissed_at_and_hides_gap(authed: TestClient, world: World, case) -> None:
    _, item, gap = case
    resp = dismiss(authed, item, gap.id)
    assert resp.status_code in (200, 204)
    assert reload_gap(world, gap).dismissed_at is not None
    assert reload_gap(world, gap).resolved_at is None
    assert authed.get(f"{API}/items/{item.id}").json()["gaps"] == []


def test_dismiss_sends_gaps_updated(authed: TestClient, world: World, case) -> None:
    _, item, gap = case
    frames = ws_events(
        authed, world.meeting.id, lambda: dismiss(authed, item, gap.id), ("gaps.updated",)
    )
    assert "gaps.updated" in {f.get("type") for f in frames}


def test_dismissed_type_does_not_return_after_edit(authed: TestClient, world: World, case) -> None:
    _, item, gap = case
    dismiss(authed, item, gap.id)
    world.db.refresh(item)
    resp = authed.patch(
        f"{API}/items/{item.id}", json={"done_criteria": "Файл в папке"}, headers=if_match(item)
    )
    assert resp.status_code == 200
    assert "no_due" not in {g["type"] for g in resp.json()["gaps"]}
    assert "no_due" not in open_gap_types(world.db, item.id)


def test_same_type_on_other_item_is_shown(authed: TestClient, world: World, case) -> None:
    ctx, item, gap = case
    other = world.item(what="Подготовить договор", assignee_person_id=ctx.anna.id)
    dismiss(authed, item, gap.id)
    resp = authed.patch(
        f"{API}/items/{other.id}", json={"done_criteria": "Подписан"}, headers=if_match(other)
    )
    assert "no_due" in {g["type"] for g in resp.json()["gaps"]}


def test_dismiss_writes_gap_dismissed_event(authed: TestClient, world: World, case) -> None:
    _, item, gap = case
    dismiss(authed, item, gap.id)
    [event] = events(world.db, "gap_dismissed", item_id=item.id)
    assert event.props["type"] == "no_due"
    assert event.props["source"] == "rule"


def test_repeated_dismiss_does_not_write_second_event(
    authed: TestClient, world: World, case
) -> None:
    _, item, gap = case
    first = dismiss(authed, item, gap.id)
    second = dismiss(authed, item, gap.id)
    assert first.status_code in (200, 204)
    assert second.status_code == 200
    assert len(events(world.db, "gap_dismissed", item_id=item.id)) == 1


def test_repeated_dismiss_keeps_first_dismissed_at(authed: TestClient, world: World, case) -> None:
    _, item, gap = case
    dismiss(authed, item, gap.id)
    first = reload_gap(world, gap).dismissed_at
    assert first is not None
    dismiss(authed, item, gap.id)
    assert reload_gap(world, gap).dismissed_at == first


def test_gap_from_another_item_is_404(authed: TestClient, world: World, case) -> None:
    _, item, _ = case
    other_gap = make_gap(world.db, world.item(), type=GapType.NO_ASSIGNEE)
    assert dismiss(authed, item, other_gap.id).status_code == 404
    assert reload_gap(world, other_gap).dismissed_at is None


def test_unknown_gap_is_404(authed: TestClient, world: World, case) -> None:
    _, item, _ = case
    assert dismiss(authed, item, uuid.uuid4()).status_code == 404


def test_dismissed_no_due_does_not_allow_accept_without_date(
    authed: TestClient, world: World, case
) -> None:
    _, item, gap = case
    dismiss(authed, item, gap.id)
    world.db.refresh(item)
    resp = authed.post(
        f"{API}/items/{item.id}/accept-in-meeting", json={"value": True}, headers=if_match(item)
    )
    assert resp.status_code == 422
    assert problem_code(resp) in ("GAPS_OPEN", "VALIDATION_ERROR")
