import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.gaps.models import GapSource, GapType
from app.modules.gaps.rules import QUESTION_LIMIT
from app.modules.items.models import (
    ActorType,
    DoneBy,
    ItemAction,
    ItemSource,
    ItemStatus,
)


class GapRead(BaseModel):
    """Пробел пункта (FR-20, FR-21). В пункте приходят только открытые."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: GapType = Field(examples=["no_due"])
    source: GapSource = Field(description="rule - правило, llm - нейросеть")
    question: str = Field(
        max_length=QUESTION_LIMIT,
        description="Готовый вопрос ведущему; если своего нет, подставляется шаблон",
        examples=["Иван, к какому числу?"],
    )
    detected_at: datetime
    blocking: bool = Field(False, description="Блокирует «Согласовано на встрече» (BR-3)")


class PersonRef(BaseModel):
    person_id: uuid.UUID
    full_name: str = Field(examples=["Иван Петров"])
    is_external: bool = Field(description="Из чужой организации, не своя сторона ведущего")


class LoadWarning(BaseModel):
    """У исполнителя набралось открытых пунктов не меньше порога серии (BR-9). Не запрет."""

    open_items: int = Field(
        description="Пункты в pending и accepted со сроком в ближайшие 7 дней", examples=[5]
    )


class ItemRead(BaseModel):
    """Пункт: одна договорённость встречи."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meeting_id: uuid.UUID
    series_id: uuid.UUID
    agenda_item_id: uuid.UUID | None = None
    raw_text: str = Field(
        description="Исходная строка ведущего", examples=["Иван разберётся с выгрузкой к пятнице"]
    )
    what: str | None = Field(None, description="Что сделать", examples=["Разобраться с выгрузкой"])
    assignee: PersonRef | None = Field(None, description="Исполнитель; null - не назначен")
    due_date: date | None = Field(None, description="Срок в поясе серии", examples=["2026-10-16"])
    due_text: str | None = Field(None, description="Срок словами из строки", examples=["к пятнице"])
    done_criteria: str | None = Field(
        None, description="Критерий «готово»", examples=["Файл выгрузки в общей папке"]
    )
    manual_fields: list[str] = Field(
        description="Поля, правленные ведущим руками; LLM их не трогает (BR-15)",
        examples=[["what"]],
    )
    source: ItemSource
    suggested: bool = Field(description="Предложен автоматически")
    status: ItemStatus
    is_overdue: bool = Field(description="Срок прошёл, а пункт в pending или accepted")
    accepted_in_meeting: bool = Field(description="Отмечено «Согласовано на встрече»")
    can_accept_in_meeting: bool = Field(description="Можно ли отметить «Согласовано» (BR-3)")
    accept_blocked_reason: str | None = Field(None, description="Почему нельзя; null, если можно")
    accepted_at: datetime | None = None
    sent_at: datetime | None = None
    done_at: datetime | None = None
    done_by: DoneBy | None = None
    cancelled_at: datetime | None = None
    reschedule_count: int = Field(description="Сколько раз меняли срок после рассылки")
    version: int = Field(description="Версия для If-Match", examples=[1])
    gaps: list[GapRead] = Field(description="Открытые пробелы")
    load_warning: LoadWarning | None = Field(
        None, description="null - нет исполнителя или нагрузка ниже порога серии"
    )
    created_at: datetime
    updated_at: datetime


class AssigneeResponse(BaseModel):
    kind: Literal["accept", "reschedule", "not_mine", "done"]
    due_date: date | None = Field(None, description="Новый срок для reschedule")
    comment: str | None = Field(
        None, description="Комментарий исполнителя", examples=["Демо проводит Анна"]
    )
    at: datetime


HostAction = Literal["edit", "done", "reschedule", "cancel", "reassign", "reopen"]


class ItemCard(ItemRead):
    """Карточка пункта для ведущего."""

    series_title: str = Field(examples=["Еженедельная планёрка по запуску оплаты"])
    meeting_planned_start: datetime
    last_response: AssigneeResponse | None = Field(
        None, description="Последний ответ исполнителя по ссылке"
    )
    allowed_actions: list[HostAction] = Field(
        description="Что ведущий может сделать с пунктом в текущем статусе"
    )


class ItemPage(BaseModel):
    items: list[ItemRead]
    total: int = Field(examples=[42])
    on_time_rate_30d: float | None = Field(
        None,
        description="Доля сделанного в срок за 30 дней (FR-44): пункты со сроком в последние "
        "30 дней, сделанные не позже срока, / все такие пункты, кроме снятых. Учитывает фильтр "
        "по исполнителю. null - таких пунктов нет",
        examples=[0.67],
    )


class ItemEventRead(BaseModel):
    """Строка журнала пункта: кто, что, когда."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    action: ItemAction
    actor_type: ActorType
    actor_name: str | None = Field(
        None, description="Имя ведущего или исполнителя", examples=["Иван Петров"]
    )
    data: dict[str, Any] = Field(
        description="Только id, статусы и даты (BR-19)",
        examples=[{"status_from": "draft", "status_to": "pending"}],
    )
    created_at: datetime


class ItemHistory(BaseModel):
    events: list[ItemEventRead]
    next_cursor: str | None = Field(
        None, description="Передать в cursor за следующей страницей; null - это конец"
    )


class ItemCreate(BaseModel):
    raw_text: str = Field(
        min_length=1,
        max_length=1000,
        description="Строка решения",
        examples=["Иван разберётся с выгрузкой к пятнице"],
    )
    agenda_item_id: uuid.UUID | None = Field(None, description="Пункт повестки этой встречи")

    @field_validator("raw_text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("строка не может быть пустой")
        return value


class ItemUpdate(BaseModel):
    """Правка полей (FR-13, FR-16). Переданные поля попадают в manual_fields.
    После рассылки исполнителя меняют только через actions/reassign."""

    model_config = ConfigDict(extra="forbid")

    what: str | None = Field(None, max_length=500, examples=["Выгрузить отчёт по оттоку"])
    assignee_person_id: uuid.UUID | None = Field(None, description="Только участник серии (BR-1)")
    due_date: date | None = Field(
        None, description="Не раньше сегодня (BR-2)", examples=["2026-10-30"]
    )
    due_text: str | None = Field(None, max_length=100)
    done_criteria: str | None = Field(None, max_length=500)


class AcceptInMeetingRequest(BaseModel):
    value: bool = Field(
        description="true - поставить «Согласовано на встрече», false - снять (OQ-10)",
        examples=[True],
    )


class DoneAction(BaseModel):
    action: Literal["done"]


class RescheduleAction(BaseModel):
    action: Literal["reschedule"]
    due_date: date = Field(description="Новый срок, не раньше сегодня", examples=["2026-11-06"])


class CancelAction(BaseModel):
    action: Literal["cancel"]


class ReassignAction(BaseModel):
    action: Literal["reassign"]
    assignee_person_id: uuid.UUID = Field(description="Новый исполнитель из состава серии")


class ReopenAction(BaseModel):
    action: Literal["reopen"] = Field(description="«Вернуть в работу»")


ItemActionRequest = Annotated[
    DoneAction | RescheduleAction | CancelAction | ReassignAction | ReopenAction,
    Field(discriminator="action"),
]
