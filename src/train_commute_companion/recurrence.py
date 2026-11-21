"""Find commute dates for weekday and monthly schedules."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from math import lcm

MAX_RECURRENCE_DATE = date(9998, 12, 31)
MAX_TRIPS = 366


@dataclass(frozen=True, slots=True)
class MonthlyWeekday:
    ordinal: int
    weekday: int

    def matches(self, day: date) -> bool:
        return day.isoweekday() == self.weekday and (day.day - 1) // 7 + 1 == self.ordinal

    def dates(self, start: date, end: date) -> Iterator[date]:
        month = start.replace(day=1)
        while month <= end:
            offset = (self.weekday - month.isoweekday()) % 7 + 7 * (self.ordinal - 1)
            day = month + timedelta(days=offset)
            if day.month == month.month and start <= day <= end:
                yield day
            if month.month == 12:
                month = date(month.year + 1, 1, 1)
            else:
                month = month.replace(month=month.month + 1)


@dataclass(frozen=True, slots=True)
class RepeatSchedule:
    start: date
    end: date | None = None
    interval: int = 1
    monthly: MonthlyWeekday | None = None
    end_after: int | None = None

    def matches(self, day: date, weekdays: frozenset[int]) -> bool:
        if day < self.start or (self.end is not None and day > self.end):
            return False
        if self.monthly is not None:
            return self.monthly.matches(day)
        week = ((day - self.start).days + self.start.weekday()) // 7
        return day.isoweekday() in weekdays and week % self.interval == 0


def recurring_dates(
    first: date,
    last: date,
    anchor: date,
    weekdays: frozenset[int],
    interval: int,
    monthly: MonthlyWeekday | None,
) -> Iterator[date]:
    first = max(first, anchor)
    last = min(last, MAX_RECURRENCE_DATE)
    if last < first:
        return
    if monthly:
        yield from monthly.dates(first, last)
    elif not weekdays:
        if first == anchor:
            yield anchor
    else:
        monday = anchor.toordinal() - anchor.weekday()
        period = 7 * interval
        start, end = first.toordinal(), last.toordinal()
        week = monday + (start - monday) // period * period
        offsets = [day - 1 for day in sorted(weekdays)]
        for monday_at in range(week, end + 1, period):
            for offset in offsets:
                ordinal = monday_at + offset
                if start <= ordinal <= end:
                    yield date.fromordinal(ordinal)


# Amsterdam has used the same Gregorian clock-change rules since 1996. Keep historical dates
# separate and allow for windows on adjacent days across the boundary.
_MODERN = date(2000, 1, 5)
_GREGORIAN_WEEKS = 20871


def recurrence_phases(
    first: date, last: date, intervals: tuple[int, int], changes: Iterable[date], *, margin: int
) -> Iterator[tuple[date, date, date, int]]:
    """Yield (start, sample end, actual end, step) for each stable span.

    Calendar dates, weekdays, and local window geometry repeat after 400 years. Weekly
    intervals may extend that period. Bounds, extra dates and baseline changes split spans.
    Callers still check holidays and pauses on each projected date.
    """
    if first > last:
        return
    period = 7 * lcm(_GREGORIAN_WEEKS, *intervals)
    limit = last + timedelta(days=1)
    cuts = {first, limit}
    if first < _MODERN < limit:
        cuts.add(_MODERN)
    cuts.update(
        date.fromordinal(cut)
        for change in changes
        for offset in range(-margin, margin + 1)
        if first.toordinal() < (cut := change.toordinal() + offset) < limit.toordinal()
    )
    for start, stop in pairwise(sorted(cuts)):
        end = stop - timedelta(days=1)
        step = period if start >= _MODERN else (stop - start).days
        sample = date.fromordinal(min(end.toordinal(), start.toordinal() + step - 1))
        yield start, sample, end, step


def phase_copies(day: date, end: date, step: int) -> Iterator[date]:
    return map(date.fromordinal, range(day.toordinal(), end.toordinal() + 1, step))


def occurrence_spacing(
    weekdays: frozenset[int], interval: int, monthly: MonthlyWeekday | None
) -> float:
    return 30 if monthly else 7 * interval / len(weekdays) if weekdays else float("inf")
