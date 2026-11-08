"""The read model for shells (5.4, 6.2, 7.3, 7.11, 7.13): frozen snapshots of what the core
follows, built by pure functions, so a shell never reaches into runtime state."""

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Literal

from train_commute_companion.calendar import TZ
from train_commute_companion.classify import pair_transfer
from train_commute_companion.disruptions import disruptions_fresh
from train_commute_companion.health import BoardHealth, board_health
from train_commute_companion.lifecycle import (
    find_next_good,
    is_resolved,
    merged_pool,
    next_good_instead_of,
    observe,
)
from train_commute_companion.lifecycle_lock import unfinished_leg
from train_commute_companion.location import (
    Fix,
    LocationKind,
    Zone,
    ZoneTrack,
    fix_zone,
    next_zone,
    seed_zone,
    station_zones,
)
from train_commute_companion.lookahead_probe import (
    WHOLE_JOURNEY,
    WORKS_MARKS,
    is_warning,
    latest_mark,
    split_day_key,
)
from train_commute_companion.model import (
    AdviceRecord,
    ClassifiedLeg,
    Connection,
    CrowdForecast,
    Ctx,
    Direction,
    DirectionState,
    EventKind,
    LegKind,
    LegState,
    Observation,
    OccurrenceKey,
    Profile,
    Provenance,
    Stop,
    TimeObs,
    TransferState,
    direction_key,
)
from train_commute_companion.providers.base import ActiveDisruption, ProviderAnswer, Query
from train_commute_companion.recovery import (
    RecoveryItinerary,
    payload,
)
from train_commute_companion.recovery_candidates import recovery_candidates
from train_commute_companion.recovery_state import RecoveryOffer, SelectedRecovery
from train_commute_companion.revisions import find_profile, occurrence_ctx, profile_for
from train_commute_companion.serde import encode
from train_commute_companion.state import (
    BaselineHistory,
    Commitment,
    LegProgress,
    LockSource,
    LookAheadLedger,
    OccurrenceState,
    holds_live,
)
from train_commute_companion.store import PersistedState
from train_commute_companion.transfer import check_transfer, transfer_minimum
from train_commute_companion.vehicle import VehicleSnapshot


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
    state: LegKind | Observation  # the observation when not seen, else the kind (6.2)
    progress: LegState
    crowd_forecast: CrowdForecast | None = None
    boarding_notes: tuple[str, ...] = ()
    alighting_notes: tuple[str, ...] = ()
    exit_side: Literal["left", "right"] | None = None


@dataclass(frozen=True, slots=True)
class TransferView:
    station: str
    state: TransferState
    margin_min: int | None
    minimum: int


@dataclass(frozen=True, slots=True)
class RecoveryPlanView:
    checked_at: datetime
    itinerary: RecoveryItinerary
    source_query: Query | None = None
    occurrence: OccurrenceKey | None = None
    generation: int = 0
    route_revision: int = 0

    @property
    def proposal_id(self) -> str:
        query = self.source_query
        anchors = (
            None
            if query is None
            else (query.origin, query.destination, query.via, query.require_accessible)
        )
        services = [
            step.service_key
            or (
                step.mode,
                step.operator,
                step.service_reference,
                None if step.mode == "WALK" else step.planned_departure,
                (step.origin.physical_id, step.origin.latitude, step.origin.longitude),
                (
                    step.destination.physical_id,
                    step.destination.latitude,
                    step.destination.longitude,
                ),
            )
            for step in self.itinerary.steps
        ]
        identity = encode(
            (self.occurrence, self.generation, self.route_revision, anchors, services)
        )
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]

    @property
    def selectable(self) -> bool:
        return (
            self.source_query is not None
            and self.occurrence is not None
            and all(
                step.mode == "WALK" or step.service_key is not None for step in self.itinerary.steps
            )
        )

    def fields(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "selectable": self.selectable,
            "source_query": encode(self.source_query),
            "checked_at": self.checked_at.isoformat(),
            "itinerary": payload(self.itinerary),
        }


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
    disruptions: tuple[ActiveDisruption, ...] = ()
    disruptions_checked_at: datetime | None = None
    disruptions_coverage: str = "unknown"
    replacement_plans: tuple[RecoveryPlanView, ...] = ()
    recovery_offer: RecoveryOffer | None = None
    selected_recovery: SelectedRecovery | None = None
    train_context: VehicleSnapshot | None = None


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
    # station code -> (lat, lon) from the provider's last station list; empty until it answers
    positions: Mapping[str, tuple[float, float]] = field(default_factory=dict)


