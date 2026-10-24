import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.modules.auth.schemas import EmailText, TimeZone
from app.modules.items.schemas import ItemRead
from app.modules.series.models import (
    AgendaItemStatus,
    CaptureMode,
    ClosureTrigger,
    MeetingStatus,
    SeriesStatus,
)

Duration = Annotated[
    int, Field(ge=15, le=240, description="Плановая длительность, минут", examples=[30])
]
Title = Annotated[
    str,
    Field(min_length=1, max_length=200, examples=["Еженедельная планёрка по запуску оплаты"]),
]


class MemberCreate(BaseModel):
    """Участник серии. Тот же e-mail у ведущего означает того же человека."""

    full_name: str = Field(min_length=1, max_length=200, examples=["Иван Петров"])
    email: EmailText
    organization_name: str = Field(
        min_length=1,
        max_length=200,
        description="Сторона: своя команда или внешняя организация; новая создаётся",
        examples=["ООО «Ромашка»"],
    )


class MemberRead(BaseModel):
    person_id: uuid.UUID
    full_name: str = Field(examples=["Иван Петров"])
    email: str | None = Field(
        None, description="null после обезличивания", examples=["ivan@example.com"]
    )
    organization_id: uuid.UUID
    organization_name: str = Field(examples=["Команда продукта"])
    is_own_side: bool = Field(description="Своя сторона ведущего")
    open_items: int = Field(
        0,
        description="Открытые пункты со сроком в ближайшие 7 дней, по всем сериям ведущего (FR-26)",
        examples=[3],
    )


class SeriesCreate(BaseModel):
    title: Title
    timezone: TimeZone = "Europe/Moscow"
    planned_duration_min: Duration
    capture_mode: CaptureMode = Field(CaptureMode.MANUAL, description="Способ записи решений")
    members: list[MemberCreate] = Field(default_factory=list, description="Стартовый состав")


class SeriesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    timezone: TimeZone | None = None
    planned_duration_min: Duration | None = None
    load_warning_threshold: int | None = Field(
        None, ge=1, description="Порог предупреждения о перегрузке, по умолчанию 5 (FR-26.7)"
    )


class SeriesRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str = Field(examples=["Еженедельная планёрка по запуску оплаты"])
    timezone: str = Field(examples=["Europe/Moscow"])
    planned_duration_min: int = Field(examples=[30])
    capture_mode: CaptureMode
    status: SeriesStatus
    created_at: datetime
    load_warning_threshold: int = Field(
        description="Сколько открытых пунктов у исполнителя за 7 дней должно набраться, чтобы "
        "показать предупреждение (BR-9)",
        examples=[5],
    )
    open_items_count: int = Field(description="Пункты серии в pending и accepted", examples=[8])
    members_count: int = Field(examples=[4])
    active_meeting_id: uuid.UUID | None = Field(
        None, description="Встреча в live или closing, если идёт"
    )
    last_meeting_at: datetime | None = Field(None, description="Начало последней встречи")


class SeriesDetail(SeriesRead):
    members: list[MemberRead]


class ParticipantRead(BaseModel):
    person_id: uuid.UUID
    full_name: str = Field(examples=["Анна Смирнова"])
    organization_name: str = Field(examples=["Команда продукта"])
    present: bool = Field(description="Присутствует")


class ParticipantPresence(BaseModel):
    person_id: uuid.UUID
    present: bool


class AgendaItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int = Field(examples=[1])
    title: str = Field(examples=["Статус интеграции с банком"])
    status: AgendaItemStatus
    uncovered: bool = Field(
        False,
        description="В режиме закрытия: тема открыта и ни одно решение к ней не привязано (FR-27)",
    )


class AgendaItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300, examples=["Статус интеграции с банком"])


class AgendaItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=1, max_length=300)
    position: int | None = Field(None, ge=1, le=20)
    status: AgendaItemStatus | None = None


class MeetingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    series_id: uuid.UUID
    planned_start: datetime
    planned_end: datetime
    status: MeetingStatus = Field(description="planned -> live -> closing -> sent")
    started_at: datetime | None = None
    closure_opened_at: datetime | None = None
    closure_trigger: ClosureTrigger | None = None
    summary_sent_at: datetime | None = None
    created_at: datetime


class MeetingDetail(MeetingRead):
    """Панель встречи: участники, повестка и пункты с открытыми пробелами."""

    series_title: str
    timezone: str = Field(description="Пояс серии", examples=["Europe/Moscow"])
    participants: list[ParticipantRead]
    agenda: list[AgendaItemRead]
    items: list[ItemRead]
    open_gaps_count: int = Field(description="Счётчик «осталось N пробелов»", examples=[2])


class MeetingStart(BaseModel):
    planned_end: datetime | None = Field(
        None, description="Плановый конец; по умолчанию сейчас + длительность серии"
    )


class ReviewRead(BaseModel):
    """Сверка в начале встречи (FR-42)."""

    overdue: list[ItemRead] = Field(description="Просроченные pending и accepted")
    disputed: list[ItemRead] = Field(description="«Это не моё»")
    stale_pending: list[ItemRead] = Field(description="Без ответа дольше 2 рабочих дней")
    unassigned: list[ItemRead] = Field(description="Разосланы без исполнителя (BR-4)")
    done_since_last: list[ItemRead] = Field(description="Сделано с прошлой встречи, одной строкой")


class MeetingStarted(BaseModel):
    meeting: MeetingDetail
    review: ReviewRead


class MeetingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planned_end: datetime | None = Field(None, description="Новый плановый конец")
    participants: list[ParticipantPresence] | None = Field(None, description="Отметки присутствия")


class SendRequest(BaseModel):
    confirm_open_gaps: bool = Field(
        False,
        description="Разослать, хотя пробелы остались (OQ-03). Без флага и с пробелами - "
        "422 GAPS_OPEN",
    )


class SendResult(BaseModel):
    meeting_id: uuid.UUID
    status: MeetingStatus
    summary_sent_at: datetime | None
    meeting: MeetingRead
    items_sent: int = Field(description="Пунктов разослано", examples=[6])
    notifications_queued: int = Field(description="Писем поставлено в очередь", examples=[5])
    already_sent: bool = Field(description="Повторное нажатие: писем не добавилось (BR-5)")
    items_total: int = Field(description="Пунктов во встрече", examples=[6])
    items_with_open_gaps: int = Field(description="Пунктов с открытыми пробелами", examples=[1])
    items_unassigned: int = Field(description="Пунктов без исполнителя (BR-4)", examples=[1])
    recipients: int = Field(description="Участников встречи (FR-28.8)", examples=[5])


class SummaryText(BaseModel):
    """Итог простым текстом для мессенджера, без ссылок /p/ и токенов (FR-36)."""

    text: str = Field(
        examples=["Итоги встречи 16.10\n1. Разобраться с выгрузкой — Иван Петров, до 23.10"]
    )
    items_total: int = Field(examples=[6])


class AnonymizeResult(BaseModel):
    person_id: uuid.UUID
    full_name: str = Field(description="Новое имя", examples=["Участник 7"])
    anonymized_at: datetime
    links_revoked: int = Field(examples=[2])
    comments_cleared: int = Field(examples=[1])
