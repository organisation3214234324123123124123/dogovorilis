import uuid
from datetime import datetime

from sqlalchemy import Text, cast, delete, func, or_, select, tuple_, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.modules.live.models import MeetingEvent
from app.modules.series.models import Meeting, MeetingStatus, Series


def next_version(db: Session, meeting_id: uuid.UUID) -> int | None:
    """Блокирует строку встречи до commit: номера идут подряд в порядке commit."""
    meeting = db.get(Meeting, meeting_id)
    version = db.execute(
        update(Meeting)
        .where(Meeting.id == meeting_id)
        .values(events_version=Meeting.events_version + 1)
        .returning(Meeting.events_version),
        execution_options={"synchronize_session": False},
    ).scalar_one_or_none()
    if meeting is not None and version is not None:
        set_committed_value(meeting, "events_version", version)
    return version


def add_event(db: Session, event: MeetingEvent) -> None:
    db.add(event)


def current_version(db: Session, meeting_id: uuid.UUID) -> int:
    return db.execute(select(Meeting.events_version).where(Meeting.id == meeting_id)).scalar_one()


def first_version(db: Session, meeting_id: uuid.UUID) -> int | None:
    return db.execute(
        select(func.min(MeetingEvent.version)).where(MeetingEvent.meeting_id == meeting_id)
    ).scalar_one()


def events_after(db: Session, meeting_id: uuid.UUID, since: int) -> list[MeetingEvent]:
    return list(
        db.execute(
            select(MeetingEvent)
            .where(MeetingEvent.meeting_id == meeting_id, MeetingEvent.version > since)
            .order_by(MeetingEvent.version)
        ).scalars()
    )


def delete_events_of_ended_before(db: Session, cutoff: datetime, limit: int) -> int:
    """Журнал встреч, не идущих и закончившихся раньше cutoff; у встречи — старые номера первыми."""
    ended = func.coalesce(Meeting.summary_sent_at, Meeting.planned_end)
    stale = (
        select(MeetingEvent.meeting_id, MeetingEvent.version)
        .join(Meeting, Meeting.id == MeetingEvent.meeting_id)
        .where(
            Meeting.status.not_in((MeetingStatus.LIVE, MeetingStatus.CLOSING)),
            ended < cutoff,
        )
        .order_by(MeetingEvent.meeting_id, MeetingEvent.version)
        .limit(limit)
    )
    result = db.execute(
        delete(MeetingEvent).where(tuple_(MeetingEvent.meeting_id, MeetingEvent.version).in_(stale))
    )
    return result.rowcount


def events_mentioning(
    db: Session, owner_user_id: uuid.UUID, needles: list[str]
) -> list[MeetingEvent]:
    """Предотбор: кадры журнала встреч ведущего с подстрокой в data (без учёта регистра)."""
    escaped = [n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for n in needles]
    return list(
        db.execute(
            select(MeetingEvent)
            .join(Meeting, Meeting.id == MeetingEvent.meeting_id)
            .join(Series, Series.id == Meeting.series_id)
            .where(
                Series.owner_user_id == owner_user_id,
                or_(*(cast(MeetingEvent.data, Text).ilike(f"%{n}%", escape="\\") for n in escaped)),
            )
            .with_for_update(of=MeetingEvent)
        ).scalars()
    )
