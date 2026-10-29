"""FR-14 + BR-20: чистка журнала событий панели (meeting_events).

Журнал нужен только для догрузки после обрыва. Ожидаем от `cleanup_retention`:
- журнал встречи, закончившейся (не live и не closing) больше 30 дней назад, удаляется —
  с отправленным итогом и без него (FR-14 и BR-20 срок для журнала панели не называют);
- журнал идущей встречи (live, closing) не трогается;
- батч удаляет у встречи самые старые номера: остаток журнала идёт подряд до последнего,
  иначе догрузка между батчами отдала бы кадры с дырой.
"""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.live.models import MeetingEvent
from app.modules.series.models import Meeting, MeetingStatus
from tests.factories import World, make_meeting, make_series

pytestmark = pytest.mark.fr("FR-14")

NOW = datetime(2030, 6, 1, 3, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def shared_task_session(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.analytics import tasks

    @contextmanager
    def shared(**_: object) -> Iterator[Session]:
        yield db

    monkeypatch.setattr(tasks, "SessionLocal", shared)


def run_cleanup() -> dict[str, int]:
    return importlib.import_module("app.modules.analytics.tasks").cleanup_retention()


def meeting_with_journal(
    w: World, status: MeetingStatus, ended_days_ago: int, versions: list[int] | None = None
) -> Meeting:
    ended = NOW - timedelta(days=ended_days_ago)
    meeting = make_meeting(
        w.db,
        make_series(w.db, w.user),
        planned_start=ended - timedelta(minutes=30),
        status=status,
        started_at=None if status == MeetingStatus.PLANNED else ended - timedelta(minutes=30),
        summary_sent_at=ended if status == MeetingStatus.SENT else None,
    )
    numbers = versions if versions is not None else [1, 2, 3]
    meeting.events_version = max(numbers)
    for version in numbers:
        w.db.add(
            MeetingEvent(
                meeting_id=meeting.id,
                version=version,
                event="gaps.updated",
                data={},
                created_at=ended - timedelta(minutes=10) + timedelta(seconds=version),
            )
        )
        w.db.flush()
    return meeting


def journal(w: World, meeting: Meeting) -> list[int]:
    w.db.expire_all()
    return list(
        w.db.scalars(
            select(MeetingEvent.version)
            .where(MeetingEvent.meeting_id == meeting.id)
            .order_by(MeetingEvent.version)
        )
    )


def test_journal_of_meeting_sent_over_30_days_ago_is_purged(world: World) -> None:
    with freeze_time(NOW):
        meeting = meeting_with_journal(world, MeetingStatus.SENT, 31)
        run_cleanup()
        assert journal(world, meeting) == []


def test_journal_of_meeting_sent_less_than_30_days_ago_is_kept(world: World) -> None:
    with freeze_time(NOW):
        meeting = meeting_with_journal(world, MeetingStatus.SENT, 29)
        run_cleanup()
        assert journal(world, meeting) == [1, 2, 3]


@pytest.mark.parametrize("status", [MeetingStatus.LIVE, MeetingStatus.CLOSING])
def test_journal_of_running_meeting_is_kept_however_old(
    world: World, status: MeetingStatus
) -> None:
    with freeze_time(NOW):
        meeting = meeting_with_journal(world, status, 40)
        run_cleanup()
        assert journal(world, meeting) == [1, 2, 3]


def test_journal_of_meeting_ended_without_summary_over_30_days_ago_is_purged(
    world: World,
) -> None:
    with freeze_time(NOW):
        meeting = meeting_with_journal(world, MeetingStatus.PLANNED, 40)
        run_cleanup()
        assert journal(world, meeting) == []


def test_journal_of_meeting_ended_without_summary_recently_is_kept(world: World) -> None:
    with freeze_time(NOW):
        meeting = meeting_with_journal(world, MeetingStatus.PLANNED, 20)
        run_cleanup()
        assert journal(world, meeting) == [1, 2, 3]


def test_purge_batch_removes_oldest_numbers_first(world: World) -> None:
    from app.modules.live import service as live_service

    with freeze_time(NOW):
        meeting = meeting_with_journal(world, MeetingStatus.SENT, 31, versions=[6, 5, 4, 3, 2, 1])
        live_service.purge_old_events(world.db, NOW - timedelta(days=30), 2)
        assert journal(world, meeting) == [3, 4, 5, 6]
