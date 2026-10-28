"""Per-leg classification and train identity (6.1, 6.2). Pure functions, no clock."""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from itertools import pairwise

from train_commute_companion.model import (
    ClassifiedLeg,
    Connection,
    Leg,
    LegKind,
    Observation,
    Provenance,
    Stop,
    TimeObs,
    TrainId,
)
from train_commute_companion.providers.base import ProviderAnswer

DELAYED_AT_MIN = 3

log = logging.getLogger(__name__)


def _delay(obs: TimeObs | None) -> int | None:
    if obs is None or obs.planned is None or obs.value is None:
        return None
    # same-zone subtraction ignores the DST fold, so subtract instants
    return max(
        0, int((obs.value.astimezone(UTC) - obs.planned.astimezone(UTC)).total_seconds() // 60)
    )


def served(s: Stop) -> bool:
    """The train calls here: not passing, and not cancelled unless it has a reported time
    (rule 14: a cancelled stop with a reported, actual, time was still served)."""
    reported = any(o and o.provenance is Provenance.REPORTED for o in (s.arrival, s.departure))
    return not s.passing and (not s.cancelled or reported)


def _expected_departure(conn: Connection) -> datetime:
    d = conn.legs[0].board.departure
    t = d.value if d and d.value else d.planned if d else None
    return (t or conn.legs[0].train.planned_departure).astimezone(UTC)


def sort_by_expected_departure(conns: Iterable[Connection]) -> list[Connection]:
    return sorted(conns, key=_expected_departure)


def classify_leg(leg: Leg, alight_station: str) -> ClassifiedLeg:
    """Kind comes from the stops between boarding and `alight_station`, never from one flag.
    A train that does not call at `alight_station` (absent, passed or cancelled there) is cut
    short at its last served stop, or cancelled when it serves none; never ok."""
    stops = leg.stops
    end = next((i for i, s in enumerate(stops) if i > 0 and s.code == alight_station), None)
    chain = stops if end is None else stops[: end + 1]
    kind = LegKind.OK
    last_served: Stop | None = None
    if not served(chain[0]):
        kind = LegKind.CANCELLED
    elif end is None or not served(chain[-1]):
        calls = [s for s in chain[1:] if served(s) and s.code != alight_station]
        if calls:
            kind, last_served = LegKind.CUT_SHORT, calls[-1]
        else:
            kind = LegKind.CANCELLED
    dep = _delay(chain[0].departure)
    arr = _delay(chain[-1].arrival)
    if kind is LegKind.CANCELLED:
        dep, arr = 0, None
    elif kind is LegKind.CUT_SHORT:
        arr = None
    elif (dep or 0) >= DELAYED_AT_MIN or (arr or 0) >= DELAYED_AT_MIN:
        kind = LegKind.DELAYED
    return ClassifiedLeg(leg, kind, Observation.SEEN, dep, arr, last_served)


def _minute(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(second=0, microsecond=0)


def find_leg(
    answer: ProviderAnswer, train: TrainId, board_station: str
) -> tuple[Leg | None, Observation]:
    """Exact planned departure at `board_station` plus the stored number; never by proximity.
    Identity is judged over every answer of the poll (6.1); a leg is returned from
    `answer.connections` first, so a pool's own candidates keep their copy."""
    want = _minute(train.planned_departure)
    found: dict[tuple[object, str], Leg] = {}
    for conn in (*answer.connections, *(c for a in evidence(answer) for c in a.connections)):
        for leg in conn.legs:
            dep = leg.board.planned_departure
            if leg.board.code == board_station and dep is not None and _minute(dep) == want:
                found.setdefault((leg.train.operating_date, leg.train.train_number), leg)
    if not found:
        return None, Observation.MISSING
    if len(found) == 1:
        ((key, leg),) = found.items()
        if key == (train.operating_date, train.train_number):
            return leg, Observation.SEEN
    return None, Observation.UNCERTAIN


def evidence(answer: ProviderAnswer) -> tuple[ProviderAnswer, ...]:
    # a lifecycle pool keeps one poll's answers apart
    answers: tuple[ProviderAnswer, ...] = getattr(answer, "answers", ())
    return answers or (answer,)


def _newest_first(answers: Sequence[ProviderAnswer]) -> list[ProviderAnswer]:
    return sorted(answers, key=lambda a: a.fetched_at.astimezone(UTC), reverse=True)


def _reaches(leg: Leg, alight: str) -> bool:
    return any(s.code == alight for s in leg.stops[1:])


_TRUST = {Provenance.MISSING: 0, Provenance.PLANNED: 1, Provenance.REPORTED: 2}


def _when(o: TimeObs | None, sign: int) -> tuple[int, float]:
    """Sort key, worst first: the least trusted time, then the earliest (`sign` 1) or the
    latest (`sign` -1)."""
    t = None if o is None else o.value or o.planned
    return (0 if o is None else _TRUST[o.provenance]), (sign * t.timestamp() if t else 0.0)


def _arrival_at(leg: Leg, alight: str) -> TimeObs | None:
    return next((s.arrival for s in leg.stops[1:] if s.code == alight), None)


def _explicit_cut(leg: Leg, alight: str) -> bool:
    """Only an unserved `alight`, or a cancelled stop before it, proves a cut short: a copy
    from another connection simply ends where that connection changes (a passing stop or a
    truncated list is no evidence)."""
    end = next((i for i, s in enumerate(leg.stops) if i > 0 and s.code == alight), None)
    if end is not None and not served(leg.stops[end]):
        return True
    between = leg.stops[1:] if end is None else leg.stops[1:end]
    return any(s.cancelled and not served(s) for s in between)


def _cut_at(c: ClassifiedLeg) -> tuple[bool, float, int]:
    """Sort key, worst first: a cancellation, then the earliest last served stop (by its
    planned time, then by the stops served up to it)."""
    s = c.last_served
    t = None if s is None else s.planned_arrival or s.planned_departure
    calls = sum(map(served, c.leg.stops))
    return c.kind is not LegKind.CANCELLED, t.timestamp() if t else 0.0, calls


def _unchanged_track(s: Stop) -> tuple[bool, str]:
    """Sort key, worst first: a platform other than planned, then by name for any order."""
    return s.actual_track in (None, s.planned_track), s.actual_track or ""


def current_run(
    answers: Sequence[ProviderAnswer], train: TrainId, board: str, alight: str
) -> tuple[datetime, ClassifiedLeg] | None:
    """The current copy of `train` boarding at `board` over one poll's answers, and the
    fetch time of the answer that showed its arrival (rule 6 times acceptance by it)."""
    runs = [
        (a.fetched_at, classify_leg(g, alight))
        for a in _newest_first(answers)
        for c in a.connections
        for g in c.legs
        if g.train == train and g.board.code == board
    ]
    if not runs:
        return None

    def rank(r: tuple[datetime, ClassifiedLeg]) -> tuple[bool, bool]:
        # never by stop count: two copies that both reach `alight` tie, and max keeps the newest
        return r[1].kind not in (LegKind.CANCELLED, LegKind.CUT_SHORT), _reaches(r[1].leg, alight)

    def stopped(c: ClassifiedLeg) -> bool:
        return c.kind is LegKind.CANCELLED or (
            c.kind is LegKind.CUT_SHORT and _explicit_cut(c.leg, alight)
        )

    # by instant: two local 02:30s of the DST fold are an hour apart (7.13)
    top = runs[0][0].astimezone(UTC)
    at_top = [r for r in runs if r[0].astimezone(UTC) == top]
    # transport is a fact of the newest fetch, whatever copy wins on stops; when its copies
    # disagree, the non-train one counts (6.3: never a train that may be a bus)
    flags = {r[1].leg.alternative_transport for r in at_top}
    if len(flags) > 1:
        log.warning("conflicting transport of train %s at one fetch", train.train_number)
    mode = next(r[1].leg for r in at_top if r[1].leg.alternative_transport is (True in flags))
    bad = [r for r in at_top if stopped(r[1])]
    if bad:
        # rule 7.12: conflicting newest copies never read as running, a missed alarm costs more
        if len(bad) < len(at_top):
            log.warning("conflicting copies of train %s at one fetch", train.train_number)
        # in any order: a cancellation, else the earliest cut, then the worst departure
        seen, win = min(bad, key=lambda r: (_cut_at(r[1]), _when(r[1].leg.board.departure, 1)))
    else:
        # copies of one fetch that disagree on timing give the worst of each end, in any
        # order (7.3, 7.12): the earliest departure and the latest arrival, least trusted first;
        # a truncated copy still runs, so its departure counts too
        board_stop = min((r[1].leg.stops[0] for r in at_top), key=lambda s: _when(s.departure, 1))
        best = max(map(rank, runs))  # a truncated newer copy never reads as a cut short
        at = next(r[0] for r in runs if rank(r) == best).astimezone(UTC)
        seen, win = min(
            (r for r in runs if rank(r) == best and r[0].astimezone(UTC) == at),
            key=lambda r: _when(_arrival_at(r[1].leg, alight), -1),
        )
        # departure time, platform and provenance always from the newest answer
        stops = (board_stop, *win.leg.stops[1:])
        win = classify_leg(replace(win.leg, stops=stops), alight)
    # a changed platform from any newest copy is kept: a missed change costs more (7.12)
    track = min((r[1].leg.board for r in at_top), key=_unchanged_track)
    here = replace(
        win.leg.board, planned_track=track.planned_track, actual_track=track.actual_track
    )
    # every newest copy's messages: works told by one copy are never lost (7.10)
    said = sorted({r[1].leg.messages for r in (*at_top, (seen, win))})
    messages = tuple(dict.fromkeys(m for ms in said for m in ms))
    win = replace(win, leg=replace(win.leg, stops=(here, *win.leg.stops[1:]), messages=messages))
    leg = replace(win.leg, alternative_transport=mode.alternative_transport, category=mode.category)
    return seen, replace(win, leg=leg)


def _pair(answers: Sequence[ProviderAnswer], inc: TrainId, out: TrainId) -> tuple[Leg, Leg] | None:
    """The newest copy of the change from `inc` directly into `out`. Copies that disagree at
    the newest fetch are merged to the worst of each field (7.12: never a missed alarm)."""
    found = [
        (a.fetched_at.astimezone(UTC), p, q)
        for a in answers
        for c in a.connections
        for p, q in pairwise(c.legs)
        if p.train == inc and q.train == out
    ]
    if not found:
        return None
    top = max(t for t, _, _ in found)
    pairs = [(p, q) for t, p, q in found if t == top]
    p, q = pairs[0]
    fields = {
        (
            x.change_possible,
            x.transfer_time_to_next,
            x.reachable,
            y.transfer_message_min,
            y.reachable,
        )
        for x, y in pairs
    }
    if len(fields) == 1:
        return p, q
    log.warning("conflicting copies of the change %s to %s", inc.train_number, out.train_number)

    def worst(vals: Iterable[bool | None]) -> bool | None:
        v = set(vals)
        return False if False in v else None if None in v else True

    def most(vals: Iterable[int | None]) -> int | None:
        return max((v for v in vals if v is not None), default=None)

    return (
        replace(
            p,
            change_possible=worst(x.change_possible for x, _ in pairs),
            transfer_time_to_next=most(x.transfer_time_to_next for x, _ in pairs),
            reachable=worst(x.reachable for x, _ in pairs),
        ),
        replace(
            q,
            transfer_message_min=most(y.transfer_message_min for _, y in pairs),
            reachable=worst(y.reachable for _, y in pairs),
        ),
    )


def _with_change(inc: Leg, out: Leg, pair: tuple[Leg, Leg] | None) -> tuple[Leg, Leg]:
    if pair is None:
        return (
            replace(
                inc,
                change_possible=None,
                transfer_time_to_next=None,
                cross_platform=None,
                reachable=None,
            ),
            replace(out, transfer_message_min=None, reachable=None),
        )
    p, q = pair
    return (
        replace(
            inc,
            change_possible=p.change_possible,
            transfer_time_to_next=p.transfer_time_to_next,
            cross_platform=p.cross_platform,
            reachable=p.reachable,
        ),
        # rule 9: the transfer message belongs to the outgoing leg (transfer.py)
        replace(out, transfer_message_min=q.transfer_message_min, reachable=q.reachable),
    )


def pair_transfer(
    answers: Sequence[ProviderAnswer], inc: ClassifiedLeg, out: ClassifiedLeg
) -> tuple[ClassifiedLeg, ClassifiedLeg]:
    """The transfer fields of one change, from that change itself, or None when no answer
    holds it."""
    # a copy of the train from another connection carries that connection's change
    a, b = _with_change(inc.leg, out.leg, _pair(answers, inc.leg.train, out.leg.train))
    return replace(inc, leg=a), replace(out, leg=b)


def current_connection(answers: Sequence[ProviderAnswer], c: Connection) -> Connection:
    """`c` with each leg's current copy, for times, ranking and as a returned copy only: a
    replaced leg may end before `c`'s alight stop, so it is never judged nor normalized again."""
    legs: list[Leg] = []
    for leg in c.legs:
        run = current_run(answers, leg.train, leg.board.code, leg.alight.code)
        if run is None:
            legs.append(leg)
            continue
        g = run[1].leg
        end = next((i for i, s in enumerate(g.stops) if i > 0 and s.code == leg.alight.code), None)
        legs.append(
            replace(
                leg,
                stops=g.stops if end is None else g.stops[: end + 1],
                cancelled=g.cancelled,
                part_cancelled=g.part_cancelled,
                messages=g.messages,
                alternative_transport=g.alternative_transport,
                category=g.category,
            )
        )
    reach: list[list[bool | None]] = [[] for _ in legs]
    for i in range(len(legs) - 1):
        # c holds the pair itself, so its own two legs are the fallback
        pair = _pair(answers, c.legs[i].train, c.legs[i + 1].train) or (c.legs[i], c.legs[i + 1])
        legs[i], legs[i + 1] = _with_change(legs[i], legs[i + 1], pair)
        reach[i].append(pair[0].reachable)
        reach[i + 1].append(pair[1].reachable)
    for i, r in enumerate(reach):
        if False in r:  # a leg in two changes stays unreachable when either change says so
            legs[i] = replace(legs[i], reachable=False)
    return replace(c, legs=tuple(legs))
