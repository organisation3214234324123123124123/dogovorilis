import uuid
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import available_timezones

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

EmailText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    Field(description="E-mail, регистр не важен", examples=["host@example.com"]),
]


def _check_tz(value: str) -> str:
    if value not in available_timezones():
        raise ValueError("Неизвестный часовой пояс, нужен IANA, например Europe/Moscow")
    return value


TimeZone = Annotated[
    str, AfterValidator(_check_tz), Field(description="Пояс IANA", examples=["Europe/Moscow"])
]
Password = Annotated[
    str, Field(min_length=8, max_length=128, description="Пароль", examples=["demo1234"])
]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str = Field(examples=["host@example.com"])
    full_name: str = Field(examples=["Мария Соколова"])
    timezone: str = Field(description="Пояс ведущего (IANA)", examples=["Europe/Moscow"])
    created_at: datetime


class LoginRequest(BaseModel):
    email: EmailText
    password: str = Field(min_length=1, max_length=128, examples=["demo1234"])


class TokenRead(BaseModel):
    """Ответ `/auth/token` (Swagger); тот же JWT ставится в cookie `session`."""

    access_token: str = Field(description="JWT для заголовка Authorization: Bearer")
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Срок жизни сессии, секунд", examples=[604800])
    user: UserRead


class RegisterRequest(BaseModel):
    email: EmailText
    password: Password
    full_name: str = Field(min_length=1, max_length=200, examples=["Мария Соколова"])
    timezone: TimeZone = "Europe/Moscow"
    pd_consent: bool = Field(
        description="Согласие на обработку ПДн (NFR-12); без него регистрация не проходит",
        examples=[True],
    )


class PasswordResetRequest(BaseModel):
    email: EmailText


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=1, description="Токен из письма сброса пароля")
    new_password: Password
