import uuid

import pytest
from fastapi.testclient import TestClient

from tests.factories import (
    World,
    add_member,
    make_meeting,
    make_participant,
    make_person,
    make_series,
    make_user,
)

pytestmark = pytest.mark.fr("FR-07")


def set_presence(client: TestClient, meeting, person_id, present: bool):
    return client.patch(
        f"/api/v1/meetings/{meeting.id}",
        json={"participants": [{"person_id": str(person_id), "present": present}]},
    )


def presence(client: TestClient, meeting) -> dict[str, bool]:
    data = client.get(f"/api/v1/meetings/{meeting.id}").json()
    return {p["person_id"]: p["present"] for p in data["participants"]}


def test_presence_can_be_cleared_and_restored(authed: TestClient, world: World) -> None:
    add_member(world.db, world.series, world.person)
    make_participant(world.db, world.meeting, world.person)
    off = set_presence(authed, world.meeting, world.person.id, False)
    assert off.status_code == 200
    assert presence(authed, world.meeting)[str(world.person.id)] is False
    on = set_presence(authed, world.meeting, world.person.id, True)
    assert on.status_code == 200
    assert presence(authed, world.meeting)[str(world.person.id)] is True


def test_non_participant_is_rejected(authed: TestClient, world: World) -> None:
    outsider = make_person(world.db, world.user, world.org)
    resp = set_presence(authed, world.meeting, outsider.id, False)
    assert resp.status_code == 422
    assert resp.json()["code"] == "NOT_A_PARTICIPANT"


def test_unknown_person_is_rejected(authed: TestClient, world: World) -> None:
    resp = set_presence(authed, world.meeting, uuid.uuid4(), False)
    assert resp.status_code == 422
    assert resp.json()["code"] == "NOT_A_PARTICIPANT"


def test_absent_assignee_cannot_be_accepted_in_meeting(authed: TestClient, world: World) -> None:
    add_member(world.db, world.series, world.person)
    make_participant(world.db, world.meeting, world.person, present=False)
    item = world.item(assignee_person_id=world.person.id, due_date="2099-01-01")
    resp = authed.post(
        f"/api/v1/items/{item.id}/accept-in-meeting",
        json={"value": True},
        headers={"If-Match": str(item.version)},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "GAPS_OPEN"


def test_foreign_meeting_is_404(authed: TestClient, world: World) -> None:
    stranger = make_user(world.db)
    foreign = make_meeting(world.db, make_series(world.db, stranger))
    resp = set_presence(authed, foreign, uuid.uuid4(), False)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
