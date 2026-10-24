from datetime import date
from typing import Protocol

from app.modules.gaps.models import GapType

QUESTION_LIMIT = 120
VAGUE_ACTION_STEMS = (
    "разобра",
    "разбер",
    "проработ",
    "прорабат",
    "посмотр",
    "подум",
)


class Gappable(Protocol):
    what: str | None
    assignee_person_id: object | None
    due_date: date | None
    due_text: str | None


def _is_vague_action(what: str | None) -> bool:
    if not what:
        return False
    words = (w.strip(".,;:!?«»\"'()").lower() for w in what.split())
    return any(w.startswith(VAGUE_ACTION_STEMS) for w in words)


def find_gaps(item: Gappable, today: date) -> list[GapType]:
    """Типы пробелов по правилам OQ-09. Уже отклонённые отсеивает сервис (FR-24)."""
    found: list[GapType] = []
    if item.assignee_person_id is None:
        found.append(GapType.NO_ASSIGNEE)
    if item.due_date is None:
        found.append(GapType.DUE_VAGUE if item.due_text else GapType.NO_DUE)
    elif item.due_date < today:
        found.append(GapType.DUE_PAST)
    if _is_vague_action(item.what):
        found.append(GapType.VAGUE_ACTION)
    return found


def default_question(
    gap_type: GapType, *, name: str | None = None, due_text: str | None = None
) -> str:
    match gap_type:
        case GapType.NO_ASSIGNEE:
            text = "Кто берёт это на себя?"
        case GapType.NO_DUE:
            text = f"{name[:60]}, к какому числу?" if name else "К какому числу?"
        case GapType.DUE_VAGUE:
            phrase = (due_text or "срок").strip()[:60]
            text = f"«{phrase[:1].upper()}{phrase[1:]}» — это к какому числу?"
        case GapType.DUE_PAST:
            text = "Этот срок уже прошёл — какой новый?"
        case GapType.VAGUE_ACTION:
            text = "Что будет результатом?"
    return text[:QUESTION_LIMIT]
