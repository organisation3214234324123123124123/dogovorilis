import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, created_at_column, pg_enum


class LlmTask(StrEnum):
    PARSE = "parse"
    GAPS = "gaps"
    QUESTION = "question"


class LlmOutcome(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"


class LlmPrompt(Base):
    """Версии промптов; новые версии только новыми строками."""

    __tablename__ = "llm_prompts"
    __table_args__ = (
        UniqueConstraint("task", "version"),
        Index(
            "uq_llm_prompts_task_active", "task", unique=True, postgresql_where=text("is_active")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task: Mapped[LlmTask] = mapped_column(pg_enum(LlmTask, "llm_task"))
    version: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = created_at_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class LlmCall(Base):
    """Метрики вызова LLM. Текст запроса и ответа не хранится."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"), index=True
    )
    task: Mapped[LlmTask] = mapped_column(pg_enum(LlmTask, "llm_task"))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    prompt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_prompts.id", ondelete="RESTRICT")
    )
    latency_ms: Mapped[int | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    outcome: Mapped[LlmOutcome] = mapped_column(pg_enum(LlmOutcome, "llm_outcome"))
    created_at: Mapped[datetime] = created_at_column()
