"""Local journey rows and the statistics computed from them."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Literal

from train_commute_companion.calendar import to_utc
from train_commute_companion.catalogue import MessageLanguage, message, message_weekday
from train_commute_companion.compensation_state import JourneyRecord
from train_commute_companion.model import EventKind, OccurrenceKey

HISTORY_DAYS = 400
ON_TIME_MINUTES = 3
STATS_PERIODS = {"week": 7, "month": 30, "year": 365}
HistoryFlag = Literal["cancelled", "moved", "missed"]
HISTORY_ALERTS = frozenset({EventKind.WEEKLY_RECAP, EventKind.AFTER_TRIP_NOTE})


@dataclass(frozen=True, slots=True)
class JourneyHistory:
    key: OccurrenceKey
    planned_departure: datetime | None
    planned_arrival: datetime | None
    ns_arrival: datetime | None
    cancelled: bool
    moved: bool
    missed: bool

    @property
    def delay_seconds(self) -> int | None:
        if self.ns_arrival is None or self.planned_arrival is None:
            return None
        return max(0, int((to_utc(self.ns_arrival) - to_utc(self.planned_arrival)).total_seconds()))


def history_row(
    record: JourneyRecord, flags: frozenset[HistoryFlag], ns_arrival: datetime | None
) -> JourneyHistory | None:
    if ns_arrival is None and not flags:
        return None
    plan = record.plan if record.plan is not None and record.plan.source == "ns" else None
    return JourneyHistory(
        record.key,
        plan.planned_departure if plan else None,
        plan.planned_arrival if plan else None,
        ns_arrival,
        "cancelled" in flags,
        "moved" in flags,
        "missed" in flags,
    )


@dataclass(frozen=True, slots=True)
class CommuteStats:
    journeys: int
    arrivals: int
    on_time: int
    average_delay_min: float | None
    worst_weekday: int | None
    cancelled: int
    moved: int
    missed: int

    @property
    def on_time_percent(self) -> float | None:
        return round(100 * self.on_time / self.arrivals, 1) if self.arrivals else None


def commute_stats(rows: Iterable[JourneyHistory], start: date, end: date) -> CommuteStats:
    counted = [row for row in rows if start <= row.key.service_date <= end]
    weekdays: dict[int, list[int]] = {}
    delays = []
    for row in counted:
        if (delay := row.delay_seconds) is not None:
            delays.append(delay)
            weekdays.setdefault(row.key.service_date.isoweekday(), []).append(delay)
    worst = max(weekdays, key=lambda day: (mean(weekdays[day]), -day), default=None)
    return CommuteStats(
        len(counted),
        len(delays),
        sum(delay < ON_TIME_MINUTES * 60 for delay in delays),
        round(mean(delays) / 60, 1) if delays else None,
        worst if worst is not None and any(delays) else None,
        sum(row.cancelled for row in counted),
        sum(row.moved for row in counted),
        sum(row.missed for row in counted),
    )


def stats_text(stats: CommuteStats, language: MessageLanguage = "en") -> str:
    if stats.journeys == 0:
        return message("stats.empty", language)
    arrival = (
        message(
            "stats.arrivals",
            language,
            on_time=stats.on_time,
            arrivals=stats.arrivals,
            percent=f"{stats.on_time_percent:g}",
            delay=f"{stats.average_delay_min:g}",
        )
        if stats.on_time_percent is not None and stats.average_delay_min is not None
        else message("stats.unavailable", language)
    )
    worst = (
        message("stats.worst", language, day=message_weekday(stats.worst_weekday - 1, language))
        if stats.worst_weekday is not None
        else ""
    )
    return message(
        "stats.journeys",
        language,
        count=stats.journeys,
        arrivals=arrival,
        worst=worst,
        cancelled=stats.cancelled,
        moved=stats.moved,
        missed=stats.missed,
    )


def retained_history(rows: Iterable[JourneyHistory], today: date) -> Iterable[JourneyHistory]:
    first = today - timedelta(days=HISTORY_DAYS - 1)
    return (row for row in rows if row.key.service_date >= first)
