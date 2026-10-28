from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.db import SessionDep
from app.core.errors import NotFound, ValidationFailed, errors
from app.core.security import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    CurrentUser,
    create_access_token,
)
from app.modules.auth import service
from app.modules.auth.schemas import (
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RegisterRequest,
    TokenRead,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=not get_settings().is_dev,
        path="/",
    )


@router.post(
    "/login",
    summary="Вход ведущего",
    description="Ставит httpOnly-cookie `session` (JWT, 7 дней), в теле - пользователь, "
    "токена в теле нет. Демо (при DEMO_DATA=1): host@example.com / demo1234.",
    response_model=UserRead,
    responses=errors(401, 422, 429, notes={401: "Неверный e-mail или пароль (UNAUTHORIZED)"}),
)
def login(body: LoginRequest, db: SessionDep, response: Response) -> UserRead:
    user = service.login(db, body)
    _set_session(response, create_access_token(user.id))
    return UserRead.model_validate(user)


@router.post(
    "/token",
    summary="Вход через форму OAuth2",
    description="Для кнопки Swagger «Authorize» и скриптов: поле `username` - e-mail. "
    "Отдаёт Bearer-токен в теле и ставит cookie `session`, как `/auth/login`. "
    "Только при DEV_MODE=1, иначе 404.",
    response_model=TokenRead,
    responses=errors(401, 404, 422, 429, notes={401: "Неверный e-mail или пароль (UNAUTHORIZED)"}),
)
def login_form(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], db: SessionDep, response: Response
) -> TokenRead:
    if not get_settings().is_dev:
        raise NotFound()
    try:
        body = LoginRequest(email=form.username, password=form.password)
    except ValidationError as exc:
        raise ValidationFailed("Нужны e-mail и пароль") from exc
    user = service.login(db, body)
    token = service.issue_token(user)
    _set_session(response, token.access_token)
    return token


@router.post(
    "/logout",
    summary="Выход",
    description="Сбрасывает cookie сессии. Bearer-токен живёт до своего срока.",
    status_code=204,
)
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get(
    "/me",
    summary="Текущий ведущий",
    description="Кто вошёл. Фронт зовёт при старте, 401 - показать вход.",
    response_model=UserRead,
    responses=errors(401),
)
def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.post(
    "/register",
    summary="Регистрация ведущего",
    description="Без подтверждения e-mail. Без согласия на обработку ПДн - 422 (NFR-12); "
    "время согласия пишется в `users.pd_consent_at`. Создаёт свою сторону ведущего. "
    "Занятый e-mail - 409 `EMAIL_TAKEN`.",
    response_model=UserRead,
    status_code=201,
    responses=errors(409, 422, notes={409: "E-mail уже занят (EMAIL_TAKEN)"}),
)
def register(body: RegisterRequest, db: SessionDep) -> UserRead:
    return UserRead.model_validate(service.register(db, body))


@router.post(
    "/password-reset",
    summary="Запросить сброс пароля",
    description="Шлёт письмо со ссылкой на 1 час. Ответ одинаковый для любого e-mail.",
    status_code=202,
    responses=errors(422),
)
def password_reset(body: PasswordResetRequest, db: SessionDep) -> None:
    service.request_password_reset(db, body)


@router.post(
    "/password-reset/confirm",
    summary="Задать новый пароль по токену",
    description="Токен одноразовый, живёт 1 час; истёк или использован - 410.",
    status_code=204,
    responses=errors(410, 422, notes={410: "Токен истёк или уже использован (LINK_EXPIRED)"}),
)
def password_reset_confirm(body: PasswordResetConfirm, db: SessionDep) -> None:
    service.confirm_password_reset(db, body)
