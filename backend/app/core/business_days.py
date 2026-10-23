from datetime import date, timedelta


def add_business_days(start: date, n: int) -> date:
    """Сдвигает дату на n рабочих дней (пн-пт), при n < 0 - назад (BR-7).

    Праздники производственного календаря пока не учитываются.
    """
    step = 1 if n >= 0 else -1
    day = start
    for _ in range(abs(n)):
        day += timedelta(days=step)
        while day.weekday() >= 5:
            day += timedelta(days=step)
    return day
