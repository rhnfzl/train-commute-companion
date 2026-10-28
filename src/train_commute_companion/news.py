"""What is news: alert keys, severity table, escalation, all_good (7.5, 7.9, 7.12 rules 4, 20)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from train_commute_companion.calendar import TZ
from train_commute_companion.classify import classify_leg, find_leg
from train_commute_companion.health import FRESH_FOR, board_health
from train_commute_companion.lifecycle import (
    Board,
    Ctx,
    Journey,
    OccurrenceState,
    _event,
    announced,
)
from train_commute_companion.messages import body
from train_commute_companion.model import (
    AlertEvent,
    ClassifiedLeg,
    Connection,
    EventKind,
    Leg,
    LegKind,
    Observation,
    Provenance,
    Severity,
    TrainId,
    TransferCheck,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.transfer import check_transfer

AT_RISK_LOUD_MIN = 30
PLATFORM_LOUD_MIN = 15
_SILENT = frozenset(
    {
        EventKind.CAN_SEE_TRAIN_AGAIN,
        EventKind.ALL_GOOD,
        EventKind.NEARLY_ON_TIME,
        EventKind.BACK_TO_NORMAL,
        EventKind.RESTORED,
        EventKind.TIMETABLE_ADOPTED,
        EventKind.EARLIER_TRAIN_WITHDRAWN,
        EventKind.OFFLINE,
    }
)

Delays = tuple[int | None, int | None]  # (departure, arrival) delay a message last stated


def _ident(t: TrainId) -> str:
    return f"{t.train_number}@{t.planned_departure.isoformat()}"


def segment(leg: Leg) -> str:
    """A leg's identity for delay history: the train, by planned departure, over its stops."""
    return f"{_ident(leg.train)}/{leg.board.code}/{leg.alight.code}"


def announced_delays(
    last: Mapping[str, Delays] | None, classified: Sequence[ClassifiedLeg]
) -> dict[str, Delays]:
    """Per segment, the delays to state next, from what the last message stated about that
    same train over the same stops. A segment a relock brought in has no history and starts
    from its newly announced facts, never another train's delay at the same position."""
    prev = last or {}
    out = {}
    for c in classified:
        was = prev.get(segment(c.leg), (None, None))
        out[segment(c.leg)] = (
            announced(was[0], c.dep_delay_min),
            announced(was[1], c.arr_delay_min),
        )
    return out


def _track(leg: Leg) -> str:
    return str(leg.board.actual_track or leg.board.planned_track or "-")


def key_legs(
    classified: Sequence[ClassifiedLeg], replacement: Connection | None
) -> list[ClassifiedLeg]:
    """The legs a message states: the journey's, then the named replacement's. What
    `announced_delays` and `alert_key` take, in this order."""
    rep = () if replacement is None else replacement.legs
    return [*classified, *(classify_leg(x, x.alight.code) for x in rep)]


def alert_key(
    lock: Journey,
    classified: Sequence[ClassifiedLeg],
    transfers: Sequence[TransferCheck],
    replacement: Connection | None,
    delays: Mapping[str, Delays],
) -> str:
    """Everything a message states, per leg and per transfer, and the named replacement by
    every leg (train with planned departure, stations, platform, delays); only a change is
    news. `delays` come from `announced_delays` over `key_legs`, so they move only by news."""
    none: Delays = (None, None)
    legs = [
        "/".join(
            (
                _ident(c.leg.train),
                c.leg.board.code,
                c.leg.alight.code,
                # ok against delayed is the delay itself, which `delays` states by news
                "running" if c.kind in (LegKind.OK, LegKind.DELAYED) else c.kind.value,
                str(d[0] or 0),  # never stated and stated on time read the same
                str(d[1] or 0),
                _track(c.leg),
                c.last_served.code if c.last_served else "-",
            )
        )
        for c in key_legs(classified, replacement)
        for d in [delays.get(segment(c.leg), none)]
    ]
    n = len(classified)
    return "|".join(
        [
            lock.legs[0].train.operating_date.isoformat(),
            *legs[:n],
            *(t.state.value for t in transfers),
            "replacement",
            *(legs[n:] or ["-"]),
        ]
    )


def severity(
    kind: EventKind,
    *,
    minutes_to_departure: int | None,
    breaks_transfer: bool,
    past_latest_arrival: bool,
    is_tomorrow: bool,
) -> Severity:
    """Table 7.9. A kind without a rule is loud: unmapped is loud."""
    soon = minutes_to_departure is None  # unknown timing errs on the loud side
    if kind is EventKind.TRANSFER_AT_RISK:
        loud = soon or minutes_to_departure <= AT_RISK_LOUD_MIN  # type: ignore[operator]
    elif kind is EventKind.PLATFORM_CHANGE:
        loud = soon or minutes_to_departure <= PLATFORM_LOUD_MIN  # type: ignore[operator]
    elif kind is EventKind.DELAYED:
        loud = breaks_transfer or past_latest_arrival
    elif kind is EventKind.WORKS_AHEAD:
        loud = is_tomorrow
    else:
        loud = kind not in _SILENT
    return Severity.LOUD if loud else Severity.SILENT


@dataclass(frozen=True, slots=True)
class Fact:
    """One fact in the delivery ledger with what decides its severity now: the departure of
    the leg or onward train it is about, the day it affects (works), and the delay facts."""

    key: str
    kind: EventKind
    departure: datetime | None = None
    day: date | None = None
    breaks_transfer: bool = False
    past_latest_arrival: bool = False


