import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_engine
from app.modules.analytics import service
from app.modules.confirm import service as confirm_service
from app.modules.items import service as items_service
from app.modules.llm import service as llm_service
from app.modules.notifications import service as notifications_service
from app.modules.series import service as series_service
from app.worker import celery_app

logger = logging.getLogger(__name__)

BATCH = 5000
YEAR = timedelta(days=365)
LINK_GRACE = timedelta(days=30)
PAYLOAD_TTL = timedelta(days=90)
ANALYTICS_TTL = timedelta(days=730)


def _in_batches(step: Callable[[Session, int], int]) -> int:
    total = 0
    while True:
        with SessionLocal(bind=get_engine()) as db:
            done = step(db, BATCH)
            db.commit()
        total += done
        if done < BATCH:
            return total


@celery_app.task(name="app.modules.analytics.tasks.cleanup_retention")
def cleanup_retention() -> dict[str, int]:
    """Сроки хранения из BR-20. Батчи по 5000 с commit, чтобы не держать длинную транзакцию;
    повторный запуск безопасен.
    """
    now = datetime.now(UTC)
    steps: dict[str, Callable[[Session, int], int]] = {
        "links": lambda db, n: confirm_service.purge_expired_links(db, now - LINK_GRACE, n),
        "payloads": lambda db, n: notifications_service.clear_old_payloads(
            db, now - PAYLOAD_TTL, n
        ),
        "events": lambda db, n: service.purge_old_events(db, now - ANALYTICS_TTL, n),
        "llm_calls": lambda db, n: llm_service.purge_old_calls(db, now - YEAR, n),
        "item_events": lambda db, n: items_service.purge_old_events(db, now - YEAR, n),
        "archived_items": lambda db, n: items_service.purge_items_of_archived_series(
            db, now - YEAR, n
        ),
    }
    result = {name: _in_batches(step) for name, step in steps.items()}
    with SessionLocal(bind=get_engine()) as db:
        result["persons"] = series_service.retire_archived(db, now - YEAR)
        db.commit()
    logger.info("retention cleanup done", extra={"deleted": result})
    return result
