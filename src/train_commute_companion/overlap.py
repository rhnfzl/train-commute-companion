"""Whether profiles collide: shared days and overlapping time windows. Knows nothing of the file."""

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from heapq import merge

from train_commute_companion.calendar import TZ, WEEKDAYS
from train_commute_companion.model import DatedSchedule, Direction, Profile, ProfileProblem
from train_commute_companion.recurrence import (
    MAX_RECURRENCE_DATE,
    occurrence_spacing,
    phase_copies,
    recurrence_phases,
    recurring_dates,
)
from train_commute_companion.schedule import dated_schedule

# a year and two weeks from two days back: every weekday pair meets both DST nights
_SCAN_DAYS = 380
# These weeks cover the normal week and both sides of each Amsterdam clock change.
_WINDOW_WEEKS = (
    date(2026, 1, 5),
    date(2026, 3, 23),
    date(2026, 3, 30),
    date(2026, 10, 19),
    date(2026, 10, 26),
)

FirstTrain = Callable[[Profile, Direction, date], time | None]
FirstOptions = Mapping[tuple[str, Direction], Collection[time]]


@dataclass(frozen=True, slots=True)
class _Occ:
    day: date
    direction: Direction
    recurring: bool  # on one of the profile's weekdays, holidays ignored
    real: bool  # an actual commute day
    start: datetime  # UTC
    end: datetime  # UTC


def _occurrences(
    p: Profile, today: date, near: frozenset[date], first_train: FirstTrain | None
) -> dict[date, list[_Occ]]:
    first = today - timedelta(days=2)  # a commute day two days back can still have an open window
    span = {first + timedelta(days=i) for i in range(_SCAN_DAYS)}
    out: dict[date, list[_Occ]] = {}
    for day in sorted(span | {d for d in near if d >= first}):
        if p.repeats_on(day) or p.commutes_on(day):
            out[day] = list(_windows(p, day, first_train))
    return out


def _collision(x: _Occ, y: _Occ, floor: datetime, same: bool) -> datetime | None:
    # a weekly pattern collides in every non-holiday week, so holidays only count when a one-off
    # date is involved
    if not ((x.real and y.real) or (x.recurring and y.recurring)):
        return None
    times = _intersect(x, y, same)
    return times[0] if times is not None and times[1] > floor else None


def _intersect(x: _Occ, y: _Occ, same: bool) -> tuple[datetime, datetime] | None:
    if same and (x.day, x.direction) == (y.day, y.direction):
        return None
    lo, hi = max(x.start, y.start), min(x.end, y.end)
    return (lo, hi) if lo < hi else None


def _window_overlap(
    oa: dict[date, list[_Occ]], ob: dict[date, list[_Occ]], floor: datetime, same: bool
) -> date | None:
    """Earliest local date two windows overlap, half-open [start, end) like in_window."""
    hits = [
        lo
        for day, xs in oa.items()
        for k in range(-3, 4)  # a dated window lies within its commute day -1 .. +2
        for y in ob.get(day + timedelta(days=k), ())
        for x in xs
        if (lo := _collision(x, y, floor, same)) is not None
    ]
    return min(hits).astimezone(TZ).date() if hits else None


def _has_unpaused_journey(p: Profile, day: date) -> bool:
    return any(not p.paused_on(day + timedelta(days=p.journey(d).day_offset)) for d in Direction)


def profile_dates(p: Profile, start: date, stop: date) -> Iterator[date]:
    rule = p.repeat
    days: Iterator[date] = iter(())
    if p.days:
        days = recurring_dates(
            start,
            min(stop, rule.end or stop) if rule else stop,
            rule.start if rule else start,
            p.days,
            rule.interval if rule else 1,
            rule.monthly if rule else None,
        )
    yield from merge(days, sorted(d for d in p.extra_dates if start <= d <= stop))


