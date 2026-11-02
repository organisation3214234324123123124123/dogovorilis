import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.core.db import SessionDep
from app.core.errors import errors
from app.core.security import CurrentUser
from app.modules.demo import service
from app.modules.demo.schemas import (
    Examples,
    FastForwardRequest,
    FastForwardResult,
    LiveEvents,
    LlmCallRead,
    LlmCheckRequest,
    LlmCheckResult,
    LlmTryRequest,
    Mailbox,
)

router = APIRouter(prefix="/demo", tags=["demo"])
PAGE = (Path(__file__).parent / "llm_page.html").read_text(encoding="utf-8")


@router.get("/llm/page", include_in_schema=False, response_class=HTMLResponse)
def llm_page() -> HTMLResponse:
    return HTMLResponse(PAGE)


@router.get(
    "/llm/examples",
    summary="Примеры для страницы нейросети",
    response_model=Examples,
)
def llm_examples(db: SessionDep) -> Examples:
    return service.examples(db)


@router.post(
    "/llm/examples/{example_id}/run",
    summary="Спросить нейросеть про пример",
    response_model=LlmCheckResult,
    responses=errors(404, 422, 429),
)
def llm_example_run(example_id: int, db: SessionDep) -> LlmCheckResult:
    return service.run_example(db, example_id)


@router.post(
    "/llm/try",
    summary="Своя строка для страницы нейросети",
    description="Без входа. Строка разбирается для выдуманной команды (Пётр Орлов, Дмитрий "
    "Белов, Светлана Ли) правилами и нейросетью; замок и лимит те же, что у примеров.",
    response_model=LlmCheckResult,
    responses=errors(422, 429),
)
def llm_try(body: LlmTryRequest, db: SessionDep) -> LlmCheckResult:
    return service.try_text(db, body.text.strip())


@router.post(
    "/llm/check",
    summary="Проверить нейросеть на строке",
    description="Одна строка решения проходит правила и нейросеть; видно, что нашёл каждый "
    "путь, сколько ушло времени и какая модель ответила. Вызов пишется в `llm_calls`.",
    response_model=LlmCheckResult,
    responses=errors(401, 404, 422, 429),
)
def llm_check(body: LlmCheckRequest, db: SessionDep, user: CurrentUser) -> LlmCheckResult:
    return service.llm_check(db, user, body)


@router.get(
    "/llm/calls",
    summary="Последние вызовы нейросети",
    description="Строки `llm_calls` (без текстов): модель, задержка, токены, исход.",
    response_model=list[LlmCallRead],
    responses=errors(401, 422),
)
def llm_calls(
    db: SessionDep, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[LlmCallRead]:
    return service.llm_calls(db, user, limit)


@router.get(
    "/mailbox",
    summary="Почтовый ящик демо-стенда",
    description="Письма, которые реально ушли по SMTP в Mailpit, со ссылками на страницу "
    "исполнителя (`/p/<token>`) разобранными на части. Только письма участникам ваших серий и вам.",
    response_model=Mailbox,
    responses=errors(401, 422),
)
def mailbox(
    db: SessionDep, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> Mailbox:
    return service.mailbox(db, user, limit)


@router.post(
    "/scheduler/fast-forward",
    summary="Перемотать время: отправить запланированные письма сейчас",
    description="Ставит запланированным письмам выбранных типов время «сейчас». Письма "
    "отправляет обычный Celery Beat (раз в минуту), эта ручка сама ничего не шлёт.",
    response_model=FastForwardResult,
    responses=errors(401, 404, 422),
)
def fast_forward(body: FastForwardRequest, db: SessionDep, user: CurrentUser) -> FastForwardResult:
    return service.fast_forward(db, user, body)


@router.get(
    "/meetings/{meeting_id}/live-events",
    summary="События живой панели",
    description="Те же кадры, что живая панель получает по WebSocket "
    "`/api/v1/ws/meetings/{id}`: события встречи после номера `since`.",
    response_model=LiveEvents,
    responses=errors(401, 404, 422),
)
def live_events(
    meeting_id: uuid.UUID,
    db: SessionDep,
    user: CurrentUser,
    since: Annotated[int, Query(ge=0)] = 0,
) -> LiveEvents:
    return service.live_events(db, user, meeting_id, since)
