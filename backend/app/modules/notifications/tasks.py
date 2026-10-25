from datetime import UTC, datetime

from app.core.db import SessionLocal, get_engine
from app.modules.notifications import sender
from app.worker import celery_app


@celery_app.task(name="app.modules.notifications.tasks.send_due_mails")
def send_due_mails() -> int:
    with SessionLocal(bind=get_engine()) as db:
        sent = sender.send_due(db, datetime.now(UTC))
        db.commit()
    return sent
