import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from app.core.errors import Unauthorized
from app.core.security import decode_access_token, token_expiry
from app.modules.auth.models import User
from app.modules.live import bus, repository
from app.modules.live.models import MeetingEvent
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
    """Пронумеровать событие в транзакции изменения; кадр уходит после commit."""
    item_version = event_.get("version")
    data = event_.get("data")
    if item_version is not None and isinstance(data, dict):
        data = {**data, "version": item_version}
    version = repository.next_version(db, meeting_id)
    if version is None:
        db.info.setdefault(OUTBOX, []).append((meeting_id, event_))
        return
    frame = json.loads(
        json.dumps(
            {**event_, "version": version, "data": data if data is not None else {}}, default=str
        )
    )
    repository.add_event(
        db,
        MeetingEvent(
            meeting_id=meeting_id,
            version=version,
            event=frame["type"],
            item_id=uuid.UUID(frame["item_id"]) if frame.get("item_id") else None,
            data=frame["data"],
        ),
    )
    db.info.setdefault(OUTBOX, []).append((meeting_id, frame))


def _frame(meeting_id: uuid.UUID, row: MeetingEvent) -> dict[str, Any]:
    return {
        "type": row.event,
        "event": row.event,
        "meeting_id": str(meeting_id),
        "item_id": str(row.item_id) if row.item_id else None,
        "version": row.version,
        "data": row.data,
    }


def resync_frames(db: Session, meeting_id: uuid.UUID, since: object) -> tuple[list[dict], int]:
    """Кадры после `since` по порядку и номер, до которого клиент догнан.

    Нецелый, отрицательный, больший текущего или уже вычищенный `since` — один `resync.reset`.
    """
    current = repository.current_version(db, meeting_id)
    first = repository.first_version(db, meeting_id)
    oldest = (first if first is not None else current + 1) - 1
    if isinstance(since, bool) or not isinstance(since, int) or not oldest <= since <= current:
        reset = {
            "type": "resync.reset",
            "event": "resync.reset",
            "meeting_id": str(meeting_id),
            "item_id": None,
            "version": current,
            "data": {},
        }
        return [reset], current
    frames = [_frame(meeting_id, row) for row in repository.events_after(db, meeting_id, since)]
    return frames, frames[-1]["version"] if frames else since


def purge_old_events(db: Session, cutoff: datetime, limit: int) -> int:
    return repository.delete_events_of_ended_before(db, cutoff, limit)


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


# Поля кадров со свободным текстом: только в них имя ищется по словам (FR-52).
_FREE_TEXT_KEYS = frozenset(
    {"raw_text", "what", "due_text", "done_criteria", "question", "comment", "text", "reason"}
)


def anonymize_person_events(
    db: Session,
    owner_user_id: uuid.UUID,
    person_id: uuid.UUID,
    full_name: str,
    email: str | None,
    alias: str,
) -> int:
    """Убирает имя и email человека из data кадров журнала, подставляя `alias` (FR-52).

    Структурные поля (id, version, status, source, даты) не меняются. Объект `{id, full_name}`
    с id человека получает `alias` независимо от текста. В свободном тексте заменяются целым
    словом без учёта регистра полное имя, его части от 3 символов и email целиком.
    Строки журнала остаются на месте (номера не меняются). Возвращает число изменённых кадров.
    """
    name = " ".join(full_name.split())
    parts = [name, *name.split(), *([email.strip()] if email else [])]
    needles = sorted({n for n in parts if len(n) >= 3}, key=len, reverse=True)
    pid = str(person_id)
    pattern = (
        re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(n) for n in needles) + r")(?!\w)",
            re.IGNORECASE | re.UNICODE,
        )
        if needles
        else None
    )

    def scrub(value: Any, free: bool = False) -> Any:
        if isinstance(value, str):
            return pattern.sub(alias, value) if free and pattern else value
        if isinstance(value, list):
            return [scrub(v, free) for v in value]
        if isinstance(value, dict):
            mine = value.get("id") == pid and "full_name" in value
            out: dict[str, Any] = {}
            for k, v in value.items():
                if mine and k == "full_name":
                    out[k] = alias
                elif mine and k == "email":
                    out[k] = None
                else:
                    out[k] = scrub(v, k in _FREE_TEXT_KEYS)
            return out
        return value

    changed = 0
    for row in repository.events_mentioning(db, owner_user_id, [pid, *needles]):
        new = scrub(row.data)
        if new != row.data:
            row.data = new
            changed += 1
    db.flush()
    return changed
