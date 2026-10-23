import uuid
from datetime import datetime

from sqlalchemy import select
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
