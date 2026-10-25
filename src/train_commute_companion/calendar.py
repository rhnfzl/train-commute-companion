"""Commute days, Dutch public holidays and DST-safe clock resolution."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def nl_public_holidays(year: int) -> frozenset[date]:
    easter = _easter(year)
    kings = date(year, 4, 27)
    if kings.isoweekday() == 7:
        kings = date(year, 4, 26)
    return frozenset(
        {
            date(year, 1, 1),
            easter + timedelta(days=1),
            kings,
            easter + timedelta(days=39),
            easter + timedelta(days=50),
            date(year, 12, 25),
            date(year, 12, 26),
        }
    )


def is_commute_day(
    day: date, days: frozenset[int], extra_dates: frozenset[date], skip_public_holidays: bool
) -> bool:
    if day in extra_dates:
        return True
    if skip_public_holidays and day in nl_public_holidays(day.year):
        return False
    return day.isoweekday() in days


def _exists(naive: datetime) -> bool:
    local = naive.replace(tzinfo=TZ)
    return local.astimezone(UTC).astimezone(TZ).replace(tzinfo=None) == naive


def resolve_local(day: date, clock: time, *, edge: Literal["start", "end"]) -> datetime:
    naive = datetime.combine(day, clock.replace(tzinfo=None))
    while not _exists(naive):  # spring gap: first wall time after it is the first instant after
        naive += timedelta(minutes=1)
    fold = 1 if edge == "end" else 0  # fold only matters in the repeated autumn hour
    return naive.replace(tzinfo=TZ, fold=fold)
