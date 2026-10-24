import logging
import uuid

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from app.core.errors import Unauthorized
from app.core.security import decode_access_token, token_expiry
from app.modules.auth.models import User
from app.modules.live import bus
from app.modules.series import service as series_service

logger = logging.getLogger(__name__)
OUTBOX = "live_outbox"


def authorize(db: Session, token: str | None, meeting_id: uuid.UUID) -> int:
    user_id = decode_access_token(token) if token else None
    user = db.get(User, user_id) if user_id else None
    exp = token_expiry(token) if token else None
    if user is None or exp is None:
        raise Unauthorized("Нужно войти")
    series_service.get_owned_meeting(db, user, meeting_id)
    return exp


def publish(meeting_id: uuid.UUID, event: dict) -> None:
    """Синхронная, зовётся и из запроса, и из задачи Celery."""
    bus.publish(meeting_id, {**event, "event": event["type"]})


def publish_after_commit(db: Session, meeting_id: uuid.UUID, event_: dict) -> None:
    """При откате кадр не уйдёт."""
    db.info.setdefault(OUTBOX, []).append((meeting_id, event_))


@event.listens_for(Session, "after_commit")
def _flush_outbox(session: Session) -> None:
    for meeting_id, frame in session.info.pop(OUTBOX, []):
        try:
            publish(meeting_id, frame)
        except Exception:
            logger.warning("live publish failed", exc_info=True)


@event.listens_for(Session, "after_soft_rollback")
def _drop_outbox(session: Session, previous_transaction: SessionTransaction) -> None:
    if not previous_transaction.nested:
        session.info.pop(OUTBOX, None)
