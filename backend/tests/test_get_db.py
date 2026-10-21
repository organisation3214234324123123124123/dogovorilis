from collections.abc import Iterator
from typing import Any

import pytest

from app.core import db as db_module

pytestmark = pytest.mark.fr("infra")


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def commit(self) -> None:
        self.calls.append("commit")

    def rollback(self) -> None:
        self.calls.append("rollback")

    def close(self) -> None:
        self.calls.append("close")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    session = FakeSession()
    monkeypatch.setattr(db_module, "SessionLocal", lambda *a, **k: session)
    monkeypatch.setattr(db_module, "get_engine", lambda: None)
    return session


def _drain(gen: Iterator[Any]) -> None:
    for _ in gen:
        pass


def test_commits_on_normal_exit(fake: FakeSession) -> None:
    _drain(db_module.get_db())
    assert "commit" in fake.calls
    assert "rollback" not in fake.calls


def test_closes_on_normal_exit(fake: FakeSession) -> None:
    _drain(db_module.get_db())
    assert fake.calls[-1] == "close"


def test_rolls_back_and_reraises_on_exception(fake: FakeSession) -> None:
    gen = db_module.get_db()
    next(gen)
    with pytest.raises(RuntimeError, match="boom"):
        gen.throw(RuntimeError("boom"))
    assert "rollback" in fake.calls
    assert "commit" not in fake.calls


def test_closes_on_exception(fake: FakeSession) -> None:
    gen = db_module.get_db()
    next(gen)
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("boom"))
    assert "close" in fake.calls
