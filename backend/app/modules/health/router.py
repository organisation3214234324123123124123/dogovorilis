from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.db import SessionDep
from app.modules.health import service
from app.modules.health.schemas import Health

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=Health,
    summary="Проба живости",
    responses={503: {"model": Health, "description": "База недоступна (db=down)"}},
)
def health(db: SessionDep) -> Health | JSONResponse:
    result = service.check(db)
    if result.db == "down":
        return JSONResponse(result.model_dump(mode="json"), status_code=503)
    return result