def running_stations(occurrences: Iterable[OccurrenceView], profile_id: str) -> set[str]:
    """Stations of the profile's running and retained legs: an old route keeps its zones."""
    return {
        s
        for o in occurrences
        if o.key.profile_id == profile_id
        for leg in (*o.legs, *(x for r in o.retained for x in r))
        for s in (leg.board, leg.alight)
    }


def track_fix(
    view: RuntimeView,
    profile: Profile,
    epoch: int,
    fix: Fix,
    track: ZoneTrack | None,
    places: Sequence[Zone] = (),
) -> tuple[ZoneTrack, list[tuple[str, LocationKind]]] | None:
    """A fix read under `epoch` against the view: the new track and the events it owes, the first
    fix of a source (`track` None) only seeding (7.2). `places` are zones the shell knows beside
    the stations. None drops the fix: the source changed since `epoch`, a switch back included,
    or the fix cannot place the user in a station circle (8, rule 6)."""
    loc = profile.location
    if loc is None or view.location_epochs.get(profile.id, 0) != epoch:
        return None
    if fix.acc is not None and fix.acc > loc.station_radius_m:
        return None
    running = running_stations(view.occurrences, profile.id)
    stations = station_zones(profile, loc.station_radius_m, view.positions, running)
    zone = fix_zone(fix, loc, [*stations, *places])
    return (seed_zone(zone, fix.at), []) if track is None else next_zone(track, zone, fix.at)


def _reported(obs: TimeObs | None) -> datetime | None:
    return obs.value if obs is not None and obs.provenance is Provenance.REPORTED else None


def alight_track(s: Stop) -> str | None:
    """The platform the user gets off at: the arrival side's, else the departure side's."""
    return s.actual_arrival_track or s.planned_arrival_track or s.actual_track or s.planned_track


def _leg_view(x: LegProgress, cl: ClassifiedLeg | None, ob: Observation) -> LegView:
    state = ob if ob is not Observation.SEEN or cl is None else cl.kind
    if cl is None:
        return LegView(
            train_number=x.train.train_number,
            board=x.board_station,
            alight=x.alight_stop,
            planned_departure=x.train.planned_departure,
            departure=None,
            planned_arrival=None,
            arrival=None,
            departure_platform=None,
            arrival_platform=None,
            departure_delay_min=None,
            arrival_delay_min=None,
            state=state,
            progress=x.state,
        )
    board = cl.leg.board
    stop = next((st for st in cl.leg.stops[1:] if st.code == x.alight_stop), None)
    return LegView(
        train_number=x.train.train_number,
        board=x.board_station,
        alight=x.alight_stop,
        planned_departure=x.train.planned_departure,
        departure=_reported(board.departure),
        planned_arrival=None if stop is None else stop.planned_arrival,
        arrival=None if stop is None else _reported(stop.arrival),
        departure_platform=board.actual_track or board.planned_track,
        arrival_platform=None if stop is None else alight_track(stop),
        departure_delay_min=cl.dep_delay_min,
        arrival_delay_min=cl.arr_delay_min,
        state=state,
        progress=x.state,
        crowd_forecast=cl.leg.crowd_forecast,
        boarding_notes=board.accessibility_notes,
        alighting_notes=() if stop is None else stop.accessibility_notes,
        exit_side=None if stop is None else stop.exit_side,
    )


def _replacement_plans(
    answers: Sequence[ProviderAnswer], ctx: Ctx, generation: int = 0, route_revision: int = 0
) -> tuple[RecoveryPlanView, ...]:
    return tuple(
        RecoveryPlanView(
            candidate.checked_at,
            candidate.itinerary,
            candidate.source_query,
            ctx.schedule.key,
            generation,
            route_revision,
        )
        for candidate in recovery_candidates(answers, ctx)
    )


