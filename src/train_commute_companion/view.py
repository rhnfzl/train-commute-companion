"""The read model for shells (5.4, 6.2, 7.3, 7.11, 7.13): frozen snapshots of what the core
follows, built by pure functions, so a shell never reaches into runtime state."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import pairwise

from train_commute_companion.classify import pair_transfer
from train_commute_companion.health import BoardHealth, board_health
from train_commute_companion.lifecycle import (
    Commitment,
    Ctx,
    DirectionState,
    LegProgress,
    LegState,
    LockSource,
    OccurrenceState,
    _next_good,
    _observe,
    _pool,
    _resolved,
    _unfinished,
)
from train_commute_companion.lookahead import BaselineHistory, LookAheadLedger
from train_commute_companion.model import (
    AdviceRecord,
    ClassifiedLeg,
    Connection,
    Direction,
    EventKind,
    Observation,
    OccurrenceKey,
    Profile,
    Provenance,
    Stop,
    TimeObs,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.transfer import check_transfer, transfer_minimum

_LIVE_ROUTE_CHANGED = f"live:{EventKind.ROUTE_CHANGED.value}:"  # runtime's live-fact prefix


@dataclass(frozen=True, slots=True)
class LegView:
    train_number: str
    board: str
    alight: str
    planned_departure: datetime
    departure: datetime | None  # reported times only (rule 20)
    planned_arrival: datetime | None
    arrival: datetime | None
    departure_platform: str | None
    arrival_platform: str | None
    departure_delay_min: int | None
    arrival_delay_min: int | None
    state: str  # the observation when not seen, else the kind (6.2)
    progress: LegState


@dataclass(frozen=True, slots=True)
class TransferView:
    station: str
    state: TransferState
    margin_min: int | None
    minimum: int


@dataclass(frozen=True, slots=True)
class OccurrenceView:
    key: OccurrenceKey
    profile_name: str
    direction_state: DirectionState
    resolved: bool
    not_travelling: bool
    route_changed: bool
    lock_source: LockSource | None
    commitment: Commitment | None
    legs: tuple[LegView, ...]
    transfers: tuple[TransferView, ...]
    retained: tuple[tuple[LegView, ...], ...]
    next_good: Connection | None
    advice: AdviceRecord | None
    health: BoardHealth


@dataclass(frozen=True, slots=True)
class DirectionInfo:
    profile_id: str
    profile_name: str
    direction: Direction
    max_transfers: int


@dataclass(frozen=True, slots=True)
class WorksDay:
    service_date: date
    leg_index: int | None  # None: the whole journey


@dataclass(frozen=True, slots=True)
class Moved:
    profile_id: str
    direction: Direction
    text: str


@dataclass(frozen=True, slots=True)
class RuntimeView:
    at: datetime
    started: bool
    profiles: tuple[Profile, ...]
    directions: tuple[DirectionInfo, ...]
    occurrences: tuple[OccurrenceView, ...]
    works: Mapping[str, tuple[WorksDay, ...]]  # "<profile_id>|<direction>"
    moved: tuple[Moved, ...]
    refusal: str | None
    store_ok: bool
    key_rejected: bool
    undelivered: int
    requests: int
    # profile id -> its location source epoch (8); a profile never changed has no entry
    location_epochs: Mapping[str, int] = field(default_factory=dict)
    # "<profile_id>|<direction>" for every direction of the current config: the last successful
    # look-ahead run (a failed works lookup leaves it), None with no ledger or no good run (7.10)
    lookahead_last_ok: Mapping[str, datetime | None] = field(default_factory=dict)


def _reported(obs: TimeObs | None) -> datetime | None:
    return obs.value if obs is not None and obs.provenance is Provenance.REPORTED else None


def _alight_track(s: Stop) -> str | None:
    """The platform the user gets off at: the arrival side's, else the departure side's."""
    return s.actual_arrival_track or s.planned_arrival_track or s.actual_track or s.planned_track


def _leg_view(x: LegProgress, cl: ClassifiedLeg | None, ob: Observation) -> LegView:
    state = ob.value if ob is not Observation.SEEN or cl is None else cl.kind.value
    if cl is None:
        return LegView(
            x.train.train_number, x.board_station, x.alight_stop, x.train.planned_departure,
            None, None, None, None, None, None, None, state, x.state,
        )  # fmt: skip
    board = cl.leg.board
    stop = next((st for st in cl.leg.stops[1:] if st.code == x.alight_stop), None)
    return LegView(
        x.train.train_number,
        x.board_station,
        x.alight_stop,
        x.train.planned_departure,
        _reported(board.departure),
        None if stop is None else stop.planned_arrival,
        None if stop is None else _reported(stop.arrival),
        board.actual_track or board.planned_track,
        None if stop is None else _alight_track(stop),
        cl.dep_delay_min,
        cl.arr_delay_min,
        state,
        x.state,
    )


def occurrence_view(
    s: OccurrenceState, ctx: Ctx, answers: Sequence[ProviderAnswer]
) -> OccurrenceView:
    pool = _pool(answers) if answers else None

    def observed(x: LegProgress) -> tuple[ClassifiedLeg | None, Observation]:
        if pool is None:
            return None, Observation.MISSING
        cl, ob, _ = _observe(pool, x)
        return cl, ob

    def legs(xs: Sequence[LegProgress]) -> tuple[LegView, ...]:
        return tuple(_leg_view(x, *observed(x)) for x in xs)

    transfers = []
    lock = s.lock.legs if s.lock else ()
    for a, b in pairwise(lock):
        station = b.board_station
        inc, out = observed(a)[0], observed(b)[0]
        if inc is None or out is None:  # not in the answers: never shown as fine (7.3)
            minimum = transfer_minimum(station, ctx.profile.transfer_min, None, None)
            transfers.append(TransferView(station, TransferState.AT_RISK, None, minimum))
            continue
        assert pool is not None  # inc was observed in it
        inc, out = pair_transfer(pool.answers, inc, out)
        t = check_transfer(inc, out, station, ctx.profile.transfer_min)
        transfers.append(TransferView(station, t.state, t.margin_min, t.minimum))

    resolved = _resolved(s)
    next_good = None
    if pool is not None and not resolved:
        x = None if s.lock is None else _unfinished(s.lock)
        if s.lock is None:
            next_good = _next_good(s, ctx, pool)
        elif x is not None:  # as `_left_note` names it (7.7, rule 7)
            i = s.lock.legs.index(x)
            kept = _observe(pool, s.lock.legs[i - 1])[0] if i else None
            if not i or kept is not None:
                next_good = _next_good(s, ctx, pool, x.train, x.board_station, kept, i)

    return OccurrenceView(
        s.key,
        ctx.profile.name,
        s.direction_state,
        resolved,
        s.not_travelling,
        any(k.startswith(_LIVE_ROUTE_CHANGED) for k in s.alert_keys),
        s.lock.source if s.lock else None,
        s.lock.commitment if s.lock else None,
        legs(lock),
        tuple(transfers),
        tuple(legs(j.legs) for j in s.retained),
        next_good,
        s.advice,
        board_health(s.poll_records, ctx.now),
    )


def works_ahead(ledger: LookAheadLedger, today: date) -> tuple[WorksDay, ...]:
    """Dates whose latest works mark is a warning, from `today` on (7.10)."""
    out = []
    for key, entries in ledger.per_date.items():
        marks = [e for e in entries if e.startswith(("warn:", "restored:"))]
        day, _, leg = key.partition("|")
        if marks and marks[-1].startswith("warn:") and date.fromisoformat(day) >= today:
            out.append(WorksDay(date.fromisoformat(day), None if leg == "j" else int(leg)))
    return tuple(sorted(out, key=lambda w: (w.service_date, w.leg_index is not None, w.leg_index)))


def moved(
    profiles: Sequence[Profile], baselines: Mapping[str, BaselineHistory]
) -> tuple[Moved, ...]:
    """Directions whose newest confirmed baseline starts on another first train than the file."""
    out = []
    for p in profiles:
        for d in Direction:
            hist = baselines.get(f"{p.id}|{d.value}")
            confirmed = [v for v in (hist.versions if hist else ()) if v.confirmed]
            if not confirmed:
                continue
            v = max(confirmed, key=lambda x: (x.effective_from, x.version))
            adopted, mine = v.signature.legs[0].departure, p.journey(d).first_train
            if adopted != mine:
                text = (
                    f"{p.name}.{d.value}: NS moved your {mine:%H:%M} to {adopted:%H:%M} "
                    f"from {v.effective_from.isoformat()}; update your profile?"
                )
                out.append(Moved(p.id, d, text))
    return tuple(out)
