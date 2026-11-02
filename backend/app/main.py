from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import RequestLogMiddleware, setup_logging
from app.core.security import register_openapi_security
from app.modules.analytics.router import router as analytics_router
from app.modules.auth.router import router as auth_router
from app.modules.confirm.router import router as confirm_router
from app.modules.demo.router import router as demo_router
from app.modules.health.router import router as health_router
from app.modules.items.router import router as items_router
from app.modules.live.router import router as live_router
from app.modules.live.schemas import LiveEvent
from app.modules.llm.router import router as llm_router
from app.modules.series.router import router as series_router

DESCRIPTION = """
**Договорились** помогает ведущему встречи довести решения до срока: что, кто, к какому числу.

**Вход:** `POST /api/v1/auth/login` -> Try it out -> Execute (тело уже заполнено:
`host@example.com / demo1234`). После этого cookie работает для всех ручек.
Локально демо-данные и вход через Authorize включает `compose.override.yaml` (`DEV_MODE=1`).

**Сценарий по шагам:**
1. Серия и участники (`series`).
2. Встреча и решения строкой (`meetings`, `items`).
3. Закрытие встречи и пробелы: нет срока, нет исполнителя.
4. «Согласовано на встрече».
5. «Разослать»: письма уходят исполнителям.
6. Письма и страница исполнителя по ссылке (`public-link`).
7. Напоминание (перемотка времени) и статусы.
8. Сверка на следующей встрече.

**Как увидеть интеграции:**
- нейросеть: `POST /demo/llm/check` и поле `llm` в `GET /health`;
- почта: `GET /demo/mailbox`;
- планировщик: `POST /demo/scheduler/fast-forward` и `worker_last_run_at` в `/health`;
- живая панель: `GET /demo/meetings/{id}/live-events` (в бою это WebSocket
  `/api/v1/ws/meetings/{id}`, схема `LiveEvent`).

Раздел `demo` есть только на демо-стенде.
Ошибки приходят как `application/problem+json` с полем `code`.
Изменение пункта идёт с заголовком `If-Match: <version>`.
"""

DEMO_BUTTON = """### [▶ Посмотреть, как отвечает нейросеть](/api/v1/demo/llm/page)
Без входа, настоящие ответы модели.
"""

DEMO_TAG = {
    "name": "demo",
    "description": "Для показа: живая проверка нейросети, страница с её ответами "
    "(`/api/v1/demo/llm/page`, без входа), письма, перемотка планировщика, "
    "события живой панели. Есть только на демо-стенде (DEV_MODE=1 и DEMO_DATA=1)",
}

TAGS = [
    {"name": "auth", "description": "Вход ведущего и выход"},
    {"name": "series", "description": "Серии встреч и их участники"},
    {"name": "meetings", "description": "Встречи: проведение, закрытие и рассылка итогов"},
    {"name": "items", "description": "Пункты решений и их карточки"},
    {"name": "public-link", "description": "Страница исполнителя по ссылке, без входа"},
    {"name": "admin", "description": "Служебные ручки, включая промпты LLM"},
    {"name": "health", "description": "Проба живости"},
]


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
    demo = settings.is_dev and settings.demo_data == "1"
    app = FastAPI(
        title="Договорились API",
        version=settings.app_version,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        description=DEMO_BUTTON + DESCRIPTION if demo else DESCRIPTION,
        openapi_tags=[*TAGS[:1], DEMO_TAG, *TAGS[1:]] if demo else TAGS,
        swagger_ui_parameters={
            "persistAuthorization": True,
            "tryItOutEnabled": True,
            "displayRequestDuration": True,
            "docExpansion": "list",
            "filter": True,
            "deepLinking": True,
            "defaultModelsExpandDepth": -1,
        },
    )
    # CORS добавлен последним (внешний слой): ответ 500 тоже получит CORS-заголовки.
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
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
        live_router,
    ):
        api.include_router(router)
    if demo:
        api.include_router(demo_router)
    app.include_router(api)
    return app


app = create_app()
