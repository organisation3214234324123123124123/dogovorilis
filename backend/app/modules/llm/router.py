from fastapi import APIRouter

from app.core.db import SessionDep
from app.core.errors import ADMIN_403, errors
from app.core.security import AdminAccess
from app.modules.llm import service
from app.modules.llm.schemas import PromptCreate, PromptRead

router = APIRouter(prefix="/admin/llm", tags=["admin"], dependencies=[AdminAccess])


@router.get(
    "/prompts",
    summary="Версии промптов LLM",
    description="Контракт LLM: все версии по задачам, сначала новые. Служебный доступ: "
    "заголовок `X-Admin-Token`.",
    response_model=list[PromptRead],
    responses=errors(403, notes=ADMIN_403),
)
def list_prompts(db: SessionDep) -> list[PromptRead]:
    return [PromptRead.model_validate(p) for p in service.list_prompts(db)]


@router.post(
    "/prompts",
    summary="Новая версия промпта",
    description="Только новая строка в llm_prompts, старые не меняются. При "
    "`activate=true` новая версия становится единственной активной для задачи.",
    response_model=PromptRead,
    status_code=201,
    responses=errors(403, 422, notes=ADMIN_403),
)
def create_prompt(body: PromptCreate, db: SessionDep) -> PromptRead:
    return PromptRead.model_validate(service.create_prompt(db, body))
