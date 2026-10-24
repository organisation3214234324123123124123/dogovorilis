from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.modules.llm.models import LlmCall, LlmPrompt, LlmTask


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
