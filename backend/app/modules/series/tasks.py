import uuid
from datetime import UTC, datetime

from app.core.db import SessionLocal, get_engine
from app.modules.series.service import open_due_closures, suggest_agenda_links
from app.worker import celery_app


@celery_app.task(name="app.modules.series.tasks.open_closures")
def open_closures() -> int:
    with SessionLocal(bind=get_engine()) as db:
        opened = open_due_closures(db, datetime.now(UTC))
        db.commit()
    return opened


@celery_app.task(name="app.modules.series.tasks.match_agenda")
def match_agenda(meeting_id: str) -> int:
    with SessionLocal(bind=get_engine()) as db:
        matched = suggest_agenda_links(db, uuid.UUID(meeting_id))
        db.commit()
    return matched
