import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

LiveEventType = Literal[
    "item.created",
    "item.updated",
    "item.deleted",
    "gaps.updated",
    "closure.opened",
    "agenda.updated",
    "meeting.sent",
]


class LiveEvent(BaseModel):
    """Сообщение сервера в WS /ws/meetings/{id} (FR-14)."""

    type: LiveEventType
    event: LiveEventType = Field(description="То же, что type")
    meeting_id: uuid.UUID
    item_id: uuid.UUID | None = None
    version: int | None = Field(
        None, description="Версия пункта: по ней клиент отличает новое от устаревшего"
    )
    data: dict[str, Any] = Field(default_factory=dict, description="ItemRead или список GapRead")
