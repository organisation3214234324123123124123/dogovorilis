import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_EMAIL = re.compile(r"([\w.+-])[\w.+-]*@([\w-]+\.[\w.-]+)")
_LINK_TOKEN = re.compile(r"^(/(?:api/v1/)?p/)[^/?]+")
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def mask_emails(text: str) -> str:
    return _EMAIL.sub(r"\1***@\2", text)


def mask_link_token(path: str) -> str:
    return _LINK_TOKEN.sub(r"\1***", path)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_emails(record.getMessage()),
            "request_id": request_id_var.get(),
        }
        data.update(
            {k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "request_id"}
        )
        if record.exc_info:
            data["exc"] = mask_emails(self.formatException(record.exc_info))
        return json.dumps(data, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True


class RequestLogMiddleware(BaseHTTPMiddleware):
    logger = logging.getLogger("app.request")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self.logger.exception(
                "unhandled error", extra={"path": mask_link_token(request.url.path)}
            )
            raise
        else:
            response.headers["X-Request-ID"] = request_id
            self.logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": mask_link_token(request.url.path),
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            return response
        finally:
            request_id_var.reset(token)
