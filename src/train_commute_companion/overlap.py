"""Whether profiles collide: shared days and overlapping time windows. Knows nothing of the file."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from train_commute_companion.calendar import TZ, WEEKDAYS, is_commute_day
from train_commute_companion.model import Direction, Profile, ProfileProblem
from train_commute_companion.schedule import dated_schedule

# a year and two weeks from two days back: every weekday pair meets both DST nights
_SCAN_DAYS = 380

FirstTrain = Callable[[Profile, Direction, date], time | None]


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
        recurring = day.isoweekday() in p.days
        real = is_commute_day(day, p.days, p.extra_dates, p.skip_public_holidays)
        if recurring or real:
            for d in Direction:
                s = dated_schedule(p, d, day, first_train(p, d, day) if first_train else None)
                out.setdefault(day, []).append(
                    _Occ(
                        day,
                        d,
                        recurring,
                        real,
                        s.window_start.astimezone(UTC),
                        s.window_end.astimezone(UTC),
                    )
                )
    return out


def _collision(x: _Occ, y: _Occ, floor: datetime, same: bool) -> datetime | None:
    if same and (x.day, x.direction) == (y.day, y.direction):
        return None  # the same occurrence when a profile is checked against itself
    # a weekly pattern collides in every non-holiday week, so holidays only count when a one-off
    # date is involved
    if not ((x.real and y.real) or (x.recurring and y.recurring)):
        return None
    lo, hi = max(x.start, y.start), min(x.end, y.end)
    return lo if lo < hi and hi > floor else None


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


def overlap_pairs(
    profiles: Sequence[Profile], today: date, first: FirstTrain | None = None
) -> list[tuple[Profile, Profile, ProfileProblem]]:
    found: dict[tuple[str, str], tuple[Profile, Profile, ProfileProblem]] = {}

    def add(a: Profile, b: Profile, msg: str, hint: str) -> None:
        found.setdefault((a.id, b.id), (a, b, ProfileProblem(a.id, "profiles", msg, hint)))

    pairs = [(a, b) for i, a in enumerate(profiles) for b in profiles[i + 1 :]]
    # a shared weekday collides on every non-holiday week
    for a, b in pairs:
        shared = a.days & b.days
        if shared:
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
                if d >= today and is_commute_day(d, y.days, y.extra_dates, y.skip_public_holidays)
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
    occ = {p.id: _occurrences(p, today, near, first) for p in profiles}
    floor = datetime.combine(today, time(), TZ).astimezone(UTC)
    for p in profiles:
        hit = _window_overlap(occ[p.id], occ[p.id], floor, True)
        if hit:
            msg = f"profile {p.name!r} has overlapping time windows on {hit.isoformat()}"
            add(p, p, msg, "narrow the outbound or return window")
    for a, b in pairs:
        if (a.id, b.id) not in found:
            hit = _window_overlap(occ[a.id], occ[b.id], floor, False)
            if hit:
                msg = (
                    f"profiles {a.name!r} and {b.name!r} have overlapping time windows "
                    f"on {hit.isoformat()}"
                )
                add(a, b, msg, "narrow a window or change the days")
    return list(found.values())


def find_overlaps(
    profiles: Sequence[Profile], today: date, first: FirstTrain | None = None
) -> list[ProfileProblem]:
    """`first` gives the effective first train of a (profile, direction, commute day), for the
    dated check after an adoption (7.12 rule 19); without it the file's first trains count."""
    return [e for _, _, e in overlap_pairs(profiles, today, first)]
