import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.health import repository
from app.modules.health.schemas import EmailQueueHealth, Health, LlmHealth

logger = logging.getLogger(__name__)


def check(db: Session) -> Health:
    """Сбой БД не роняет ручку: отвечаем db=down и 503 (NFR-11)."""
    settings = get_settings()
    llm = LlmHealth(
        available=bool(settings.llm_api_key),
        provider="yandexgpt" if settings.llm_api_key else "stub",
    )
    try:
        db.execute(text("SELECT 1"))
        now = datetime.now(UTC)
        queued, failed, oldest = repository.email_queue(db, now)
        worker_last_run = repository.last_mail_sent_at(db)
    except Exception:
        logger.warning("health: database check failed", exc_info=True)
        db.rollback()
        return Health(
            status="down",
            version=settings.app_version,
            db="down",
            email_queue=EmailQueueHealth(queued=0, failed_24h=0),
            llm=llm,
            worker_last_run_at=None,
        )
    return Health(
        status="ok",
        version=settings.app_version,
        db="ok",
        email_queue=EmailQueueHealth(
            queued=queued,
            failed_24h=failed,
            oldest_queued_age_s=max(0, int((now - oldest).total_seconds())) if oldest else None,
        ),
        llm=llm,
        worker_last_run_at=worker_last_run,
    )
