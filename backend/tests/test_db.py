import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.fr("infra")


def test_select_one(db: Session) -> None:
    assert db.execute(text("SELECT 1")).scalar_one() == 1
