"""BR-20: ежедневная чистка по срокам хранения: ссылки, payload писем, события аналитики."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from app.models import AccessLink, AnalyticsEvent, Item, ItemEvent, Notification, Person
from app.modules.llm.models import LlmCall, LlmOutcome, LlmTask
from app.modules.notifications.models import NotificationStatus, NotificationType
from app.modules.series.models import SeriesStatus
from tests.factories import (
    World,
    add_member,
    make_access_link,
    make_event,
    make_item,
    make_meeting,
    make_person,
    make_series,
)

pytestmark = pytest.mark.fr("FR-52")

NOW = datetime(2030, 6, 1, 3, 0, tzinfo=UTC)
TASK = "app.modules.analytics.tasks.cleanup_retention"


@pytest.fixture(autouse=True)
def shared_task_session(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Задача открывает свою сессию; в тесте она работает в транзакции теста."""
    from app.modules.analytics import tasks

    @contextmanager
    def shared(**_: object) -> Iterator[Session]:
        yield db

    monkeypatch.setattr(tasks, "SessionLocal", shared)


def run_cleanup() -> dict[str, int]:
    module = importlib.import_module("app.modules.analytics.tasks")
    return module.cleanup_retention()


def notification(w: World, key: str, created: datetime) -> Notification:
    row = Notification(
        type=NotificationType.SUMMARY,
        recipient_person_id=w.person.id,
        meeting_id=w.meeting.id,
        dedupe_key=key,
        scheduled_for=created,
        status=NotificationStatus.SENT,
        payload={"subject": "s"},
        created_at=created,
    )
    w.db.add(row)
    w.db.flush()
    return row


def test_task_is_registered_in_beat_daily() -> None:
    from app.worker import celery_app

    entries = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == TASK]
    assert len(entries) == 1
    schedule = entries[0]["schedule"]
    if isinstance(schedule, (int, float)):
        assert schedule <= 86400
    else:  # crontab: раз в сутки = один час, одна минута, каждый день
        assert len(schedule.hour) == 1 and len(schedule.minute) == 1
        assert len(schedule.day_of_week) == 7 and len(schedule.day_of_month) == 31


def test_links_expired_over_30_days_ago_are_deleted(world: World) -> None:
    with freeze_time(NOW):
        old = make_access_link(
            world.db, world.person, world.meeting, expires_at=NOW - timedelta(days=31)
        )
        meeting2 = make_meeting(world.db, world.series)
        fresh = make_access_link(
            world.db, world.person, meeting2, expires_at=NOW - timedelta(days=29)
        )
        old_id, fresh_id = old.id, fresh.id
        run_cleanup()
        world.db.expire_all()
        left = set(world.db.scalars(select(AccessLink.id)))
    assert old_id not in left
    assert fresh_id in left


def test_payload_older_than_90_days_is_cleared_row_stays(world: World) -> None:
    with freeze_time(NOW):
        old = notification(world, "old", NOW - timedelta(days=91))
        fresh = notification(world, "fresh", NOW - timedelta(days=89))
        run_cleanup()
        world.db.refresh(old)
        world.db.refresh(fresh)
    assert old.payload is None
    assert fresh.payload == {"subject": "s"}


def test_analytics_older_than_two_years_are_deleted(world: World) -> None:
    with freeze_time(NOW):
        world.db.add(AnalyticsEvent(name="old_evt", created_at=NOW - timedelta(days=731)))
        world.db.add(AnalyticsEvent(name="new_evt", created_at=NOW - timedelta(days=729)))
        world.db.flush()
        run_cleanup()
        names = set(world.db.scalars(select(AnalyticsEvent.name)))
    assert "old_evt" not in names
    assert "new_evt" in names


def test_cleanup_is_idempotent(world: World) -> None:
    with freeze_time(NOW):
        world.db.add(AnalyticsEvent(name="old_evt", created_at=NOW - timedelta(days=731)))
        world.db.add(AnalyticsEvent(name="new_evt", created_at=NOW - timedelta(days=729)))
        world.db.flush()
        first = run_cleanup()
        second = run_cleanup()
        names = set(world.db.scalars(select(AnalyticsEvent.name)))
    assert first["events"] == 1
    assert second["events"] == 0
    assert "old_evt" not in names
    assert "new_evt" in names


def payload_is_sql_null(w: World, note: Notification) -> bool:
    return w.db.scalar(
        text("SELECT payload IS NULL FROM notifications WHERE id = :id"), {"id": note.id}
    )


def test_cleared_payload_is_sql_null_not_json_null(world: World) -> None:
    with freeze_time(NOW):
        old = notification(world, "old", NOW - timedelta(days=91))
        run_cleanup()
        assert payload_is_sql_null(world, old) is True