def escalations(ledger: Mapping[str, Severity], facts: Sequence[Fact], now: datetime) -> list[str]:
    """Fact keys sent silent that now cross a loud boundary (rule 4); each goes out once more.
    Each fact is timed by its own departure and day, never the journey's first train."""
    tomorrow = now.astimezone(TZ).date() + timedelta(days=1)
    out = []
    for f in facts:
        if ledger.get(f.key) is not Severity.SILENT:
            continue
        minutes = (
            None
            if f.departure is None
            else int((_utc(f.departure) - _utc(now)) // timedelta(minutes=1))
        )
        sev = severity(
            f.kind,
            minutes_to_departure=minutes,
            breaks_transfer=f.breaks_transfer,
            past_latest_arrival=f.past_latest_arrival,
            is_tomorrow=f.day == tomorrow,
        )
        if sev is Severity.LOUD:
            out.append(f.key)
    return out


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC)


def unsent(ledger: Mapping[str, Severity], events: Sequence[AlertEvent]) -> list[AlertEvent]:
    """Ongoing facts repeat on every poll with the same key; only a key not yet sent is news."""
    out: dict[str, AlertEvent] = {}
    for e in events:
        if e.key not in ledger:
            out.setdefault(e.key, e)
    return list(out.values())


def _contrary(answers: Sequence[ProviderAnswer], train: TrainId, board: str, alight: str) -> bool:
    """Any copy of the train in any answer of the board that is not ok: a cancelled copy, or
    one that reaches the alighting stop delayed or not served, whatever order they come in."""
    for a in answers:
        for conn in a.connections:
            for g in conn.legs:
                if g.train != train or g.board.code != board:
                    continue
                c = classify_leg(g, alight)
                # a copy cut where its connection changes earlier says nothing about `alight`
                reaches = any(st.code == alight for st in g.stops[1:])
                if c.kind is LegKind.CANCELLED or (reaches and c.kind is not LegKind.OK):
                    return True
    return False


def _copies(
    answers: Sequence[ProviderAnswer], train: TrainId, board: str, alight: str | None
) -> list[ClassifiedLeg]:
    """Every copy of the train boarding at `board`, seen; with `alight`, only the copies whose
    connection changes there, since their transfer flags are about that change."""
    return [
        replace(classify_leg(g, alight or g.alight.code), observation=Observation.SEEN)
        for a in answers
        for conn in a.connections
        for g in conn.legs
        if g.train == train and g.board.code == board and alight in (None, g.alight.code)
    ]


def all_good(s: OccurrenceState, board: Board, ctx: Ctx) -> AlertEvent | None:
    """Evaluated once, at the first lock (Q4, OQ3 = A). The caller sets `all_good_evaluated`.
    Rule 20: the newest answer is fresh and contains every locked leg, seen, ok and with
    reported times, every transfer is ok, the board is healthy now (no failed poll since the
    last success), and no answer of the board says otherwise (6.1: one identity across all
    answers; no copy cancelled or late). When in doubt, none."""
    if s.all_good_evaluated or s.lock is None or not board.answers:
        return None
    h = board_health((*s.poll_records, *board.polls), ctx.now)
    if not h.fresh or h.consecutive_failures or h.auth_failed:
        return None
    newest = max(board.answers, key=lambda a: _utc(a.fetched_at))
    if _utc(ctx.now) - _utc(newest.fetched_at) >= FRESH_FOR:
        return None
    merged = ProviderAnswer(
        newest.query, tuple(c for a in board.answers for c in a.connections), newest.fetched_at
    )
    seen: list[ClassifiedLeg] = []
    for x in s.lock.legs:
        found, obs = find_leg(newest, x.train, x.board_station)
        if found is None or obs is not Observation.SEEN:
            return None
        if find_leg(merged, x.train, x.board_station)[1] is not Observation.SEEN:
            return None
        if _contrary(board.answers, x.train, x.board_station, x.alight_stop):
            return None
        if any(
            g.leg.alternative_transport
            for g in _copies(board.answers, x.train, x.board_station, None)
        ):
            return None  # 6.3: a train any answer shows as other transport is never "on time"
        c = replace(classify_leg(found, x.alight_stop), observation=Observation.SEEN)
        times = (found.board.departure, found.alight.arrival)
        if c.kind is not LegKind.OK or any(
            t is None or t.provenance is not Provenance.REPORTED for t in times
        ):
            return None
        seen.append(c)
    for a, b, x, y in zip(seen, seen[1:], s.lock.legs, s.lock.legs[1:], strict=False):
        # every copy that changes here, whatever order the answers list them in (6.1): a copy
        # saying the change is not possible or not reachable is evidence against all_good
        ins = [a, *_copies(board.answers, x.train, x.board_station, x.alight_stop)]
        outs = [b, *_copies(board.answers, y.train, y.board_station, None)]
        if any(
            check_transfer(i, o, x.alight_stop, ctx.profile.transfer_min).state
            is not TransferState.OK
            for i in ins
            for o in outs
        ):
            return None
    legs = [c.leg for c in seen]
    return _event(
        ctx, EventKind.ALL_GOOD, Severity.SILENT, body(EventKind.ALL_GOOD, legs=legs), legs[0].train
    )
