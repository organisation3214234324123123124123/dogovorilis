import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Header, Query, Response

from app.core.db import SessionDep
from app.core.errors import errors
from app.core.security import CurrentUser
from app.modules.items import service
from app.modules.items.models import ItemStatus
from app.modules.items.schemas import (
    AcceptInMeetingRequest,
    ItemActionRequest,
    ItemCard,
    ItemCreate,
    ItemEventRead,
    ItemPage,
    ItemRead,
    ItemUpdate,
)

router = APIRouter(tags=["items"])

IfMatch = Annotated[
    str | None,
    Header(
        alias="If-Match",
        description='Версия пункта из поля `version` или заголовка ETag, например `3` или `"3"`',
    ),
]
_VERSION_409 = {
    409: "VERSION_CONFLICT - пункт уже изменён; INVALID_TRANSITION - недопустимый переход; "
    "SERIES_ARCHIVED - серия в архиве"
}


@router.post(
    "/meetings/{meeting_id}/items",
    summary="Добавить решение строкой",
    description="Строка до 1000 символов. Пункт создаётся в draft, поля раскладываются правилами "
    "в панель уходят item.created и item.updated. Встреча "
    "должна быть в live, closing или sent: пункт, добавленный после рассылки, уходит повторным "
    "«Разослать» (OQ-02).",
    response_model=ItemRead,
    status_code=201,
    responses=errors(
        401,
        404,
        409,
        422,
        429,
        notes={409: "Встреча ещё не началась (MEETING_NOT_LIVE); серия в архиве (SERIES_ARCHIVED)"},
    ),
)
def create_item(
    meeting_id: uuid.UUID, body: ItemCreate, db: SessionDep, user: CurrentUser
) -> ItemRead:
    return service.create_item(db, user, meeting_id, body)


@router.get(
    "/items/{item_id}",
    summary="Карточка пункта",
    description="Поля, статус, открытые пробелы и последний ответ исполнителя. "
    "Заголовок ETag - версия для If-Match.",
    response_model=ItemCard,
    responses=errors(401, 404),
)
def get_item(item_id: uuid.UUID, db: SessionDep, user: CurrentUser, response: Response) -> ItemCard:
    card = service.get_card(db, user, item_id)
    response.headers["ETag"] = f'"{card.version}"'
    return card


@router.patch(
    "/items/{item_id}",
    summary="Править пункт",
    description="В панели правятся все поля. После рассылки - только «что», срок и «готово», "
    "исполнителя меняют через actions/reassign. Нужен If-Match. Смена срока после рассылки "
    "увеличивает reschedule_count и отправляет исполнителю письмо ITEM_CHANGED.",
    response_model=ItemRead,
    responses=errors(401, 404, 409, 422, 428, notes=_VERSION_409),
)
def update_item(
    item_id: uuid.UUID,
    body: ItemUpdate,
    db: SessionDep,
    user: CurrentUser,
    if_match: IfMatch = None,
) -> ItemRead:
    return service.update_item(db, user, item_id, if_match, body)


@router.delete(
    "/items/{item_id}",
    summary="Удалить пункт в draft",
    description="Только до рассылки, вместе с пробелами. "
    "После рассылки - 409, пункт можно только «Снять».",
    status_code=204,
    responses=errors(401, 404, 409, 428, notes=_VERSION_409),
)
def delete_item(
    item_id: uuid.UUID, db: SessionDep, user: CurrentUser, if_match: IfMatch = None
) -> None:
    service.delete_item(db, user, item_id, if_match)


@router.get(
    "/items/{item_id}/history",
    summary="История пункта",
    description="Журнал item_events: кто, что, когда. Доступен только ведущему - владельцу серии.",
    response_model=list[ItemEventRead],
    responses=errors(401, 404),
)
def item_history(item_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> list[ItemEventRead]:
    return service.history(db, user, item_id)


@router.post(
    "/items/{item_id}/gaps/{gap_id}/dismiss",
    summary="«Не пробел»",
    description="Ставит dismissed_at; пробел этого типа по пункту больше не показывается. "
    "Повторный вызов - 200 без нового события.",
    response_model=ItemRead,
    responses=errors(401, 404, 409, notes={409: "Серия в архиве (SERIES_ARCHIVED)"}),
)
def dismiss_gap(
    item_id: uuid.UUID, gap_id: uuid.UUID, db: SessionDep, user: CurrentUser
) -> ItemRead:
    return service.dismiss_gap(db, user, item_id, gap_id)


@router.post(
    "/items/{item_id}/accept-in-meeting",
    summary="«Согласовано на встрече»",
    description="`value: true` ставит отметку, если исполнитель присутствует, есть due_date и нет "
    "блокирующих пробелов (BR-3); `false` снимает её (OQ-10). Статус остаётся draft до "
    "рассылки. Нужен If-Match.",
    response_model=ItemRead,
    responses=errors(
        401,
        404,
        409,
        422,
        428,
        notes={
            409: "VERSION_CONFLICT; встреча уже разослана (ALREADY_SENT); "
            "серия в архиве (SERIES_ARCHIVED)",
            422: "Исполнитель отсутствует или открыты пробелы (GAPS_OPEN, NOT_A_PARTICIPANT)",
        },
    ),
)
def accept_in_meeting(
    item_id: uuid.UUID,
    body: AcceptInMeetingRequest,
    db: SessionDep,
    user: CurrentUser,
    if_match: IfMatch = None,
) -> ItemRead:
    return service.accept_in_meeting(db, user, item_id, if_match, body)


@router.post(
    "/items/{item_id}/actions",
    summary="Действие ведущего над пунктом",
    description="done, reschedule (новый срок), cancel («Снять»), reassign (из pending, accepted "
    "или disputed в pending у нового исполнителя). Нужен If-Match.",
    response_model=ItemRead,
    responses=errors(401, 404, 409, 422, 428, notes=_VERSION_409),
)
def item_action(
    item_id: uuid.UUID,
    body: Annotated[
        ItemActionRequest,
        Body(
            openapi_examples={
                "reschedule": {
                    "summary": "Перенести",
                    "value": {"action": "reschedule", "due_date": "2026-11-06"},
                },
                "cancel": {"summary": "Снять", "value": {"action": "cancel"}},
                "done": {"summary": "Сделано", "value": {"action": "done"}},
            }
        ),
    ],
    db: SessionDep,
    user: CurrentUser,
    if_match: IfMatch = None,
) -> ItemRead:
    return service.apply_action(db, user, item_id, if_match, body)


@router.get(
    "/series/{series_id}/items",
    summary="Пункты серии",
    description="История пунктов серии. Фильтры по статусу, исполнителю, встрече и просрочке; "
    "сначала новые. Пагинация limit/offset. `on_time_share_30d` - доля сделанного в срок "
    "за 30 дней.",
    response_model=ItemPage,
    responses=errors(401, 404),
)
def series_items(
    series_id: uuid.UUID,
    db: SessionDep,
    user: CurrentUser,
    status: Annotated[
        list[ItemStatus] | None, Query(description="Один или несколько статусов")
    ] = None,
    assignee_person_id: Annotated[
        uuid.UUID | None, Query(description="Только пункты этого исполнителя")
    ] = None,
    meeting_id: Annotated[uuid.UUID | None, Query(description="Только пункты этой встречи")] = None,
    overdue: Annotated[bool, Query(description="Только просроченные")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ItemPage:
    return service.list_series_items(
        db,
        user,
        series_id,
        status=status,
        assignee_person_id=assignee_person_id,
        meeting_id=meeting_id,
        overdue=overdue,
        limit=limit,
        offset=offset,
    )
