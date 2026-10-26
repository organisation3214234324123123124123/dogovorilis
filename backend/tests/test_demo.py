import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.demo import loader
from app.demo.loader import main
from app.models import AccessLink, Item, Person, User
from app.modules.items.models import OPEN_STATUSES

pytestmark = pytest.mark.fr("infra")


@pytest.fixture
def demo_db(db: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    @contextmanager
    def shared_session(**_: object) -> Iterator[Session]:
        yield db

    monkeypatch.setattr(loader, "SessionLocal", shared_session)
    return db


@pytest.fixture
def demo_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loader, "DEMO_DIR", tmp_path)
    return tmp_path


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model))


def overdue_count(db: Session) -> int:
    stmt = (
        select(func.count())
        .select_from(Item)
        .where(Item.status.in_(OPEN_STATUSES), Item.due_date < date.today())
    )
    return db.scalar(stmt)


def test_load_main_counts(demo_db: Session) -> None:
    loader.clear(demo_db)
    assert main(["load"]) == 0
    assert count(demo_db, User) == 1
    assert count(demo_db, Item) == 10
    assert overdue_count(demo_db) == 2


def test_reset_is_idempotent(demo_db: Session) -> None:
    assert main(["reset"]) == 0
    first = (count(demo_db, User), count(demo_db, Person), count(demo_db, Item))
    assert main(["reset"]) == 0
    assert (count(demo_db, User), count(demo_db, Person), count(demo_db, Item)) == first
    assert first[2] == 10


def test_overdue_on_top_of_main_reuses_persons_by_email(demo_db: Session) -> None:
    assert main(["reset"]) == 0
    users, persons, items = count(demo_db, User), count(demo_db, Person), count(demo_db, Item)
    assert main(["load", "--set", "overdue"]) == 0
    assert count(demo_db, User) == users
    assert count(demo_db, Person) == persons
    assert count(demo_db, Item) == items + 5
    assert count(demo_db, AccessLink) >= 2


def test_if_empty_skips_when_users_exist(
    demo_db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["reset"]) == 0
    items = count(demo_db, Item)
    assert main(["load", "--set", "overdue", "--if-empty"]) == 0
    assert count(demo_db, Item) == items
    assert "skipped" in capsys.readouterr().out


def test_if_empty_loads_into_empty_database(demo_db: Session) -> None:
    assert main(["clear"]) == 0
    assert main(["load", "--if-empty"]) == 0
    assert count(demo_db, Item) == 10


def test_clear_empties_database_but_keeps_prompts(demo_db: Session) -> None:
    from app.models import LlmPrompt

    prompts = count(demo_db, LlmPrompt)
    assert main(["reset"]) == 0
    assert main(["clear"]) == 0
    assert count(demo_db, User) == 0
    assert count(demo_db, LlmPrompt) == prompts


def test_load_empty_set(demo_dir, demo_db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    (demo_dir / "main.yaml").write_text("", encoding="utf-8")
    assert main(["load"]) == 0
    assert "nothing to load" in capsys.readouterr().out


def test_load_unknown_set_exits_2() -> None:
    assert main(["load", "--set", "nope"]) == 2


def test_unknown_entries_warn_and_exit_0(
    demo_dir, demo_db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    (demo_dir / "main.yaml").write_text("martians:\n  - name: x\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert main(["load"]) == 0
    assert any(
        r.levelno == logging.WARNING and "martians" in r.getMessage() for r in caplog.records
    )


def test_missing_set_file_exits_2(demo_dir) -> None:
    assert main(["load", "--set", "absent"]) == 2


def test_bad_args_exit_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 2


def test_is_empty_reflects_users(db: Session) -> None:
    loader.clear(db)
    assert loader.is_empty(db)
    db.add(User(email="a@example.com", password_hash="x", full_name="A"))
    db.flush()
    assert not loader.is_empty(db)
