"""A train named by its clock time (shells, 7.13): what it names and which connections of an answer
match it. Pure: time and data are arguments, the runtime fetches and applies the choice."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from train_commute_companion.calendar import TZ, floor_to_minute, resolve_local, to_utc
from train_commute_companion.lifecycle import first_open, target_occurrence
from train_commute_companion.model import Connection, Ctx, Leaving, LegState
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.revisions import occurrence_ctx
from train_commute_companion.state import LegProgress, OccurrenceState
from train_commute_companion.store import PersistedState


@dataclass(frozen=True, slots=True)
class ChoiceTarget:
    occ: OccurrenceState
    ctx: Ctx
    station: str
    when: datetime


def _where_and_when(s: OccurrenceState, ctx: Ctx) -> tuple[str, datetime, datetime]:
    """The station a new choice boards at and the UTC span its departure must fall in."""
    n = 0 if s.lock is None else first_open(s.lock)
    if s.lock and n:
        # an onward train continues the kept incoming leg: dated from it, not the window
        lo = to_utc(s.lock.legs[n - 1].train.planned_departure)
        return s.lock.legs[n - 1].alight_stop, lo, lo + timedelta(days=1)
    w = ctx.schedule
    return ctx.profile.origin(ctx.direction), to_utc(w.window_start), to_utc(w.window_end)


def choice_target(
    cands: Sequence[OccurrenceState],
    departure: time,
    train_number: str | None,
    service_date: date | None,
    now: datetime,
    st: PersistedState,
) -> ChoiceTarget | str | None:
    """Where and when the named train leaves. A string is why it cannot; None: no occurrence."""

    def named_leg(x: LegProgress) -> bool:
        t = x.train.planned_departure.astimezone(TZ)
        return (
            x.state is not LegState.DONE
            and (t.hour, t.minute) == (departure.hour, departure.minute)
            and train_number in (None, x.train.train_number)
        )

    # a train of the lock or of a retained remainder: the one the user may ride (N3)
    hit = next(
        (
            (s, x)
            for s in cands
            for j in (*((s.lock,) if s.lock else ()), *s.retained)
            for x in j.legs
            if named_leg(x)
        ),
        None,
    )
    if hit is not None:
        occ, x = hit
        ctx = occurrence_ctx(st, occ, now)
        if ctx is None:
            return None
        return ChoiceTarget(occ, ctx, x.board_station, x.train.planned_departure)
    # at a transfer station: target_occurrence would skip it as riding
    found = next(
        (s for s in cands if s.lock and 0 < first_open(s.lock) < len(s.lock.legs)),
        None,
    ) or target_occurrence(cands, Leaving(service_date, now))
    ctx = None if found is None else occurrence_ctx(st, found, now)
    if found is None or ctx is None:
        return None
    station, lo, hi = _where_and_when(found, ctx)
    first = lo.astimezone(TZ).date()
    inside = sorted(
        (
            t for d in (first, first + timedelta(days=1)) for edge in ("start", "end")
            if lo <= to_utc(t := resolve_local(d, departure, edge=edge)) < hi
        ),
        key=to_utc,
    )  # fmt: skip
    if not inside:
        return f"{departure:%H:%M} is outside the {ctx.direction.value} window"
    return ChoiceTarget(found, ctx, station, inside[0])


def named_connections(
    got: ProviderAnswer, t: ChoiceTarget, departure: time, train_number: str | None
) -> list[Connection] | str:
    """The answer's connections on the named train, earliest arrival first; else why none."""
    minute = floor_to_minute(t.when)
    same = [
        c for c in got.connections
        if c.legs and c.legs[0].board.code == t.station
        and floor_to_minute(c.legs[0].train.planned_departure) == minute
    ]  # fmt: skip
    named_at = f"{departure:%H:%M}"
    # 6.1: two trains in one boarding minute are uncertain even by number, so check_good refuses
    if len({(c.legs[0].train.operating_date, c.legs[0].train.train_number) for c in same}) > 1:
        return f"two trains leave {t.station} at {named_at}; NS data cannot tell them apart"
    found = [c for c in same if train_number in (None, c.legs[0].train.train_number)]
    if not found:
        return f"no train at {named_at} from {t.station} in NS data"
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(found, key=lambda c: to_utc(c.legs[-1].alight.planned_arrival or far))
