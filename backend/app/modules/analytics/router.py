from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.core.db import StreamSessionDep
from app.core.errors import ADMIN_403, errors
from app.core.security import AdminAccess
from app.modules.analytics import service
from app.modules.analytics.schemas import EventsExportQuery

CSV_HEADER = "name,created_at,user_id,series_id,meeting_id,item_id,props"

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[AdminAccess])


@router.get(
    "/export/events.csv",
    summary="Выгрузка событий в CSV",
    description="События analytics_events без ПДн за период. Служебный доступ: "
    "заголовок `X-Admin-Token`; сессия ведущего не подходит.",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/csv": {"example": CSV_HEADER}}},
        **errors(403, notes=ADMIN_403),
    },
)
def export_events(
    query: Annotated[EventsExportQuery, Query()], db: StreamSessionDep
) -> StreamingResponse:
    return StreamingResponse(service.export_events_csv(db, query), media_type="text/csv")
