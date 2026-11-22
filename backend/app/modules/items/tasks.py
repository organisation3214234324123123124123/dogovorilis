import logging
import uuid
from datetime import UTC, datetime

import redis

from app.core.config import get_settings
from app.core.db import SessionLocal, get_engine
from app.modules.items import service
from app.worker import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.modules.items.tasks.emit_overdue_events")
def emit_overdue_events() -> int:
    with SessionLocal(bind=get_engine()) as db:
        emitted = service.emit_overdue_events(db, datetime.now(UTC))
        db.commit()
    return emitted


@celery_app.task(
    bind=True,
    name="app.modules.items.tasks.enrich_item",
    soft_time_limit=15,
    time_limit=20,
    max_retries=2,
    acks_late=True,
    reject_on_worker_lost=True,
)
def enrich_item(self, item_id: str) -> bool:
    lock_key = f"llm:item-enrichment:lock:{item_id}"
    lock_id = str(uuid.uuid4())
    try:
        cache = redis.Redis.from_url(get_settings().redis_url)
        if not cache.set(lock_key, lock_id, nx=True, ex=25):
            return False
        try:
            return service.enrich_item_background(uuid.UUID(item_id))
        finally:
            cache.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                lock_id,
            )
    except Exception:
        logger.warning("Не удалось завершить фоновый разбор пункта")
        raise self.retry(countdown=2 ** self.request.retries) from None
