from datetime import datetime

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from app.modules.live.models import MeetingEvent
from app.modules.series.models import Meeting, MeetingStatus


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
