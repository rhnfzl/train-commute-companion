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

ACTIVE_EXTRA_MIN = 15


def active_lead(j: UsualJourney) -> int:
    """Minutes before the first departure at which the quiet early watch ends (Q9):
    `active_lead_min`, else buffer + 15, the old auto-lock lead."""
    return j.buffer_min + ACTIVE_EXTRA_MIN if j.active_lead_min is None else j.active_lead_min


def effective_first_train(j: UsualJourney, baseline_first_train: time | None) -> time:
    return j.first_train if baseline_first_train is None else baseline_first_train


def _local(naive: datetime, edge: str) -> datetime:
    return resolve_local(naive.date(), naive.time(), edge="end" if edge == "end" else "start")


def dated_schedule(
    p: Profile,
    d: Direction,
    commute_day: date,
    baseline_first_train: time | None,
    stay_from: datetime | None = None,
) -> DatedSchedule:
    """`stay_from`: the arrival a window day's minimum stay counts from; fixed days ignore it."""
    j = p.journey_on(d, commute_day)
    service_date = commute_day + timedelta(days=j.day_offset)
    if j.mode == "window":
        return _window_schedule(p, d, j, service_date, stay_from)
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
    )


def _window_schedule(
    p: Profile, d: Direction, j: UsualJourney, service_date: date, stay_from: datetime | None
) -> DatedSchedule:
    """Window mode: any departure from window[0] to window[1], both inclusive. The minimum stay
    moves the start to arrival + stay, but never past the latest time. A stay that ends after
    the latest time makes that day's misses silent: the user may still be inside the stay when
    the train TCC follows leaves. No usual train, so a baseline is never adopted."""
    start = _local(datetime.combine(service_date, j.window[0]), "start")
    end_day = service_date + timedelta(days=1 if j.window[1] < j.window[0] else 0)
    end = _local(datetime.combine(end_day, j.window[1]), "end")
    pick, capped = j.pick, False
    if j.min_stay_min and stay_from is not None:
        stayed = stay_from.astimezone(UTC) + timedelta(minutes=j.min_stay_min)
        capped = stayed > end.astimezone(UTC)
        if capped and pick == "earliest":
            pick = "earliest_silent"
        start = min(max(start.astimezone(UTC), stayed), end.astimezone(UTC)).astimezone(TZ)
    return DatedSchedule(
        OccurrenceKey(p.id, d, service_date),
        start,
        (start.astimezone(UTC) - timedelta(minutes=active_lead(j))).astimezone(TZ),
        (end.astimezone(UTC) + timedelta(minutes=1)).astimezone(TZ),  # half-open: end included
        "window",
        pick,
        end,
        capped,
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
                    if not p.paused_on(s.key.service_date) and in_window(s, at):
                        out.append(s)
    return out
