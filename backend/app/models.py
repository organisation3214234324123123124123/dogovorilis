"""Все ORM-модели в одном модуле, чтобы autogenerate Alembic и загрузчик демо видели все таблицы."""

from app.core.db import Base
from app.modules.analytics.models import AnalyticsEvent
from app.modules.auth.models import LoginAttempt, PasswordResetToken, User
from app.modules.confirm.models import AccessLink, AccessLinkToken
from app.modules.gaps.models import Gap
from app.modules.items.models import Item, ItemComment, ItemEvent
from app.modules.llm.models import LlmCall, LlmPrompt
from app.modules.notifications.models import Notification
from app.modules.series.models import (
    AgendaItem,
    Meeting,
    MeetingParticipant,
    Organization,
    Person,
    Series,
    SeriesMember,
)

__all__ = [
    "AccessLink",
    "AccessLinkToken",
    "AgendaItem",
    "AnalyticsEvent",
    "Base",
    "Gap",
    "Item",
    "ItemComment",
    "ItemEvent",
    "LlmCall",
    "LlmPrompt",
    "LoginAttempt",
    "Meeting",
    "MeetingParticipant",
    "Notification",
    "Organization",
    "PasswordResetToken",
    "Person",
    "Series",
    "SeriesMember",
    "User",
]