def test_payload_cleanup_second_run_touches_nothing(world: World) -> None:
    with freeze_time(NOW):
        notification(world, "old", NOW - timedelta(days=91))
        first = run_cleanup()
        second = run_cleanup()
    assert first["payloads"] == 1
    assert second["payloads"] == 0


def test_queued_letters_keep_payload_even_when_old(world: World) -> None:
    with freeze_time(NOW):
        queued = notification(world, "queued", NOW - timedelta(days=200))
        queued.status = NotificationStatus.QUEUED
        world.db.flush()
        run_cleanup()
        world.db.refresh(queued)
    assert queued.payload == {"subject": "s"}


def add_llm_call(w: World, created: datetime) -> LlmCall:
    row = LlmCall(
        task=LlmTask.PARSE,
        provider="stub",
        model="stub",
        outcome=LlmOutcome.OK,
        created_at=created,
    )
    w.db.add(row)
    w.db.flush()
    return row


def test_llm_calls_older_than_a_year_deleted_younger_kept(world: World) -> None:
    with freeze_time(NOW):
        old = add_llm_call(world, NOW - timedelta(days=366))
        young = add_llm_call(world, NOW - timedelta(days=364))
        old_id, young_id = old.id, young.id
        run_cleanup()
        world.db.expire_all()
        left = set(world.db.scalars(select(LlmCall.id)))
    assert old_id not in left
    assert young_id in left


def test_item_events_older_than_a_year_deleted_younger_kept(world: World) -> None:
    with freeze_time(NOW):
        item = world.item()
        old = make_event(world.db, item, created_at=NOW - timedelta(days=366))
        young = make_event(world.db, item, created_at=NOW - timedelta(days=364))
        old_id, young_id = old.id, young.id
        run_cleanup()
        world.db.expire_all()
        left = set(world.db.scalars(select(ItemEvent.id)))
        assert world.db.get(Item, item.id) is not None
    assert old_id not in left
    assert young_id in left


def archived_series(w: World, days_ago: int, **kw):
    return make_series(
        w.db,
        w.user,
        status=SeriesStatus.ARCHIVED,
        archived_at=NOW - timedelta(days=days_ago),
        **kw,
    )


def test_items_of_series_archived_over_a_year_ago_cascade_deleted(world: World) -> None:
    with freeze_time(NOW):
        old_series = archived_series(world, 366)
        young_series = archived_series(world, 364)
        old_item = make_item(world.db, make_meeting(world.db, old_series))
        young_item = make_item(world.db, make_meeting(world.db, young_series))
        active_item = world.item()
        make_event(world.db, old_item)
        ids = (old_item.id, young_item.id, active_item.id)
        run_cleanup()
        world.db.expire_all()
        left = set(world.db.scalars(select(Item.id)))
        events_left = world.db.scalar(
            text("SELECT count(*) FROM item_events WHERE item_id = :id"), {"id": ids[0]}
        )
    assert ids[0] not in left
    assert ids[1] in left and ids[2] in left
    assert events_left == 0


def test_members_of_old_archived_series_without_other_links_are_anonymized(
    world: World,
) -> None:
    with freeze_time(NOW):
        old_series = archived_series(world, 400)
        young_series = archived_series(world, 100)
        lonely = make_person(world.db, world.user, world.org, full_name="Lonely")
        also_active = make_person(world.db, world.user, world.org, full_name="Active")
        with_item = make_person(world.db, world.user, world.org, full_name="Assigned")
        only_young = make_person(world.db, world.user, world.org, full_name="Young")
        for person in (lonely, also_active, with_item):
            add_member(world.db, old_series, person)
        add_member(world.db, world.series, also_active)
        add_member(world.db, young_series, only_young)
        world.item(assignee_person_id=with_item.id)
        result = run_cleanup()
        world.db.expire_all()
        got = {p.full_name: p for p in world.db.scalars(select(Person))}
    assert result["persons"] == 1
    assert "Lonely" not in got
    anon = next(p for p in got.values() if p.anonymized_at is not None)
    assert anon.id == lonely.id
    assert anon.email is None and anon.full_name.startswith("Участник ")
    for name in ("Active", "Assigned", "Young"):
        assert got[name].anonymized_at is None
        assert got[name].email is not None


def test_retention_runs_in_batches_over_5000_rows(world: World) -> None:
    old = NOW - timedelta(days=800)
    with freeze_time(NOW):
        world.db.execute(
            insert(AnalyticsEvent),
            [{"name": "bulk", "props": {}, "created_at": old} for _ in range(5001)],
        )
        world.db.add(AnalyticsEvent(name="keep", created_at=NOW))
        world.db.flush()
        result = run_cleanup()
        names = set(world.db.scalars(select(AnalyticsEvent.name)))
    assert result["events"] == 5001
    assert "bulk" not in names and "keep" in names
