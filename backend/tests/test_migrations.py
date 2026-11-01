import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.db import get_engine
from app.models import Base

pytestmark = pytest.mark.fr("infra")

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg


def reset_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture
def scratch_url(db, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    base = make_url(os.environ["DATABASE_URL"])
    name = f"leaderhelp_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    reset_caches()
    try:
        yield url
    finally:
        reset_caches()
        monkeypatch.undo()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        reset_caches()


def public_tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def test_single_head() -> None:
    assert len(ScriptDirectory.from_config(alembic_config()).get_heads()) == 1


def test_upgrade_downgrade_upgrade(scratch_url: str) -> None:
    cfg = alembic_config()
    command.upgrade(cfg, "head")
    assert {t.name for t in Base.metadata.sorted_tables} <= public_tables(scratch_url)
    reset_caches()
    command.downgrade(cfg, "base")
    assert public_tables(scratch_url) == set()
    reset_caches()
    command.upgrade(cfg, "head")
    assert {t.name for t in Base.metadata.sorted_tables} <= public_tables(scratch_url)


def test_models_match_migrations(scratch_url: str) -> None:
    command.upgrade(alembic_config(), "head")
    engine = create_engine(scratch_url)
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()
    assert diff == []


def meetings_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("meetings")}
    finally:
        engine.dispose()


@pytest.mark.fr("FR-14")
def test_fr14_downgrade_drops_journal_and_upgrade_restores_it(scratch_url: str) -> None:
    cfg = alembic_config()
    command.upgrade(cfg, "0011")
    reset_caches()
    command.downgrade(cfg, "0010")
    assert "meeting_events" not in public_tables(scratch_url)
    assert "events_version" not in meetings_columns(scratch_url)
    reset_caches()
    command.upgrade(cfg, "head")
    assert "meeting_events" in public_tables(scratch_url)
    assert "events_version" in meetings_columns(scratch_url)


def _parse_prompts(url: str) -> list[tuple[int, bool]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT version, is_active FROM llm_prompts WHERE task = 'parse' ORDER BY 1")
            )
            return [(r[0], r[1]) for r in rows]
    finally:
        engine.dispose()


def _use_active_parse_prompt(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_calls (id, task, provider, model, prompt_id, outcome) "
                    "SELECT gen_random_uuid(), task, 'fake', 'fake-1', id, 'ok' "
                    "FROM llm_prompts WHERE task = 'parse' AND is_active"
                )
            )
    finally:
        engine.dispose()


@pytest.mark.fr("FR-12")
def test_parse_prompt_downgrade_with_calls_keeps_one_active(scratch_url: str) -> None:
    cfg = alembic_config()
    command.upgrade(cfg, "head")
    _use_active_parse_prompt(scratch_url)
    reset_caches()
    command.downgrade(cfg, "0012")
    active = [v for v, on in _parse_prompts(scratch_url) if on]
    assert active == [2]
    reset_caches()
    command.upgrade(cfg, "head")
    active = [v for v, on in _parse_prompts(scratch_url) if on]
    assert active == [3]
