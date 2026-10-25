import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Header, Query, Response

from app.core.db import SessionDep
from app.core.errors import PROBLEM_JSON, errors
from app.modules.confirm import service
from app.modules.confirm.schemas import (
    DeclineRequest,
    LinkExpiredProblem,
    PublicItem,
    PublicPage,
    RescheduleRequest,
)

CSRF_COOKIE = "csrf_token"
NotificationKind = Literal["SUMMARY", "CONFIRM_REQUEST", "CONFIRM_REPEAT", "DUE_TOMORROW"]


def _private_page_headers(response: Response) -> None:
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex"


router = APIRouter(
    prefix="/p/{token}", tags=["public-link"], dependencies=[Depends(_private_page_headers)]
)

_GONE = {
    410: {
        "model": LinkExpiredProblem,
        "description": "Ссылка истекла или отозвана (LINK_EXPIRED); в ответе только имя ведущего",
        "content": {PROBLEM_JSON: {}},
    }
}
_POST_ERRORS = {
    **errors(
        403,
        404,
        409,
        422,
        429,
        notes={
            403: "Чужой пункт (NOT_YOUR_ITEM) или нет CSRF-токена (CSRF_INVALID)",
            409: "Недопустимый переход (INVALID_TRANSITION) или серия в архиве (SERIES_ARCHIVED)",
        },
    ),
    **_GONE,
}


def read_csrf(
    header: Annotated[
        str | None, Header(alias="X-CSRF-Token", description="csrf_token из GET /p/{token}")
    ] = None,
    cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE, include_in_schema=False)] = None,
) -> str | None:
    """Double submit: заголовок со страницы должен совпасть с cookie SameSite=Strict (FR-32)."""
    return header if header and header == cookie else None


CsrfHeader = Annotated[str | None, Depends(read_csrf)]


@router.get(
    "",
    summary="Страница исполнителя по ссылке",
    description="Без входа. Пункты получателя по этой встрече и CSRF-токен. "
    "Ничего не меняет (BR-12): письма открывают почтовые сканеры. "
    "Демо-токены: demo-ivan, demo-anna, demo-oleg, demo-kate.",
    response_model=PublicPage,
    responses={
        200: {
            "description": "Страница и CSRF-токен; тот же токен в cookie для double submit",
            "headers": {
                "Set-Cookie": {
                    "description": "`csrf_token=<токен>; HttpOnly; SameSite=Strict; "
                    "Path=/api/v1/p`. Действия шлют этот токен ещё и в заголовке X-CSRF-Token",
                    "schema": {"type": "string"},
                }
            },
        },
        **errors(404, 429),
        **_GONE,
    },
)
def get_page(
    token: str,
    db: SessionDep,
    response: Response,
    n: Annotated[
        NotificationKind | None,
        Query(description="Тип письма, из которого открыта ссылка (для события link_opened)"),
    ] = None,
) -> PublicPage:
    page = service.get_page(db, token, n)
    response.set_cookie(
        CSRF_COOKIE, page.csrf_token, httponly=True, samesite="strict", path="/api/v1/p"
    )
    return page


@router.post(
    "/items/{item_id}/accept",
    summary="«Принимаю»",
    description="pending -> accepted; повтор CONFIRM_REPEAT отменяется.",
    response_model=PublicItem,
    responses=_POST_ERRORS,
)
def accept(
    token: str,
    item_id: uuid.UUID,
    db: SessionDep,
    csrf: CsrfHeader,
) -> PublicItem:
    return service.accept(db, token, item_id, csrf)


@router.post(
    "/items/{item_id}/reschedule",
    summary="«Нужен другой срок»",
    description="pending/accepted -> accepted с новым сроком (BR-6); ведущему HOST_RESPONSE.",
    response_model=PublicItem,
    responses=_POST_ERRORS,
)
def reschedule(
    token: str, item_id: uuid.UUID, body: RescheduleRequest, db: SessionDep, csrf: CsrfHeader
) -> PublicItem:
    return service.reschedule(db, token, item_id, csrf, body)


@router.post(
    "/items/{item_id}/decline",
    summary="«Это не моё»",
    description="pending/accepted -> disputed; комментарий до 500 символов; ведущему "
    "HOST_RESPONSE.",
    response_model=PublicItem,
    responses=_POST_ERRORS,
)
def decline(
    token: str, item_id: uuid.UUID, body: DeclineRequest, db: SessionDep, csrf: CsrfHeader
) -> PublicItem:
    return service.decline(db, token, item_id, csrf, body)


@router.post(
    "/items/{item_id}/done",
    summary="«Сделано»",
    description="Только из accepted -> done; ведущий не подтверждает.",
    response_model=PublicItem,
    responses=_POST_ERRORS,
)
def done(token: str, item_id: uuid.UUID, db: SessionDep, csrf: CsrfHeader) -> PublicItem:
    return service.done(db, token, item_id, csrf)
