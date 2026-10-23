from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.modules.gaps.models import GapType
from app.modules.items.models import ItemStatus
from tests.factories import (
    World,
    make_access_link,
    make_event,
    make_gap,
    make_item,
    make_meeting,
    make_series,
)

pytestmark = pytest.mark.fr("data-model")

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def w(db: Session) -> World:
    return World(db)


def test_item_defaults(w: World) -> None:
    item = w.item()
    assert item.status == ItemStatus.DRAFT
    assert item.version == 1
    assert item.reschedule_count == 0


def test_done_requires_done_at(w: World) -> None:
    with pytest.raises(IntegrityError, match="done_at"):
        w.item(status=ItemStatus.DONE)


def test_done_at_requires_done_status(w: World) -> None:
    with pytest.raises(IntegrityError, match="done_at"):
        w.item(status=ItemStatus.ACCEPTED, done_at=NOW)


def test_done_with_done_at_ok(w: World) -> None:
    assert w.item(status=ItemStatus.DONE, done_at=NOW).id


def test_cancelled_requires_cancelled_at(w: World) -> None:
    with pytest.raises(IntegrityError, match="cancelled_at"):
        w.item(status=ItemStatus.CANCELLED)


def test_cancelled_with_cancelled_at_ok(w: World) -> None:
    assert w.item(status=ItemStatus.CANCELLED, cancelled_at=NOW).id


def test_negative_reschedule_count_rejected(w: World) -> None:
    with pytest.raises(IntegrityError, match="reschedule_count"):
        w.item(reschedule_count=-1)


@pytest.mark.parametrize("minutes", [14, 241, 0])
def test_series_duration_out_of_bounds(w: World, minutes: int) -> None:
    with pytest.raises(IntegrityError, match="planned_duration_min"):
        make_series(w.db, w.user, planned_duration_min=minutes)


@pytest.mark.parametrize("minutes", [15, 240])
def test_series_duration_bounds_inclusive(w: World, minutes: int) -> None:
    assert make_series(w.db, w.user, planned_duration_min=minutes).id


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(minutes=-5)])
def test_meeting_end_must_follow_start(w: World, delta: timedelta) -> None:
    with pytest.raises(IntegrityError, match="planned_period"):
        make_meeting(w.db, w.series, planned_start=NOW, planned_end=NOW + delta)


def test_one_open_gap_per_item_and_type(w: World) -> None:
    item = w.item()
    make_gap(w.db, item, type=GapType.NO_DUE)
    with pytest.raises(IntegrityError, match="uq_gaps_item_id_type_open"):
        make_gap(w.db, item, type=GapType.NO_DUE)


def test_other_gap_type_allowed(w: World) -> None:
    item = w.item()
    make_gap(w.db, item, type=GapType.NO_DUE)
    assert make_gap(w.db, item, type=GapType.NO_ASSIGNEE).id


def test_new_gap_allowed_after_resolve(w: World) -> None:
    item = w.item()
    make_gap(w.db, item, resolved_at=NOW)
    assert make_gap(w.db, item).id


def test_item_events_cannot_be_updated(w: World) -> None:
    event = make_event(w.db, w.item())
    with pytest.raises(DBAPIError, match="append-only"):
        w.db.execute(
            text("UPDATE item_events SET action = 'done' WHERE id = :id"), {"id": event.id}
        )


def test_item_events_direct_delete_is_refused(w: World) -> None:
    event = make_event(w.db, w.item())
    with pytest.raises(DBAPIError, match="append-only"):
        w.db.execute(text("DELETE FROM item_events WHERE id = :id"), {"id": event.id})


def test_deleting_item_cascades_its_events(w: World) -> None:
    item = w.item()
    make_event(w.db, item)
    w.db.execute(text("DELETE FROM items WHERE id = :id"), {"id": item.id})
    left = w.db.execute(
        text("SELECT count(*) FROM item_events WHERE item_id = :id"), {"id": item.id}
    ).scalar_one()
    assert left == 0


def test_one_active_access_link_per_person_and_meeting(w: World) -> None:
    make_access_link(w.db, w.person, w.meeting)
    with pytest.raises(IntegrityError, match="uq_access_links_person_id_meeting_id_active"):
        make_access_link(w.db, w.person, w.meeting)


def test_new_access_link_allowed_after_revoke(w: World) -> None:
    make_access_link(w.db, w.person, w.meeting, revoked_at=NOW)
    assert make_access_link(w.db, w.person, w.meeting).id


def test_token_hash_must_be_32_bytes(w: World) -> None:
    with pytest.raises(IntegrityError, match="token_hash_len"):
        make_access_link(w.db, w.person, w.meeting, token_hash=b"short")


def test_person_with_items_cannot_be_deleted(w: World) -> None:
    w.item(assignee_person_id=w.person.id)
    with pytest.raises(IntegrityError, match="assignee_person_id"):
        w.db.execute(text("DELETE FROM persons WHERE id = :id"), {"id": w.person.id})


def test_deleting_meeting_cascades_items(w: World) -> None:
    item = make_item(w.db, w.meeting)
    w.db.execute(text("DELETE FROM meetings WHERE id = :id"), {"id": w.meeting.id})
    assert w.db.scalar(text("SELECT count(*) FROM items WHERE id = :id"), {"id": item.id}) == 0
