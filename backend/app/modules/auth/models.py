import uuid
from datetime import datetime

from sqlalchemy import String, Uuid
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
