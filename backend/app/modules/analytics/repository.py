import uuid
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.modules.analytics.models import AnalyticsEvent


def add(db: Session, event: AnalyticsEvent) -> AnalyticsEvent:
    db.add(event)
    db.flush()
    return event


def exists_since(db: Session, name: str, item_id: uuid.UUID, since: datetime) -> bool:
    stmt = select(AnalyticsEvent.id).where(
        AnalyticsEvent.name == name,
        AnalyticsEvent.item_id == item_id,
        AnalyticsEvent.created_at >= since,
    )
    return db.scalar(stmt.limit(1)) is not None


def stream_between(
    db: Session, start: datetime | None, end: datetime | None
) -> Iterator[AnalyticsEvent]:
    stmt = select(AnalyticsEvent).order_by(AnalyticsEvent.created_at, AnalyticsEvent.id)
    if start is not None:
        stmt = stmt.where(AnalyticsEvent.created_at >= start)
    if end is not None:
        stmt = stmt.where(AnalyticsEvent.created_at < end)
    yield from db.scalars(stmt.execution_options(yield_per=1000))


def delete_before(db: Session, cutoff: datetime, limit: int) -> int:
    result = db.execute(
        delete(AnalyticsEvent).where(
            AnalyticsEvent.id.in_(
                select(AnalyticsEvent.id).where(AnalyticsEvent.created_at < cutoff).limit(limit)
            )
        )
    )
    db.flush()
    return result.rowcount
