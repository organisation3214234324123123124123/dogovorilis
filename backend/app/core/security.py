import hmac
import time
import uuid
from typing import Annotated

import jwt
from fastapi import Depends, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.security import APIKeyCookie, APIKeyHeader, OAuth2PasswordBearer
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import Forbidden, Unauthorized
from app.modules.auth.models import User

password_hash = PasswordHash.recommended()

ALGORITHM = "HS256"
SESSION_COOKIE = "access_token"
SESSION_TTL_SECONDS = 7 * 24 * 3600


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def create_access_token(user_id: uuid.UUID, ttl: int = SESSION_TTL_SECONDS) -> str:
    now = int(time.time())
    claims = {"sub": str(user_id), "iat": now, "exp": now + ttl}
    return jwt.encode(claims, get_settings().secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> uuid.UUID | None:
    try:
        claims = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
        return uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError):
        return None


def token_expiry(token: str) -> int | None:
    try:
        return int(jwt.decode(token, options={"verify_signature": False})["exp"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None


# Cookie for the SPA (FR-01), bearer for Swagger "Authorize" and scripts.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)
cookie_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)


def get_current_user(
    cookie: Annotated[str | None, Depends(cookie_scheme)] = None,
    bearer: Annotated[str | None, Depends(oauth2_scheme)] = None,
    db: Annotated[Session | None, Depends(get_db, scope="function")] = None,
) -> User:
    token = cookie or bearer
    user_id = decode_access_token(token) if token else None
    user = db.get(User, user_id) if user_id and db is not None else None
    if user is None:
        raise Unauthorized("Нужно войти")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

# /admin/* (OQ-07): только по заголовку X-Admin-Token, сессия ведущего доступа не даёт.
admin_scheme = APIKeyHeader(
    name="X-Admin-Token",
    scheme_name="AdminToken",
    description="Служебный доступ: значение ADMIN_TOKEN. Если оно пусто, /admin выключен.",
    auto_error=False,
)


def require_admin(token: Annotated[str | None, Depends(admin_scheme)] = None) -> None:
    expected = get_settings().admin_token
    if not expected:
        raise Forbidden("Служебный доступ выключен (ADMIN_TOKEN пуст)", code="ADMIN_DISABLED")
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise Forbidden("Нужен заголовок X-Admin-Token", code="ADMIN_FORBIDDEN")


AdminAccess = Depends(require_admin)


def register_openapi_security(app: FastAPI) -> None:
    def build() -> dict:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                routes=app.routes,
                summary=app.summary,
                description=app.description,
                tags=app.openapi_tags,
            )
            schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
            for scheme in (oauth2_scheme, cookie_scheme, admin_scheme):
                schemes[scheme.scheme_name] = scheme.model.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = build
