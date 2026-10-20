from collections.abc import Iterator
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, ClassVar

from fastapi import Depends
from sqlalchemy import DateTime, Engine, Enum, MetaData, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import get_settings
from app.core.errors import AppError

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar = {datetime: DateTime(timezone=True)}


def pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Нативный enum PostgreSQL, в котором хранятся значения StrEnum, а не имена."""
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


def created_at_column() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal(bind=get_engine()) as db:
        try:
            yield db
            db.commit()
        except AppError as exc:
            if exc.keep_changes:
                db.commit()
            else:
                db.rollback()
            raise
        except Exception:
            db.rollback()
            raise


# scope="function": commit до отправки ответа, иначе клиент получит 201 до фиксации
SessionDep = Annotated[Session, Depends(get_db, scope="function")]

# Потоковый ответ читает БД уже при отправке: сессия живёт до конца отправки (scope="request"),
# commit после ответа допустим только для чтения (экспорт)
StreamSessionDep = Annotated[Session, Depends(get_db, scope="request")]
