"""Effective first train and dated windows (design 7.12 rule 19, 7.13)."""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta

from train_commute_companion.calendar import (
    MINUTES_PER_DAY,
    TZ,
    is_commute_day,
    minute_of_day,
    resolve_local,
)
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


def _local(naive: datetime, edge: str) -> datetime:
    return resolve_local(naive.date(), naive.time(), edge="end" if edge == "end" else "start")


def dated_schedule(
    p: Profile, d: Direction, commute_day: date, baseline_first_train: time | None
) -> DatedSchedule:
    j = p.journey(d)
    service_date = commute_day + timedelta(days=j.day_offset)
    eff = effective_first_train(j, baseline_first_train)
    # clock differences, so an adoption moves the window by the same amount
    before = timedelta(
        minutes=(minute_of_day(j.first_train) - minute_of_day(j.window[0])) % MINUTES_PER_DAY
    )
    after = timedelta(
        minutes=(minute_of_day(j.window[1]) - minute_of_day(j.first_train)) % MINUTES_PER_DAY
    )
    first = datetime.combine(service_date, eff)
    return DatedSchedule(
        OccurrenceKey(p.id, d, service_date),
        _local(first, "start"),
        _local(first - before, "start"),
        _local(first + after, "end"),
        _local(first - timedelta(minutes=j.buffer_min + _LOCK_LEAD_MIN), "start"),
    )


def in_window(s: DatedSchedule, at: datetime) -> bool:
    """Half-open [start, end), the same policy the overlap check uses."""
    return s.window_start.astimezone(UTC) <= at < s.window_end.astimezone(UTC)


def schedules_active(
    cfg: Config,
    baselines: Mapping[tuple[str, Direction], time],
    now: datetime,
    first: Callable[[Profile, Direction, date], time | None] | None = None,
) -> list[DatedSchedule]:
    """`first(profile, direction, commute_day)`, when given, is the baseline first train in
    force on that occurrence's own service date; else `baselines` holds one for every date."""
    out: list[DatedSchedule] = []
    at = now.astimezone(UTC)  # same-zone comparison ignores fold; instants do not
    today = now.astimezone(TZ).date()
    for p in cfg.profiles:
        for d in Direction:
            # a window can open the evening before and an offset return runs a day late
            for back in (-1, 0, 1, 2):
                day = today - timedelta(days=back)
                if is_commute_day(day, p.days, p.extra_dates, p.skip_public_holidays):
                    base = first(p, d, day) if first else baselines.get((p.id, d))
                    s = dated_schedule(p, d, day, base)
                    if in_window(s, at):
                        out.append(s)
    return out
