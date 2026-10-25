from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import RequestLogMiddleware, setup_logging
from app.core.security import register_openapi_security
from app.modules.analytics.router import router as analytics_router
from app.modules.auth.router import router as auth_router
from app.modules.confirm.router import router as confirm_router
from app.modules.health.router import router as health_router
from app.modules.items.router import router as items_router
from app.modules.live.router import router as live_router
from app.modules.live.schemas import LiveEvent
from app.modules.llm.router import router as llm_router
from app.modules.series.router import router as series_router


def add_ws_schemas(app: FastAPI) -> None:
    """Добавляет в OpenAPI схему сообщений WebSocket, чтобы у фронтенда были типы."""
    build = app.openapi

    def openapi() -> dict:
        schema = build()
        schema.setdefault("components", {}).setdefault("schemas", {})["LiveEvent"] = (
            LiveEvent.model_json_schema(ref_template="#/components/schemas/{model}")
        )
        return schema

    app.openapi = openapi


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    app = FastAPI(
        title="Договорились API",
        version=settings.app_version,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        swagger_ui_parameters={"persistAuthorization": True},
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestLogMiddleware)
    register_error_handlers(app)
    register_openapi_security(app)
    add_ws_schemas(app)

    api = APIRouter(prefix="/api/v1")
    for router in (
        health_router,
        auth_router,
        series_router,
        items_router,
        confirm_router,
        analytics_router,
        llm_router,
    ):
        api.include_router(router)
    app.include_router(api)
    app.include_router(live_router)
    return app


app = create_app()
