import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.health import heartbeat, repository
from app.modules.health.schemas import EmailQueueHealth, Health, LlmHealth
from app.modules.llm.client import llm_configured

logger = logging.getLogger(__name__)

MAIL_STUCK_AFTER_S = 5 * 60
WORKER_SILENT_AFTER = timedelta(minutes=3)


def check(db: Session) -> Health:
    """Сбой БД или Redis не роняет ручку: отвечаем 503 и status=down (NFR-11).

    degraded (200): заглушка LLM, старейшее письмо ждёт больше 5 минут, воркер молчит больше 3.
    """
    settings = get_settings()
    available = llm_configured()
    llm = LlmHealth(
        available=available,
        provider=(urlsplit(settings.llm_base_url).hostname or "unknown") if available else "stub",
    )
    now = datetime.now(UTC)
    queued, failed, oldest, worker_last_run = 0, 0, None, None
    db_ok = redis_ok = True
    try:
        db.execute(text("SELECT 1"))
        queued, failed, oldest = repository.email_queue(db, now)
    except Exception:
        logger.warning("health: database check failed", exc_info=True)
        db.rollback()
        db_ok = False
    try:
        worker_last_run = heartbeat.last_run()
    except redis.RedisError:
        logger.warning("health: redis check failed", exc_info=True)
        redis_ok = False
    age = max(0, int((now - oldest).total_seconds())) if oldest else None
    if not (db_ok and redis_ok):
        status = "down"
    elif (
        not llm.available
        or (age or 0) > MAIL_STUCK_AFTER_S
        or (worker_last_run and now - worker_last_run > WORKER_SILENT_AFTER)
    ):
        status = "degraded"
    else:
        status = "ok"
    return Health(
        status=status,
        version=settings.app_version,
        db="ok" if db_ok else "down",
        email_queue=EmailQueueHealth(queued=queued, failed_24h=failed, oldest_queued_age_s=age),
        llm=llm,
        worker_last_run_at=worker_last_run,
    )
