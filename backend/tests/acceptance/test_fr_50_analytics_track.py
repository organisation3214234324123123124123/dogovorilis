import logging

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnalyticsEvent
from tests.factories import World

pytestmark = pytest.mark.fr("FR-50")

EVENTS = [
    ("series_created", {"members_count": 3, "has_external": True}),
    ("meeting_started", {"capture_mode": "manual", "review_items_count": 2}),
    ("review_action", {"action": "reschedule"}),
    ("review_skipped", {}),
    ("decision_captured", {"source": "manual", "fields_filled": 3}),
    ("closure_opened", {"trigger": "button", "minutes_to_planned_end": 4}),
    ("gap_detected", {"type": "no_due", "source": "rule"}),
    ("gap_resolved", {"type": "no_due", "seconds_since_detected": 42}),
    ("gap_dismissed", {"type": "no_due", "source": "llm"}),
    ("overload_warning_shown", {"open_items": 6}),
    ("agenda_item_uncovered", {"position": 1, "resolution": "covered"}),
    ("accepted_in_meeting", {"due_changed": False}),
    (
        "summary_sent",
        {"items_total": 5, "items_with_open_gaps": 1, "recipients": 3},
    ),
    ("summary_copied", {"items_total": 5, "summary_sent_before": True}),
    ("link_opened", {"notification_type": "SUMMARY"}),
    ("link_answered", {"answer": "accept", "hours_since_sent": 3}),
    ("reminder_sent", {}),
    ("item_done", {"on_time": True, "by": "assignee"}),
    ("item_overdue", {"days_overdue": 2}),
]


def track(db: Session, name: str, **props):
    from app.modules.analytics.service import track as fn

    return fn(db, name, **props)


def events(db: Session, name: str | None = None) -> list[AnalyticsEvent]:
    stmt = select(AnalyticsEvent).order_by(AnalyticsEvent.id)
    if name:
        stmt = stmt.where(AnalyticsEvent.name == name)
    return list(db.scalars(stmt))


@pytest.mark.parametrize(("name", "props"), EVENTS, ids=[e[0] for e in EVENTS])
def test_track_writes_exactly_one_row_per_listed_event(
    world: World, name: str, props: dict
) -> None:
    track(world.db, name, **props)
    rows = events(world.db, name)
    assert len(rows) == 1
    assert rows[0].props == props
    assert rows[0].created_at is not None


def test_track_puts_context_ids_in_columns_not_props(world: World) -> None:
    item = world.item()
    track(
        world.db,
        "item_done",
        user_id=world.user.id,
        series_id=world.series.id,
        meeting_id=world.meeting.id,
        item_id=item.id,
        on_time=True,
        by="host",
    )
    row = events(world.db, "item_done")[0]
    assert (row.user_id, row.series_id, row.meeting_id, row.item_id) == (
        world.user.id,
        world.series.id,
        world.meeting.id,
        item.id,
    )
    assert row.props == {"on_time": True, "by": "host"}


def test_track_without_context_leaves_ids_null(world: World) -> None:
    track(world.db, "reminder_sent")
    row = events(world.db, "reminder_sent")[0]
    assert (row.user_id, row.series_id, row.meeting_id, row.item_id) == (None,) * 4


def test_event_is_rolled_back_with_the_action_transaction(world: World) -> None:
    savepoint = world.db.begin_nested()
    track(world.db, "item_done", on_time=True, by="host")
    assert len(events(world.db, "item_done")) == 1
    savepoint.rollback()
    assert events(world.db, "item_done") == []


def test_track_does_not_commit(world: World) -> None:
    track(world.db, "reminder_sent")
    world.db.rollback()
    assert events(world.db, "reminder_sent") == []


@pytest.fixture
def warnings_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.WARNING, logger="app.modules.analytics.service")
    return caplog


@pytest.mark.parametrize(
    "value",
    ["ivan@example.com", "Иван разберётся с выгрузкой к пятнице", "x" * 200],
    ids=["email", "sentence", "long"],
)
def test_track_drops_free_text_prop_keeps_event(
    world: World, warnings_log: pytest.LogCaptureFixture, value: str
) -> None:
    row = track(world.db, "gap_detected", type=value, source="rule")
    rows = events(world.db, "gap_detected")
    assert len(rows) == 1
    assert row is rows[0]
    assert "type" not in rows[0].props
    assert rows[0].props == {"source": "rule"}
    assert any(r.levelno == logging.WARNING for r in warnings_log.records)


def test_track_drops_nested_prop_keeps_others(
    world: World, warnings_log: pytest.LogCaptureFixture
) -> None:
    track(world.db, "item_done", on_time={"a": "b"}, by="host")
    rows = events(world.db, "item_done")
    assert len(rows) == 1
    assert rows[0].props == {"by": "host"}
    assert any(r.levelno == logging.WARNING for r in warnings_log.records)


def test_track_valid_props_give_no_warning(
    world: World, warnings_log: pytest.LogCaptureFixture
) -> None:
    track(world.db, "item_done", on_time=True, by="host")
    assert not [r for r in warnings_log.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("name", ["llm_call", "suggestion_added", "suggestion_rejected", "nope"])
def test_track_unknown_name_writes_nothing_and_warns(
    world: World, warnings_log: pytest.LogCaptureFixture, name: str
) -> None:
    assert track(world.db, name) is None
    assert events(world.db) == []
    assert any(r.levelno == logging.WARNING for r in warnings_log.records)


def test_track_never_raises_on_bad_input(world: World) -> None:
    track(world.db, "nope", x=object())
    track(world.db, "item_done", on_time=object(), by=["a b"])
    assert len(events(world.db, "item_done")) == 1