def occurrence_view(
    s: OccurrenceState, ctx: Ctx, answers: Sequence[ProviderAnswer]
) -> OccurrenceView:
    pool = merged_pool(answers) if answers else None

    def observed(x: LegProgress) -> tuple[ClassifiedLeg | None, Observation]:
        if pool is None:
            return None, Observation.MISSING
        cl, ob, _ = observe(pool, x)
        return cl, ob

    def legs(xs: Sequence[LegProgress]) -> tuple[LegView, ...]:
        return tuple(_leg_view(x, *observed(x)) for x in xs)

    transfers = []
    lock = s.lock.legs if s.lock else ()
    for a, b in pairwise(lock):
        station = b.board_station
        inc, out = observed(a)[0], observed(b)[0]
        # not in the answers: never shown as fine (7.3)
        if pool is None or inc is None or out is None:
            minimum = transfer_minimum(station, ctx.profile.transfer_min, None, None)
            transfers.append(TransferView(station, TransferState.AT_RISK, None, minimum))
            continue
        inc, out = pair_transfer(pool.answers, inc, out)
        t = check_transfer(inc, out, station, ctx.profile.transfer_min)
        transfers.append(TransferView(station, t.state, t.margin_min, t.minimum))

    resolved = is_resolved(s) and not (
        s.selected_recovery is not None and s.selected_recovery.status == "active"
    )
    next_good = None
    if pool is not None and not resolved:
        x = None if s.lock is None else unfinished_leg(s.lock)
        if s.lock is None:
            next_good = find_next_good(s, ctx, pool)
        elif x is not None:  # as `left_note` names it (7.7, rule 7)
            next_good = next_good_instead_of(s, ctx, pool, s.lock, s.lock.legs.index(x))[1]

    coverage = "complete" if disruptions_fresh(s, ctx.now) else "unknown"
    return OccurrenceView(
        key=s.key,
        selected_recovery=s.selected_recovery,
        disruptions=tuple(n.item for n in s.disruptions.values() if n.active and n.relevant),
        disruptions_checked_at=s.disruptions_checked_at,
        disruptions_coverage=coverage,
        profile_name=ctx.profile.name,
        direction_state=s.direction_state,
        resolved=resolved,
        not_travelling=s.not_travelling,
        route_changed=holds_live(s.alert_keys, (EventKind.ROUTE_CHANGED,)),
        lock_source=s.lock.source if s.lock else None,
        commitment=s.lock.commitment if s.lock else None,
        legs=legs(lock),
        transfers=tuple(transfers),
        retained=tuple(legs(j.legs) for j in s.retained),
        next_good=None
        if s.selected_recovery is not None and s.selected_recovery.status == "active"
        else next_good,
        advice=None
        if s.selected_recovery is not None and s.selected_recovery.status == "active"
        else s.advice,
        health=board_health(s.poll_records, ctx.now),
        replacement_plans=()
        if resolved or s.not_travelling
        else _replacement_plans(answers, ctx, s.generation, s.route_revision),
    )


def works_ahead(ledger: LookAheadLedger, today: date) -> tuple[WorksDay, ...]:
    """Dates whose latest works mark is a warning, from `today` on (7.10)."""
    out = []
    for key, entries in ledger.per_date.items():
        last, _ = latest_mark(entries, WORKS_MARKS)
        if not is_warning(last):
            continue
        day, leg = split_day_key(key)
        if day >= today:
            out.append(WorksDay(day, None if leg == WHOLE_JOURNEY else int(leg)))
    return tuple(sorted(out, key=lambda w: (w.service_date, w.leg_index is not None, w.leg_index)))


