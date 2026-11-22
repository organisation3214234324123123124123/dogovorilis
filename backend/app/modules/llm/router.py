import uuid

from fastapi import APIRouter

from app.core.db import SessionDep
from app.core.errors import ADMIN_401, errors
from app.core.security import AdminAccess
from app.modules.llm import service
from app.modules.llm.models import LlmTask
from app.modules.llm.schemas import PromptCreate, PromptRead

router = APIRouter(tags=["admin"], dependencies=[AdminAccess])


@router.get("/admin/llm/prompts", include_in_schema=False)
@router.get(
    "/admin/prompts",
    summary="Версии промптов LLM",
    description="Контракт LLM: все версии по задачам, сначала новые. Служебный доступ: "
    "заголовок `X-Admin-Token`.",
    response_model=list[PromptRead],
    responses=errors(401, notes=ADMIN_401),
)
def list_prompts(db: SessionDep, task: LlmTask | None = None) -> list[PromptRead]:
    return [PromptRead.model_validate(p) for p in service.list_prompts(db, task)]


@router.post("/admin/llm/prompts", status_code=201, include_in_schema=False)
@router.post(
    "/admin/prompts",
    summary="Новая версия промпта",
    description="Создаёт неактивную версию. Активная версия не меняется",
    response_model=PromptRead,
    status_code=201,
    responses=errors(401, 422, notes=ADMIN_401),
)
def create_prompt(body: PromptCreate, db: SessionDep) -> PromptRead:
    return PromptRead.model_validate(service.create_prompt(db, body))


@router.post(
    "/admin/prompts/{prompt_id}/activate",
    summary="Включить версию промпта",
    description="Делает версию единственной активной для её задачи; повторный вызов безопасен",
    response_model=PromptRead,
    responses=errors(401, 404, notes=ADMIN_401),
)
def activate_prompt(prompt_id: uuid.UUID, db: SessionDep) -> PromptRead:
    return PromptRead.model_validate(service.activate_prompt(db, prompt_id))
