from datetime import date

import pytest

pytestmark = pytest.mark.fr("FR-42")


def add(start: date, n: int) -> date:
    from app.core.business_days import add_business_days

    return add_business_days(start, n)


@pytest.mark.parametrize(
    ("start", "n", "expected"),
    [
        (date(2026, 10, 14), 1, date(2026, 10, 15)),
        (date(2026, 10, 16), 1, date(2026, 10, 19)),
        (date(2026, 10, 12), 2, date(2026, 10, 14)),
        (date(2026, 10, 15), 2, date(2026, 10, 19)),
        (date(2026, 10, 16), 0, date(2026, 10, 16)),
        (date(2026, 10, 17), 1, date(2026, 10, 19)),
        (date(2026, 10, 18), 1, date(2026, 10, 19)),
        (date(2026, 10, 14), 5, date(2026, 10, 21)),
        (date(2026, 10, 19), -1, date(2026, 10, 16)),
    ],
    ids=str,
)
def test_add_business_days(start: date, n: int, expected: date) -> None:
    assert add(start, n) == expected
