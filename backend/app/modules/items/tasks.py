from datetime import UTC, datetime

from app.core.db import SessionLocal, get_engine
from app.modules.items import service
from app.worker import celery_app


@celery_app.task(name="app.modules.items.tasks.emit_overdue_events")
def emit_overdue_events() -> int:
    with SessionLocal(bind=get_engine()) as db:
        emitted = service.emit_overdue_events(db, datetime.now(UTC))
        db.commit()
    return emitted
