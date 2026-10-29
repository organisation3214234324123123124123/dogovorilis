from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    status: int = 500
    title: str = "Internal Server Error"
    code: str = "INTERNAL_ERROR"
    # True: get_db фиксирует изменения сессии, хотя запрос завершается этой ошибкой.
    keep_changes: bool = False

    def __init__(
        self,
        detail: str | None = None,
        code: str | None = None,
        keep_changes: bool | None = None,
        field: str | None = None,
        **extra: object,
    ) -> None:
        super().__init__(detail)
        if field is not None:
            extra["errors"] = [{"field": field, "message": detail or ""}]
        if keep_changes is not None:
            self.keep_changes = keep_changes
        self.detail = detail
        self.extra = extra
        self.headers: dict[str, str] = {}
        if code is not None:
            self.code = code


class NotFound(AppError):
    status = 404
    title = "Not Found"
    code = "NOT_FOUND"


class Conflict(AppError):
    status = 409
    title = "Conflict"
    code = "CONFLICT"


class Forbidden(AppError):
    status = 403
    title = "Forbidden"
    code = "FORBIDDEN"


class Unauthorized(AppError):
    status = 401
    title = "Unauthorized"
    code = "UNAUTHORIZED"


class Gone(AppError):
    status = 410
    title = "Gone"
    code = "LINK_EXPIRED"


class ValidationFailed(AppError):
    status = 422
    title = "Unprocessable Entity"
    code = "VALIDATION_ERROR"


class RateLimited(AppError):
    status = 429
    title = "Too Many Requests"
    code = "RATE_LIMITED"

    def __init__(self, detail: str | None = None, *, retry_after: int) -> None:
        super().__init__(detail)
        self.headers = {"Retry-After": str(max(1, retry_after))}


class FieldError(BaseModel):
    field: str = Field(examples=["due_date"])
    message: str = Field(examples=["Срок не может быть в прошлом"])


class Problem(BaseModel):
    """Ошибка в формате RFC 9457 (application/problem+json)."""

    type: str = Field("about:blank", examples=["about:blank"])
    title: str = Field(examples=["Conflict"])
    status: int = Field(examples=[409])
    code: str = Field(
        description="Стабильный код для фронтенда",
        examples=["INVALID_TRANSITION"],
    )
    detail: str | None = Field(None, examples=["Переход из done в pending запрещён"])
    errors: list[FieldError] | None = Field(
        None, description="Ошибки по полям, только для 422 VALIDATION_ERROR и ошибок с полем"
    )
    open_gaps: int | None = Field(
        None, description="Только для GAPS_OPEN при рассылке", examples=[2]
    )


_ERROR_TEXT = {
    401: "Нет сессии или она истекла",
    403: "Нет доступа",
    404: "Объекта нет или он чужой",
    409: "Конфликт состояния; конкретная причина в поле code",
    410: "Ссылка истекла или отозвана",
    422: "Неверные данные; причина в поле code",
    429: "Превышен лимит, см. Retry-After",
}


ADMIN_401 = {401: "Нет или неверный X-Admin-Token, либо ADMIN_TOKEN не задан"}


def errors(*statuses: int, notes: dict[int, str] | None = None) -> dict[int | str, dict]:
    """`responses=` для ручки: схема problem+json на каждый статус, `notes` заменяет текст."""
    notes = notes or {}
    return {
        s: {
            "model": Problem,
            "description": notes.get(s) or _ERROR_TEXT.get(s) or HTTPStatus(s).phrase,
            "content": {PROBLEM_JSON: {}},
        }
        for s in dict.fromkeys((*statuses, *notes))
    }


def problem_for_422(schema: dict) -> None:
    """FastAPI описывает 422 как HTTPValidationError, а обработчик отдаёт Problem."""
    problem_ref = {"$ref": "#/components/schemas/Problem"}
    for path_item in schema.get("paths", {}).values():
        for op in path_item.values():
            responses = op.get("responses") if isinstance(op, dict) else None
            if responses and "422" in responses:
                old = responses["422"].get("description", "")
                responses["422"] = {
                    "description": old if old != "Validation Error" else _ERROR_TEXT[422],
                    "content": {PROBLEM_JSON: {"schema": problem_ref}},
                }
    schemas = schema.get("components", {}).get("schemas", {})
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)


_HTTP_CODES = {404: "NOT_FOUND", 422: "VALIDATION_ERROR"}


def problem(
    request: Request,
    status: int,
    title: str,
    detail: str | None,
    code: str | None = None,
    extra: dict | None = None,
) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code or _HTTP_CODES.get(status, HTTPStatus(status).name),
        "detail": detail,
        **(extra or {}),
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def on_app_error(request: Request, exc: AppError) -> JSONResponse:
        response = problem(request, exc.status, exc.title, exc.detail, exc.code, exc.extra)
        response.headers.update(exc.headers)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        title = HTTPStatus(exc.status_code).phrase
        response = problem(request, exc.status_code, title, str(exc.detail))
        response.headers.update(exc.headers or {})
        return response

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [
            {
                "field": ".".join(
                    str(p) for p in (e["loc"][1:] if e["loc"][0] == "body" else e["loc"])
                ),
                "message": e["msg"],
            }
            for e in exc.errors()
        ]
        detail = "; ".join(f"{e['field']}: {e['message']}" for e in errs)
        return problem(
            request, 422, "Unprocessable Entity", detail, "VALIDATION_ERROR", {"errors": errs}
        )
