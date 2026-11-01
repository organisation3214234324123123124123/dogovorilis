import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.modules.gaps.models import GapType
from app.modules.llm.models import LlmOutcome, LlmTask
from app.modules.notifications.models import NotificationType

DEFAULT_TYPES = [NotificationType.DUE_TOMORROW, NotificationType.CONFIRM_REPEAT]


class LlmCheckRequest(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=500,
        examples=["Маша до конца недели пришлёт обновлённый прайс клиенту, чтобы он подписал"],
    )
    series_id: uuid.UUID | None = Field(
        None, description="Серия, чьи участники ищутся в строке; пусто - первая активная"
    )


class GapView(BaseModel):
    type: GapType
    question: str


class RulesView(BaseModel):
    what: str | None
    assignee: str | None
    due_date: date | None
    due_text: str | None
    gaps: list[GapView]


class LlmView(BaseModel):
    outcome: LlmOutcome
    what: str | None
    assignee: str | None
    due_date: date | None
    due_text: str | None
    done_criteria: str | None
    vague_action: bool | None
    question: str | None


class LlmCheckResult(BaseModel):
    live: bool = Field(description="false - работает заглушка")
    provider: str = Field(examples=["api.groq.com"])
    model: str = Field(examples=["qwen/qwen3.8-27b"])
    elapsed_ms: int
    verdict: str
    rules: RulesView
    llm: LlmView


class LlmCallRead(BaseModel):
    created_at: datetime
    task: LlmTask
    provider: str
    model: str
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    outcome: LlmOutcome
    meeting_id: uuid.UUID | None


class LetterLink(BaseModel):
    url: str
    token: str
    action: str | None
    item_id: str | None
    kind: str | None


class Letter(BaseModel):
    received_at: datetime
    to: list[str]
    subject: str
    text: str
    links: list[LetterLink]


class Mailbox(BaseModel):
    enabled: bool
    hint: str | None = None
    letters: list[Letter]


class FastForwardRequest(BaseModel):
    types: list[NotificationType] = Field(DEFAULT_TYPES, min_length=1)
    series_id: uuid.UUID | None = Field(None, description="Только эта серия; пусто - все свои")


class MovedMail(BaseModel):
    type: NotificationType
    recipient_email: str | None
    item_what: str | None
    was_scheduled_for: datetime


class FastForwardResult(BaseModel):
    moved: list[MovedMail]
    hint: str


class LiveEvents(BaseModel):
    version: int = Field(description="Номер последнего события встречи")
    frames: list[dict[str, Any]] = Field(description="Кадры в формате схемы LiveEvent")
