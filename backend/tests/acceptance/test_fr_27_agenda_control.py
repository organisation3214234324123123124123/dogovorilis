import pytest
from fastapi.testclient import TestClient

from tests.factories import World, events_named, make_agenda_item, make_item

pytestmark = pytest.mark.fr("FR-27")


@pytest.mark.parametrize("status", ["covered", "moved"])
def test_resolution_sets_status_and_writes_event(
    authed: TestClient, world: World, status: str
) -> None:
    make_agenda_item(world.db, world.meeting)
    agenda = make_agenda_item(world.db, world.meeting)
    resp = authed.patch(f"/api/v1/agenda/{agenda.id}", json={"status": status})
    assert resp.status_code == 200
    assert resp.json()["status"] == status
    events = events_named(world.db, "agenda_item_uncovered", meeting_id=world.meeting.id)
    assert len(events) == 1
    assert events[0].props["position"] == 2
    assert events[0].props["resolution"] == status


def uncovered_events(world: World):
    return events_named(world.db, "agenda_item_uncovered", meeting_id=world.meeting.id)


@pytest.mark.parametrize("status", ["covered", "moved"])
def test_no_event_when_agenda_item_has_linked_decision(
    authed: TestClient, world: World, status: str
) -> None:
    agenda = make_agenda_item(world.db, world.meeting)
    make_item(world.db, world.meeting, agenda_item_id=agenda.id)
    resp = authed.patch(f"/api/v1/agenda/{agenda.id}", json={"status": status})
    assert resp.status_code == 200
    assert resp.json()["status"] == status
    assert uncovered_events(world) == []


def test_second_transition_writes_no_second_event(authed: TestClient, world: World) -> None:
    agenda = make_agenda_item(world.db, world.meeting)
    assert authed.patch(f"/api/v1/agenda/{agenda.id}", json={"status": "moved"}).status_code == 200
    assert (
        authed.patch(f"/api/v1/agenda/{agenda.id}", json={"status": "covered"}).status_code == 200
    )
    assert len(uncovered_events(world)) == 1


def test_same_status_again_writes_no_second_event(authed: TestClient, world: World) -> None:
    agenda = make_agenda_item(world.db, world.meeting)
    for _ in range(2):
        assert (
            authed.patch(f"/api/v1/agenda/{agenda.id}", json={"status": "moved"}).status_code == 200
        )
    assert len(uncovered_events(world)) == 1
