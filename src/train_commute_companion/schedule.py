"""Effective first train and dated windows (design 7.12 rule 19, 7.13)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta

from train_commute_companion.calendar import is_commute_day, resolve_local
from train_commute_companion.model import (
    Config,
    DatedSchedule,
    Direction,
    OccurrenceKey,
    Profile,
    UsualJourney,
)

_LOCK_LEAD_MIN = 15


def effective_first_train(j: UsualJourney, baseline_first_train: time | None) -> time:
    return j.first_train if baseline_first_train is None else baseline_first_train


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _local(naive: datetime, edge: str) -> datetime:
    return resolve_local(naive.date(), naive.time(), edge="end" if edge == "end" else "start")


def dated_schedule(
    p: Profile, d: Direction, commute_day: date, baseline_first_train: time | None
) -> DatedSchedule:
    j = p.journey(d)
    service_date = commute_day + timedelta(days=j.day_offset)
    eff = effective_first_train(j, baseline_first_train)
    # clock differences, so an adoption moves the window by the same amount
    before = timedelta(minutes=(_minutes(j.first_train) - _minutes(j.window[0])) % 1440)
    after = timedelta(minutes=(_minutes(j.window[1]) - _minutes(j.first_train)) % 1440)
    first = datetime.combine(service_date, eff)
    return DatedSchedule(
        OccurrenceKey(p.id, d, service_date),
        _local(first, "start"),
        _local(first - before, "start"),
        _local(first + after, "end"),
        _local(first - timedelta(minutes=j.buffer_min + _LOCK_LEAD_MIN), "start"),
    )


def schedules_active(
    cfg: Config, baselines: Mapping[tuple[str, Direction], time], now: datetime
) -> list[DatedSchedule]:
    out: list[DatedSchedule] = []
    today = now.date()
    for p in cfg.profiles:
        for d in Direction:
            # a window can open the evening before and an offset return runs a day late
            for back in (-1, 0, 1, 2):
                day = today - timedelta(days=back)
                if is_commute_day(day, p.days, p.extra_dates, p.skip_public_holidays):
                    s = dated_schedule(p, d, day, baselines.get((p.id, d)))
                    if s.window_start <= now <= s.window_end:
                        out.append(s)
    return out
