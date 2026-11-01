import uuid
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.modules.llm.models import LlmCall, LlmPrompt, LlmTask
from app.modules.series.models import Meeting, Series


def list_prompts(db: Session) -> list[LlmPrompt]:
    return list(db.scalars(select(LlmPrompt).order_by(LlmPrompt.task, LlmPrompt.version.desc())))


def get_active_prompt(db: Session, task: LlmTask) -> LlmPrompt | None:
    return db.scalar(select(LlmPrompt).where(LlmPrompt.task == task, LlmPrompt.is_active))


def lock_task(db: Session, task: LlmTask) -> None:
    """Замок до конца транзакции: две версии промпта одной задачи не создаются одновременно."""
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"llm_prompts:{task.value}"))))


def next_version(db: Session, task: LlmTask) -> int:
    last = db.scalar(select(func.max(LlmPrompt.version)).where(LlmPrompt.task == task))
    return (last or 0) + 1


def deactivate_prompts(db: Session, task: LlmTask) -> None:
    db.execute(update(LlmPrompt).where(LlmPrompt.task == task).values(is_active=False))


def add_prompt(db: Session, prompt: LlmPrompt) -> LlmPrompt:
    db.add(prompt)
    db.flush()
    return prompt


def add_call(db: Session, call: LlmCall) -> LlmCall:
    db.add(call)
    db.flush()
    return call


def delete_calls_before(db: Session, cutoff: datetime, limit: int) -> int:
    result = db.execute(
        delete(LlmCall).where(
            LlmCall.id.in_(select(LlmCall.id).where(LlmCall.created_at < cutoff).limit(limit))
        )
    )
    db.flush()
    return result.rowcount


CHECK_LOCK_KEY = 7_302_041_966_411


def try_check_lock(db: Session) -> bool:
    """Замок на одну проверку за раз; держится до конца транзакции."""
    return bool(db.scalar(select(func.pg_try_advisory_xact_lock(CHECK_LOCK_KEY))))


def recent_calls(db: Session, limit: int, owner_id: uuid.UUID) -> list[LlmCall]:
    """Вызовы по встречам ведущего и без встречи (в них нет текстов)."""
    own = select(Meeting.id).join(Series, Series.id == Meeting.series_id)
    stmt = (
        select(LlmCall)
        .where(
            or_(
                LlmCall.meeting_id.is_(None),
                LlmCall.meeting_id.in_(own.where(Series.owner_user_id == owner_id)),
            )
        )
        .order_by(LlmCall.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def latest_calls(db: Session, limit: int) -> list[LlmCall]:
    return list(db.scalars(select(LlmCall).order_by(LlmCall.created_at.desc()).limit(limit)))


def standalone_window(db: Session, window_sec: int) -> tuple[int, datetime | None, datetime]:
    """Вызовы без встречи за окно: число, самый старый и now БД."""
    now = func.clock_timestamp()
    row = db.execute(
        select(func.count(), func.min(LlmCall.created_at), now).where(
            LlmCall.meeting_id.is_(None),
            LlmCall.created_at > now - func.make_interval(0, 0, 0, 0, 0, 0, window_sec),
        )
    ).one()
    return row[0], row[1], row[2]
