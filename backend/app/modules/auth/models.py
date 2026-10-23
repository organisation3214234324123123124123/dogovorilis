import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, created_at_column

DEFAULT_TIMEZONE = "Europe/Moscow"


class User(Base):
    """Ведущий встреч. В MVP учётные записи есть только у ведущих."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), server_default=DEFAULT_TIMEZONE)
    pd_consent_at: Mapped[datetime | None]  # согласие на обработку персональных данных (NFR-12)
    created_at: Mapped[datetime] = created_at_column()


class LoginAttempt(Base):
    """Неудачный вход (FR-02). Считается по e-mail, в том числе для адресов вне users."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_email_attempted_at", "email", "attempted_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(CITEXT)
    attempted_at: Mapped[datetime]


class PasswordResetToken(Base):
    """Ссылка сброса пароля. Хранится только SHA-256 токена, сам токен уходит в письме."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()
