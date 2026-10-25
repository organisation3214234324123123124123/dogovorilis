import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.analytics import repository
from app.modules.analytics.models import AnalyticsEvent

EVENT_NAMES = frozenset(
    {
        "series_created",
        "meeting_started",
        "review_action",
        "review_skipped",
        "decision_captured",
        "closure_opened",
        "gap_detected",
        "gap_resolved",
        "gap_dismissed",
        "overload_warning_shown",
        "agenda_item_uncovered",
        "accepted_in_meeting",
        "summary_sent",
        "summary_copied",
        "link_opened",
        "link_answered",
        "reminder_sent",
        "item_done",
        "item_overdue",
        "item_edited",
    }
)
_TOKEN = re.compile(r"[A-Za-z0-9_.:+-]{1,40}")
logger = logging.getLogger(__name__)


def _valid_prop(value: Any) -> bool:
    if value is None or isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return _TOKEN.fullmatch(value) is not None
    if isinstance(value, list | tuple):
        return all(isinstance(v, str) and _TOKEN.fullmatch(v) for v in value)
    return False


def _plain(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def track(
    db: Session,
    name: str,
    *,
    user_id: uuid.UUID | None = None,
    series_id: uuid.UUID | None = None,
    meeting_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
    **props: Any,
) -> AnalyticsEvent | None:
    """Только id и счётчики, без персональных данных."""
    if name not in EVENT_NAMES:
        logger.warning("analytics event dropped: unknown name %r", name)
        return None
    clean = {}
    for key, value in _plain(props).items():
        if _valid_prop(value):
            clean[key] = value
        else:
            logger.warning("analytics prop dropped: %s.%s is not a number, flag or code", name, key)
    return repository.add(
        db,
        AnalyticsEvent(
            name=name,
            user_id=user_id,
            series_id=series_id,
            meeting_id=meeting_id,
            item_id=item_id,
            props=clean,
        ),
    )


def was_tracked_since(db: Session, name: str, item_id: uuid.UUID, since: datetime) -> bool:
    return repository.exists_since(db, name, item_id, since)


def purge_old_events(db: Session, cutoff: datetime, limit: int) -> int:
    return repository.delete_before(db, cutoff, limit)
