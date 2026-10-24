import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.gaps.models import Gap, GapSource, GapType


def item_gaps(db: Session, item_id: uuid.UUID) -> list[Gap]:
    stmt = select(Gap).where(Gap.item_id == item_id).order_by(Gap.detected_at, Gap.id)
    return list(db.scalars(stmt))


def get_for_item(db: Session, item_id: uuid.UUID, gap_id: uuid.UUID) -> Gap | None:
    return db.scalar(select(Gap).where(Gap.id == gap_id, Gap.item_id == item_id))


def dismiss(db: Session, gap: Gap, now: datetime) -> bool:
    """Атомарно: из параллельных запросов победит один, остальные получат False."""
    stmt = (
        update(Gap)
        .where(Gap.id == gap.id, Gap.dismissed_at.is_(None), Gap.resolved_at.is_(None))
        .values(dismissed_at=now)
        .returning(Gap.id)
    )
    won = db.scalar(stmt) is not None
    if won:
        db.refresh(gap)
    return won


def add(
    db: Session, item_id: uuid.UUID, gap_type: GapType, source: GapSource, question: str
) -> Gap:
    # clock_timestamp, а не now(): в одной транзакции вновь открытый пробел идёт после старого
    gap = Gap(
        item_id=item_id,
        type=gap_type,
        source=source,
        question=question,
        detected_at=func.clock_timestamp(),
    )
    db.add(gap)
    db.flush()
    db.refresh(gap)
    return gap
