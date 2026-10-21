from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EmailQueueHealth(BaseModel):
    queued: int = Field(examples=[0])
    failed_24h: int = Field(examples=[0])
    oldest_queued_age_s: int | None = Field(None, description="Возраст старейшего письма, секунд")


class LlmHealth(BaseModel):
    available: bool = Field(description="false - работает заглушка (ключа нет)")
    provider: str = Field(examples=["stub"])


class Health(BaseModel):
    """Состояние сервиса; при недоступной БД ручка отвечает 503 (NFR-11)."""

    status: Literal["ok", "degraded", "down"]
    version: str = Field(description="Версия бэка: фронт сверяет её со своей", examples=["dev"])
    db: Literal["ok", "down"]
    email_queue: EmailQueueHealth
    llm: LlmHealth
    worker_last_run_at: datetime | None = Field(
        None, description="Когда воркер последний раз отправил письмо; null - ещё не отправлял"
    )
