import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.llm.models import LlmTask


class PromptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task: LlmTask = Field(description="parse, gaps или question")
    version: int = Field(examples=[2])
    text: str = Field(description="Текст промпта")
    is_active: bool
    created_at: datetime


class PromptCreate(BaseModel):
    task: LlmTask
    text: str = Field(min_length=1, max_length=20000, description="Текст новой версии")
    activate: bool = Field(True, description="Сделать активной сразу (флаг is_active - OQ)")
