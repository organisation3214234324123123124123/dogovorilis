from datetime import UTC, datetime

from app.core.db import SessionLocal, get_engine
from app.modules.auth import service
from app.worker import celery_app


@celery_app.task(name="app.modules.auth.tasks.cleanup_login_attempts")
def cleanup_login_attempts() -> int:
    with SessionLocal(bind=get_engine()) as db:
        deleted = service.purge_login_attempts(db, datetime.now(UTC))
        db.commit()
    return deleted