def possible_offsets(
    a: Profile,
    b: Profile,
    same: bool,
    first_options: FirstOptions,
    *,
    radius: int = 3,
    modern: bool = False,
) -> dict[int, tuple[int, ...]]:
    weeks = _WINDOW_WEEKS if modern else _WINDOW_WEEKS[:1]
    margin = timedelta(hours=0 if modern else 3)
    a_days = a.days | {day.isoweekday() for day in a.extra_dates}
    b_days = b.days | {day.isoweekday() for day in b.extra_dates}

    def windows(p: Profile, day: date) -> tuple[DatedSchedule, ...]:
        return tuple(
            dated_schedule(p, d, day, clock)
            for d in Direction
            for clock in (None, *first_options.get((p.id, d), ()))
        )

    offsets = {}
    for weekday in a_days:
        days = [monday + timedelta(days=weekday - 1) for monday in weeks]
        samples = [(day, windows(a, day)) for day in days]
        close = []
        for offset in range(-radius, radius + 1):
            if (weekday - 1 + offset) % 7 + 1 not in b_days:
                continue
            if not same and offset == 0:
                close.append(offset)
                continue
            if any(
                max(left.window_start.astimezone(UTC), right.window_start.astimezone(UTC))
                < min(left.window_end.astimezone(UTC), right.window_end.astimezone(UTC)) + margin
                for day, left_windows in samples
                for right in windows(b, day + timedelta(days=offset))
                for left in left_windows
                if not (same and offset == 0 and left.key.direction == right.key.direction)
            ):
                close.append(offset)
        offsets[weekday] = tuple(close)
    return offsets


def profile_changes(p: Profile) -> Iterator[date]:
    if p.repeat is not None:
        yield p.repeat.start
        if p.repeat.end is not None:
            yield p.repeat.end + timedelta(days=1)
    for day in p.extra_dates:
        yield day
        yield day + timedelta(days=1)


def profile_spacing(p: Profile) -> float:
    return occurrence_spacing(
        p.days, p.repeat.interval if p.repeat else 1, p.repeat.monthly if p.repeat else None
    )


def _active(p: Profile, day: date, direction: Direction) -> bool:
    service = day + timedelta(days=p.journey(direction).day_offset)
    return p.commutes_on(day) and not p.paused_on(service)


def _dated_overlap(
    a: Profile,
    b: Profile,
    today: date,
    first: FirstTrain | None,
    options: FirstOptions | None,
    first_changes: Collection[date] | None,
) -> date | None:
    same = a.id == b.id
    if profile_spacing(b) > profile_spacing(a):
        a, b = b, a  # enumerate the sparser profile
    possible = (
        possible_offsets(a, b, same, options or {}, modern=today >= date(2000, 1, 5))
        if first is None or options is not None
        else None
    )
    if possible is not None and not any(possible.values()):
        return None
    start = today - timedelta(days=2)
    stop = MAX_RECURRENCE_DATE
    if b.repeat is not None:
        earliest = min((b.repeat.start, *b.extra_dates))
        start = max(start, earliest - timedelta(days=3))
        if b.repeat.end is not None:
            stop = min(stop, max((b.repeat.end, *b.extra_dates)) + timedelta(days=3))
    floor = datetime.combine(today, time(), TZ).astimezone(UTC)
    intervals = (a.repeat.interval if a.repeat else 1, b.repeat.interval if b.repeat else 1)
    phases = (
        recurrence_phases(
            start,
            stop,
            intervals,
            (*profile_changes(a), *profile_changes(b), *(first_changes or ())),
            margin=4,
        )
        if first is None or first_changes is not None
        else ((start, stop, stop, 1),)
    )
    best = None
    for lo, sample, end, step in phases:
        for day in profile_dates(a, lo, sample):
            if best is not None and day > best + timedelta(days=1):
                return best
            offsets = possible.get(day.isoweekday(), ()) if possible is not None else range(-3, 4)
            windows_a = None
            for offset in offsets:
                target = day + timedelta(days=offset)
                if target not in b.extra_dates and not b.repeats_on(target):
                    continue
                if not same and offset == 0:
                    for actual in phase_copies(day, end, step):
                        if best is not None and actual >= best:
                            break
                        if actual >= today and all(
                            p.commutes_on(actual) and _has_unpaused_journey(p, actual)
                            for p in (a, b)
                        ):
                            best = actual
                            break
                if windows_a is None:
                    windows_a = _windows(a, day, first)
                for y in _windows(b, target, first):
                    for x in windows_a:
                        times = _intersect(x, y, same)
                        if times is None:
                            continue
                        hit_day = times[0].astimezone(TZ).date()
                        for actual in phase_copies(day, end, step):
                            shift = actual - day
                            hit = hit_day + shift
                            if best is not None and hit >= best:
                                break
                            if (actual != day or times[1] > floor) and (
                                _active(a, actual, x.direction)
                                and _active(b, target + shift, y.direction)
                            ):
                                best = hit
                                break
    return best


