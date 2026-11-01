from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EmailQueueHealth(BaseModel):
    queued: int = Field(examples=[0])
    failed_24h: int = Field(examples=[0])
    oldest_queued_age_s: int | None = Field(None, description="Возраст старейшего письма, секунд")


class LlmHealth(BaseModel):
    available: bool = Field(description="false - работает заглушка (нет ключа или модели)")
    provider: str = Field(
        description="Хост LLM_BASE_URL; stub - если LLM недоступна", examples=["stub"]
    )
    model: str = Field(
        description="Имя модели из LLM_MODEL; stub - если LLM недоступна", examples=["stub"]
    )


class Health(BaseModel):
    """NFR-11. 200: ok или degraded; 503: БД или Redis недоступны (status=down)."""

    status: Literal["ok", "degraded", "down"]
    version: str = Field(description="Версия бэка: фронт сверяет её со своей", examples=["dev"])
    db: Literal["ok", "down"]
    email_queue: EmailQueueHealth
    llm: LlmHealth
    worker_last_run_at: datetime | None = Field(
        description=(
            "Последний запуск воркера (с письмами или без); "
            "null - не запускался или Redis недоступен"
        )
    )
