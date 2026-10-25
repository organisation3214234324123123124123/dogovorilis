from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.llm.models import LlmPrompt, LlmTask


def activate_prompt(db: Session, task: LlmTask = LlmTask.PARSE, text: str = "PROMPT") -> LlmPrompt:
    db.execute(update(LlmPrompt).where(LlmPrompt.task == task).values(is_active=False))
    last = max(
        (v for v in db.scalars(select(LlmPrompt.version).where(LlmPrompt.task == task))),
        default=0,
    )
    prompt = LlmPrompt(task=task, version=last + 1, text=text, is_active=True)
    db.add(prompt)
    db.flush()
    return prompt
