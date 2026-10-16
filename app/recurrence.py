"""Expand recurring items into dated occurrences."""
import calendar
from datetime import date, timedelta

RECURRENCES = ("once", "weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly")


def _add_months(y: int, m: int, n: int, day: int) -> date:
    m = m - 1 + n
    y, m = y + m // 12, m % 12 + 1
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def occurrences(start: date, recurrence: str, lo: date, hi: date, end: date | None = None):
    """Yield occurrence dates within [lo, hi] inclusive."""
    if end and end < hi:
        hi = end
    if hi < start or hi < lo:
        return
    if recurrence == "once":
        if lo <= start <= hi:
            yield start
        return
    if recurrence in ("weekly", "biweekly"):
        step = 7 if recurrence == "weekly" else 14
        d = start
        if d < lo:
            d += timedelta(days=((lo - d).days + step - 1) // step * step)
        while d <= hi:
            yield d
            d += timedelta(days=step)
        return
    if recurrence == "semimonthly":  # start day and 15 days later each month (e.g. 1st & 16th)
        for base in occurrences(start, "monthly", lo - timedelta(days=31), hi, None):
            for d in (base, base + timedelta(days=15)):
                if lo <= d <= hi and d >= start:
                    yield d
        return
    step = {"monthly": 1, "quarterly": 3, "yearly": 12}[recurrence]
    i = 0
    if lo > start:  # jump ahead instead of iterating every period
        i = max(0, ((lo.year - start.year) * 12 + lo.month - start.month) // step - 1)
    while True:
        d = _add_months(start.year, start.month, i * step, start.day)
        if d > hi:
            return
        if d >= lo:
            yield d
        i += 1


def week_bounds(d: date, week_start: int):
    s = d - timedelta(days=(d.weekday() - week_start) % 7)
    return s, s + timedelta(days=6)
