from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.modules.analytics import service as analytics
from app.modules.gaps import repository
from app.modules.gaps.models import Gap, GapSource, GapType
from app.modules.gaps.rules import default_question, find_gaps
from app.modules.items.models import Item


def _ids(item: Item) -> dict:
    return {"item_id": item.id, "meeting_id": item.meeting_id, "series_id": item.series_id}


def recalc(db: Session, item: Item, today: date, name: str | None) -> bool:
    """Пересчёт открытых пробелов (FR-20, FR-24).

    Снявшееся условие проставляет resolved_at, вернувшееся открывает новый пробел, а отклонённый
    тип заново не открывается. Пробелы от LLM правила не закрывают. Результат - изменилось ли что-
    нибудь.
    """
    now = datetime.now(UTC)
    rows = repository.item_gaps(db, item.id)
    dismissed = {g.type for g in rows if g.dismissed_at is not None}
    open_by_type = {g.type: g for g in rows if g.resolved_at is None and g.dismissed_at is None}
    wanted = [t for t in find_gaps(item, today) if t not in dismissed]
    changed = False
    for gap_type, gap in open_by_type.items():
        if gap_type not in wanted and gap.source == GapSource.RULE:
            gap.resolved_at = now
            seconds = max(0, int((now - gap.detected_at).total_seconds()))
            analytics.track(
                db,
                "gap_resolved",
                type=gap_type.value,
                seconds_since_detected=seconds,
                **_ids(item),
            )
            changed = True
    for gap_type in wanted:
        if gap_type in open_by_type:
            continue
        question = default_question(gap_type, name=name, due_text=item.due_text)
        repository.add(db, item.id, gap_type, GapSource.RULE, question)
        analytics.track(db, "gap_detected", type=gap_type.value, source="rule", **_ids(item))
        changed = True
    return changed


def dismiss(db: Session, item: Item, gap: Gap) -> bool:
    """False, если пробел уже закрыт или отклонён: тогда и события нет."""
    if not repository.dismiss(db, gap, datetime.now(UTC)):
        return False
    analytics.track(db, "gap_dismissed", type=gap.type.value, source=gap.source.value, **_ids(item))
    return True


def get_for_item(db: Session, item_id, gap_id) -> Gap | None:
    return repository.get_for_item(db, item_id, gap_id)


def has_open(db: Session, item: Item, types: tuple[GapType, ...]) -> bool:
    return any(
        g.type in types and g.resolved_at is None and g.dismissed_at is None
        for g in repository.item_gaps(db, item.id)
    )
