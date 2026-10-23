import os
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

celery_app = Celery("leaderhelp", broker=get_settings().redis_url)
modules_dir = Path(__file__).parent / "modules"
celery_app.autodiscover_tasks(
    [f"app.modules.{path.parent.name}" for path in sorted(modules_dir.glob("*/tasks.py"))]
)
celery_app.conf.task_always_eager = os.environ.get("CELERY_EAGER") == "1"
celery_app.conf.beat_schedule = {
    "emit-overdue-events": {
        "task": "app.modules.items.tasks.emit_overdue_events",
        "schedule": crontab(minute="*/5"),
    },
    "send-due-mails": {
        "task": "app.modules.notifications.tasks.send_due_mails",
        "schedule": crontab(minute="*"),
    },
    "open-due-closures": {
        "task": "app.modules.series.tasks.open_closures",
        "schedule": crontab(minute="*"),
    },
    "cleanup-login-attempts": {
        "task": "app.modules.auth.tasks.cleanup_login_attempts",
        "schedule": 3600.0,
    },
    "cleanup-retention": {
        "task": "app.modules.analytics.tasks.cleanup_retention",
        "schedule": crontab(hour=3, minute=0),
    },
}
