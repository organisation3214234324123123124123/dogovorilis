import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.core.errors import Problem
from app.modules.items.models import ItemStatus

PublicAction = Literal["accept", "reschedule", "decline", "done"]


class PublicItem(BaseModel):
    """Пункт на странице исполнителя: без истории и чужих данных."""

    id: uuid.UUID
    what: str | None = Field(None, examples=["Выгрузить отчёт по оттоку за квартал"])
    due_date: date | None = Field(None, examples=["2026-10-30"])
    done_criteria: str | None = Field(None, examples=["Таблица в общей папке"])
    status: ItemStatus = Field(description="cancelled показывается как «снят» без кнопок")
    is_overdue: bool
    actions: list[PublicAction] = Field(
        description="Кнопки по статусу: pending - accept, reschedule, decline; "
        "accepted - done, reschedule, decline; остальные - нет",
        examples=[["accept", "reschedule", "decline"]],
    )
    max_due_date: date | None = Field(
        None, description="Позднейший допустимый новый срок: исходный + 60 дней (BR-6)"
    )


class PublicPage(BaseModel):
    """Данные страницы /p/{token}. GET ничего не меняет (BR-12)."""

    host_name: str = Field(examples=["Мария Соколова"])
    series_title: str = Field(examples=["Еженедельная планёрка по запуску оплаты"])
    meeting_start: datetime
    person_name: str = Field(examples=["Иван Петров"])
    timezone: str = Field(examples=["Europe/Moscow"])
    today: date = Field(description="Сегодня по поясу серии: нижняя граница нового срока")
    csrf_token: str = Field(description="Передать в X-CSRF-Token при любом POST")
    items: list[PublicItem]


class RescheduleRequest(BaseModel):
    due_date: date = Field(
        description="Не раньше сегодня и не позже исходного срока + 60 дней (BR-6)",
        examples=["2026-11-06"],
    )
    comment: str | None = Field(None, max_length=500, examples=["Жду данные от бухгалтерии"])


class DeclineRequest(BaseModel):
    comment: str | None = Field(
        None, max_length=500, description="Необязательно", examples=["Демо проводит Анна"]
    )


class LinkExpiredProblem(Problem):
    host_name: str | None = Field(None, description="Кому написать", examples=["Мария Соколова"])