def _windows(p: Profile, day: date, first: FirstTrain | None) -> tuple[_Occ, ...]:
    recurring, real = p.repeats_on(day), p.commutes_on(day)
    windows = []
    for d in Direction:
        s = dated_schedule(p, d, day, first(p, d, day) if first else None)
        windows.append(
            _Occ(
                day,
                d,
                recurring,
                real and not p.paused_on(s.key.service_date),
                s.window_start.astimezone(UTC),
                s.window_end.astimezone(UTC),
            )
        )
    return tuple(windows)


def overlap_pairs(
    profiles: Sequence[Profile],
    today: date,
    first: FirstTrain | None = None,
    *,
    first_options: FirstOptions | None = None,
    first_changes: Collection[date] | None = None,
) -> list[tuple[Profile, Profile, ProfileProblem]]:
    found: dict[tuple[str, str], tuple[Profile, Profile, ProfileProblem]] = {}

    def add(a: Profile, b: Profile, msg: str, hint: str) -> None:
        found.setdefault((a.id, b.id), (a, b, ProfileProblem(a.id, "profiles", msg, hint)))

    pairs = [(a, b) for i, a in enumerate(profiles) for b in profiles[i + 1 :]]
    # a shared weekday collides on every non-holiday week
    for a, b in pairs:
        shared = a.days & b.days
        if shared and a.repeat is None and b.repeat is None:
            msg = (
                f"profiles {a.name!r} and {b.name!r} are both active on {WEEKDAYS[min(shared) - 1]}"
            )
            add(a, b, msg, "give them different days")
    # a one-off date collides with any other profile active that day, however far ahead
    for a, b in pairs:
        hit = min(
            (
                d
                for x, y in ((a, b), (b, a))
                for d in x.extra_dates
                if d >= today
                and _has_unpaused_journey(x, d)
                and _has_unpaused_journey(y, d)
                and y.commutes_on(d)
            ),
            default=None,
        )
        if hit:
            msg = (
                f"profiles {a.name!r} and {b.name!r} are both active on "
                f"{WEEKDAYS[hit.weekday()]} {hit.isoformat()}"
            )
            add(a, b, msg, "give them different days or remove the extra date")
    # different commute days, or two directions of one profile, can still collide in real time
    near = frozenset(
        d + timedelta(days=k) for p in profiles for d in p.extra_dates for k in range(-3, 4)
    )
    occ = {p.id: _occurrences(p, today, near, first) for p in profiles if p.repeat is None}
    floor = datetime.combine(today, time(), TZ).astimezone(UTC)
    for p in profiles:
        hit = (
            _window_overlap(occ[p.id], occ[p.id], floor, True)
            if p.repeat is None
            else _dated_overlap(p, p, today, first, first_options, first_changes)
        )
        if hit:
            msg = f"profile {p.name!r} has overlapping time windows on {hit.isoformat()}"
            add(p, p, msg, "narrow the outbound or return window")
    for a, b in pairs:
        if (a.id, b.id) not in found:
            hit = (
                _window_overlap(occ[a.id], occ[b.id], floor, False)
                if a.repeat is None and b.repeat is None
                else _dated_overlap(a, b, today, first, first_options, first_changes)
            )
            if hit:
                msg = (
                    f"profiles {a.name!r} and {b.name!r} have overlapping time windows "
                    f"on {hit.isoformat()}"
                )
                add(a, b, msg, "narrow a window or change the days")
    return list(found.values())


def find_overlaps(
    profiles: Sequence[Profile],
    today: date,
    first: FirstTrain | None = None,
    *,
    first_options: FirstOptions | None = None,
    first_changes: Collection[date] | None = None,
) -> list[ProfileProblem]:
    """`first` gives the effective first train of a (profile, direction, commute day), for the
    dated check after an adoption (7.12 rule 19); without it the file's first trains count.
    `first_options` lists every baseline departure `first` may supply.
    `first_changes` lists the service dates that can change those departures."""
    return [
        e
        for _, _, e in overlap_pairs(
            profiles, today, first, first_options=first_options, first_changes=first_changes
        )
    ]