def moved(
    profiles: Sequence[Profile],
    baselines: Mapping[str, BaselineHistory],
    *,
    today: date | None = None,
) -> tuple[Moved, ...]:
    """Directions whose newest confirmed baseline starts on another first train than the file."""
    out = []
    for p in profiles:
        for d in Direction:
            hist = baselines.get(direction_key(p.id, d))
            confirmed = [v for v in (hist.versions if hist else ()) if v.confirmed]
            if not confirmed:
                continue
            if p.journey(d).first_train_by_weekday:
                active = p.days | {
                    day.isoweekday() for day in p.extra_dates if today is None or day >= today
                }
                fragments = []
                for weekday in sorted(active):
                    candidates = [
                        version for version in confirmed if version.commute_weekday == weekday
                    ]
                    if not candidates:
                        continue
                    version = max(candidates, key=lambda x: (x.effective_from, x.version))
                    commute = date(2026, 10, 26) + timedelta(days=weekday - 1)
                    mine = p.journey_on(d, commute).first_train
                    adopted = version.signature.legs[0].departure
                    if adopted != mine:
                        fragments.append(
                            f"{commute:%A}: NS moved your {mine:%H:%M} to {adopted:%H:%M} from {version.effective_from.isoformat()}"
                        )
                if fragments:
                    out.append(
                        Moved(
                            p.id,
                            d,
                            f"{p.name}.{d.value}: "
                            + "; ".join(fragments)
                            + "; update your profile?",
                        )
                    )
                continue
            confirmed = [version for version in confirmed if version.commute_weekday is None]
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


def _shown_profiles(st: PersistedState) -> list[Profile]:
    """The current profiles, then each removed one that still runs on its occurrences (rule 10)."""
    cfg = st.config
    profiles = list(cfg.profiles if cfg else ())
    running = [s for s in st.occurrences.values() if find_profile(cfg, s.key.profile_id) is None]
    for pid in sorted({s.key.profile_id for s in running}):
        newest = max(
            (s for s in running if s.key.profile_id == pid), key=lambda s: s.key.service_date
        )
        p = profile_for(st, newest)
        if p is not None:
            profiles.append(p)
    return profiles


def _direction_infos(st: PersistedState, profiles: Sequence[Profile]) -> tuple[DirectionInfo, ...]:
    directions = []
    for p in profiles:
        for d in Direction:
            routes = [
                find_profile(st.config_snapshots.get(s.route_revision), p.id)
                for s in st.occurrences.values()
                if s.key.profile_id == p.id and s.key.direction is d
            ]
            n = max(len(r.journey(d).transfers) for r in (p, *routes) if r is not None)
            directions.append(DirectionInfo(p.id, p.name, d, n))
    return tuple(directions)


def runtime_view(
    st: PersistedState,
    boards: Mapping[str, Sequence[ProviderAnswer]],
    now: datetime,
    *,
    started: bool,
    refusal: str | None,
    store_ok: bool,
    key_rejected: bool,
    undelivered: int,
    requests: int,
    location_epochs: Mapping[str, int],
    positions: Mapping[str, tuple[float, float]],
) -> RuntimeView:
    """The frozen snapshot of `st` and what the runtime remembers beside it (5.4)."""
    cfg = st.config
    occurrences = []
    for k, s in sorted(st.occurrences.items()):
        ctx = occurrence_ctx(st, s, now)
        if ctx is not None:
            occurrences.append(occurrence_view(s, ctx, boards.get(k, ())))
    profiles = _shown_profiles(st)
    today = now.astimezone(TZ).date()
    last_ok: dict[str, datetime | None] = {}
    for p in cfg.profiles if cfg else ():
        for d in Direction:
            k = direction_key(p.id, d)
            led = st.lookahead.get(k)
            last_ok[k] = led.last_run if led else None
    return RuntimeView(
        at=now,
        started=started,
        # deep copies: a profile's mappings (`transfer_min`, zones) stay the runtime's own
        profiles=copy.deepcopy(tuple(profiles)),
        directions=_direction_infos(st, profiles),
        occurrences=tuple(occurrences),
        works={k: works_ahead(v, today) for k, v in st.lookahead.items()},
        moved=moved(cfg.profiles if cfg else (), st.baselines, today=today),
        refusal=refusal,
        store_ok=store_ok,
        key_rejected=key_rejected,
        undelivered=undelivered,
        requests=requests,
        location_epochs=dict(location_epochs),
        lookahead_last_ok=last_ok,
        positions=dict(positions),
    )
