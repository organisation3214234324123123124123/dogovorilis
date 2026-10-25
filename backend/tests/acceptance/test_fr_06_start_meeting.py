from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import MeetingParticipant
from app.modules.series.models import MeetingStatus, SeriesStatus
from tests.acceptance.helpers import frozen
from tests.factories import (
    World,
    add_member,
    events_named,
    make_meeting,
    make_person,
    make_series,
    make_user,
)

pytestmark = pytest.mark.fr("FR-06")

NOON = "2026-10-16T10:00:00+00:00"


def start(client: TestClient, series, body: dict | None = None):
    return client.post(f"/api/v1/series/{series.id}/meetings", json=body or {})


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def test_start_sets_planned_window_from_series_duration(authed: TestClient, world: World) -> None:
    world.series.planned_duration_min = 60
    world.db.flush()
    with frozen(authed, world.user, NOON):
        resp = start(authed, world.series)
    assert resp.status_code == 201
    meeting = resp.json()["meeting"]
    assert parse(meeting["planned_start"]) == datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
    assert parse(meeting["planned_end"]) == datetime(2026, 10, 16, 11, 0, tzinfo=UTC)
    assert parse(meeting["started_at"]) == datetime(2026, 10, 16, 10, 0, tzinfo=UTC)


def test_start_response_has_empty_review_without_open_items(
    authed: TestClient, world: World
) -> None:
    resp = start(authed, world.series)
    assert resp.status_code == 201
    review = resp.json()["review"]
    assert all(review[key] == [] for key in review)


def test_start_creates_present_participants_for_all_members(
    authed: TestClient, world: World
) -> None:
    second = make_person(world.db, world.user, world.org)
    add_member(world.db, world.series, world.person)
    add_member(world.db, world.series, second)
    resp = start(authed, world.series)
    assert resp.status_code == 201
    rows = world.db.scalars(
        select(MeetingParticipant).where(
            MeetingParticipant.meeting_id == resp.json()["meeting"]["id"]
        )
    ).all()
    assert {r.person_id for r in rows} == {world.person.id, second.id}
    assert all(r.present for r in rows)


def test_patch_changes_planned_end(authed: TestClient, world: World) -> None:
    new_end = datetime.now(UTC) + timedelta(hours=3)
    resp = authed.patch(
        f"/api/v1/meetings/{world.meeting.id}", json={"planned_end": new_end.isoformat()}
    )
    assert resp.status_code == 200
    assert parse(resp.json()["planned_end"]) == new_end


@pytest.mark.parametrize("status", [MeetingStatus.LIVE, MeetingStatus.CLOSING])
def test_second_active_meeting_is_conflict(
    authed: TestClient, world: World, status: MeetingStatus
) -> None:
    make_meeting(world.db, world.series, status=status)
    resp = start(authed, world.series)
    assert resp.status_code == 409
    assert resp.json()["code"] == "MEETING_ALREADY_LIVE"


def test_archived_series_cannot_start_meeting(authed: TestClient, world: World) -> None:
    world.series.status = SeriesStatus.ARCHIVED
    world.db.flush()
    resp = start(authed, world.series)
    assert resp.status_code == 409
    assert resp.json()["code"] == "SERIES_ARCHIVED"


def test_foreign_series_is_404(authed: TestClient, world: World) -> None:
    foreign = make_series(world.db, make_user(world.db))
    resp = start(authed, foreign)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_meeting_started_event(authed: TestClient, world: World) -> None:
    resp = start(authed, world.series)
    assert resp.status_code == 201
    events = events_named(world.db, "meeting_started", meeting_id=resp.json()["meeting"]["id"])
    assert len(events) == 1
    assert events[0].props["capture_mode"] == "manual"
    assert events[0].props["review_items_count"] == 0


def test_start_race_on_unique_index_is_conflict(
    authed: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка не видит живой встречи (параллельный старт ещё не закоммичен)."""
    from app.modules.series import repository

    make_meeting(world.db, world.series, status=MeetingStatus.LIVE)
    monkeypatch.setattr(repository, "active_meetings", lambda *a, **k: [])
    resp = start(authed, world.series)
    assert resp.status_code == 409
    assert resp.json()["code"] == "MEETING_ALREADY_LIVE"
