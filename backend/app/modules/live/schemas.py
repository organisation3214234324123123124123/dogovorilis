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
    "resync.reset",
]


class LiveEvent(BaseModel):
    """Сообщение сервера в WS /api/v1/ws/meetings/{id} (FR-14)."""

    type: LiveEventType
    event: LiveEventType = Field(description="То же, что type")
    meeting_id: uuid.UUID
    item_id: uuid.UUID | None = None
    version: int = Field(
        description="Номер события встречи: идёт подряд с 1; по нему клиент находит дыру и dedup"
    )
    data: dict[str, Any] | list[Any] = Field(
        default_factory=dict,
        description=(
            "ItemRead (версия пункта в data.version); "
            "для gaps.updated - {item_id, gaps}, gaps - список GapRead"
        ),
    )
