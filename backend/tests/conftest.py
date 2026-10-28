import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Переменные окружения по умолчанию задаются до импорта приложения.
os.environ.pop("APP_VERSION", None)
os.environ["DEV_MODE"] = "1"  # тесты идут в режиме разработки; боевой режим включают сами тесты
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://leaderhelp:leaderhelp@localhost:5432/leaderhelp_test"
)
os.environ["CELERY_EAGER"] = "1"
os.environ["LIVE_BUS"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import create_app


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "fr(id): functional requirement the test covers")


BACKEND_DIR = Path(__file__).resolve().parents[1]
_db_error: list[str] = []


def _skip_or_raise(exc: Exception) -> None:
    if os.environ.get("ALLOW_NO_DB") == "1":
        pytest.skip(f"database unreachable (ALLOW_NO_DB=1): {exc}")
    raise exc


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    """Применяет миграции Alembic к тестовой БД один раз за сессию."""
    get_settings.cache_clear()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        if os.environ.get("ALLOW_NO_DB") != "1":
            raise
        _db_error.append(str(exc))
    finally:
        get_settings.cache_clear()


@pytest.fixture
def settings_reset() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# Изоляция: сессия привязана к соединению, внешняя транзакция которого не коммитится.
# При join_transaction_mode="create_savepoint" commit()/rollback() приложения трогают
# только SAVEPOINT, и в конце теста всё откатывается. `client` подменяет get_db этой же
# сессией, поэтому вызовы API и прямая работа с `db` видят одно состояние.
@pytest.fixture
def db(settings_reset: None, migrated_db: None) -> Iterator[Session]:
    from sqlalchemy import text

    from app.core.db import get_engine

    if _db_error:
        pytest.skip(f"database unreachable (ALLOW_NO_DB=1): {_db_error[0]}")
    try:
        connection = get_engine().connect()
        outer = connection.begin()
        connection.execute(text("SELECT 1"))
    except Exception as exc:
        _skip_or_raise(exc)
        return
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


@pytest.fixture
def client(settings_reset: None, request: pytest.FixtureRequest) -> Iterator[TestClient]:
    from app.core.db import get_db

    app = create_app()
    if not _db_error:
        try:
            session = request.getfixturevalue("db")
        except pytest.skip.Exception:
            session = None
        if session is not None:

            def override_get_db() -> Iterator[Session]:
                from app.core.errors import AppError

                session.commit()  # данные теста переживут откат запроса
                try:
                    yield session
                    session.commit()
                except AppError as exc:
                    if exc.keep_changes:
                        session.commit()
                    else:
                        session.rollback()
                    raise
                except Exception:
                    session.rollback()
                    raise

            app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c


PASSWORD = "secret123"


@pytest.fixture
def world(db: Session):
    from app.core.security import hash_password
    from tests.factories import World

    w = World(db)
    w.user.password_hash = hash_password(PASSWORD)
    db.flush()
    return w


def login_as(client: TestClient, user) -> None:
    from app.core.security import SESSION_COOKIE, create_access_token

    client.cookies.set(SESSION_COOKIE, create_access_token(user.id))


@pytest.fixture
def authed(client: TestClient, world) -> TestClient:
    login_as(client, world.user)
    return client
