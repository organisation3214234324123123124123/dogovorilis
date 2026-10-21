import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, created_at_column


class AccessLink(Base):
    """Ссылка исполнителя на встречу. token_hash: SHA-256 токена, который никому не отправляется;
    токены писем лежат в access_link_tokens."""

    __tablename__ = "access_links"
    __table_args__ = (
        CheckConstraint("octet_length(token_hash) = 32", name="token_hash_len"),
        Index(
            "uq_access_links_person_id_meeting_id_active",
            "person_id",
            "meeting_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_access_links_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = created_at_column()
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
