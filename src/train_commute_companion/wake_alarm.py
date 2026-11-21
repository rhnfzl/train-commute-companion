"""Select a confirmed earlier departure before changing a traveller's wake time."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from train_commute_companion.calendar import TZ, floor_to_minute, resolve_local, to_utc
from train_commute_companion.classify import current_connection, evidence, expected_departure
from train_commute_companion.health import FRESH_FOR
from train_commute_companion.lookahead_probe import WORKS_MARKS, is_warning, latest_mark
from train_commute_companion.match import planned_departure
from train_commute_companion.model import (
    Connection,
    Ctx,
    Direction,
    Signature,
    TrainId,
    direction_key,
    okey,
    present,
)
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.replacement import Catchability, check_good, max_transfers
from train_commute_companion.revisions import baseline_on, commute_day_for, travel_replaces
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.store import PersistedState
from train_commute_companion.transfer import is_reported

WakeReason = Literal["cancelled", "delayed", "timetable"]
WAKE_GRACE = timedelta(minutes=2)
WAKE_CHECK_INTERVAL = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class WakeSampleKey:
    profile_id: str
    day: date
    usual: Query
    signature: Signature | None
    normal_wake: datetime
    start: datetime
    buffer_min: int
    transfers: tuple[str, ...]
    transfer_min: tuple[tuple[str, int], ...]
    threshold_min: int
    saving_min: int


@dataclass(frozen=True, slots=True)
class WakeSlot:
    context: Ctx
    normal_wake: datetime
    evening_warning: bool

    @property
    def key(self) -> tuple[str, date]:
        return self.context.profile.id, self.context.schedule.key.service_date

    @property
    def sample_key(self) -> WakeSampleKey:
        p, d, s = self.context.profile, self.context.direction, self.context.schedule
        journey = p.journey_on(d, commute_day_for(p, d, s.key.service_date))
        transfers = journey.transfers
        query = Query.for_profile(
            p, p.origin(d), p.destination(d), s.first_departure, transfers[0] if transfers else None
        )
        return WakeSampleKey(
            *self.key,
            query,
            self.context.signature,
            self.normal_wake,
            to_utc(s.window_start),
            journey.buffer_min,
            transfers,
            tuple(sorted(p.transfer_min.items())),
            p.alerts.earlier_train_threshold_min,
            p.alerts.earlier_train_min_saving_min,
        )


def wake_slots(state: PersistedState, now: datetime) -> list[WakeSlot]:
    local = now.astimezone(TZ)
    stamp = to_utc(now)
    direction = Direction.OUTBOUND
    slots = []
    for profile in state.config.profiles if state.config is not None else ():
        clock = profile.alerts.wake_at
        if clock is None:
            continue
        ledger = state.lookahead.get(direction_key(profile.id, direction))
        history = state.baselines.get(direction_key(profile.id, direction))
        for day in (local.date(), local.date() + timedelta(days=1)):
            if (
                not profile.commutes_on(day)
                or profile.paused_on(day)
                or state.wake_alarm_dates.get(profile.id) == day
                or travel_replaces(state, profile, direction, day)
            ):
                continue
            base = None if history is None else baseline_on(history, profile, direction, day)
            signature = None if base is None or not base.confirmed else base.signature
            schedule = dated_schedule(profile, direction, day, None)
            if schedule.mode != "fixed":
                continue
            running = state.occurrences.get(okey(schedule.key))
            if running is not None and (running.not_travelling or running.travelling):
                continue
            first = schedule.first_departure
            wake_day = day if clock < first.astimezone(TZ).time() else day - timedelta(days=1)
            normal = to_utc(resolve_local(wake_day, clock, edge="start"))
            hinted = bool(
                ledger is not None
                and ledger.last_evening is not None
                and ledger.last_evening.astimezone(TZ).date() == day - timedelta(days=1)
                and any(
                    key.startswith(f"{day.isoformat()}|")
                    and is_warning(latest_mark(marks, WORKS_MARKS)[0])
                    for key, marks in ledger.per_date.items()
                )
            )
            earliest = normal - (to_utc(first) - to_utc(schedule.window_start))
            warmup = WAKE_CHECK_INTERVAL * (2 if hinted else 1)
            if earliest - warmup <= stamp < normal:
                slots.append(
                    WakeSlot(Ctx(profile, direction, schedule, signature, now), normal, hinted)
                )
    return sorted(slots, key=lambda slot: (not slot.evening_warning, slot.normal_wake))


@dataclass(frozen=True, slots=True)
class WakeCandidate:
    trains: tuple[TrainId, ...]
    at: datetime
    departure: datetime
    catch_by: datetime
    reason: WakeReason


@dataclass(frozen=True, slots=True)
class WakeEvidence:
    candidate: WakeCandidate
    checked_at: datetime
    confirmed: bool = False

    def ready(self, now: datetime) -> bool:
        stamp = to_utc(now)
        return (
            self.confirmed
            and timedelta() <= stamp - to_utc(self.checked_at) < FRESH_FOR
            and self.candidate.at <= stamp < self.candidate.at + WAKE_GRACE
            and stamp <= self.candidate.catch_by
        )


def observe_wake(
    previous: WakeEvidence | None, candidate: WakeCandidate | None, at: datetime
) -> WakeEvidence | None:
    if candidate is None:
        return None
    if previous is None or previous.candidate != candidate:
        return WakeEvidence(candidate, at)
    gap = to_utc(at) - to_utc(previous.checked_at)
    if gap <= timedelta():
        return previous
    return WakeEvidence(candidate, at, confirmed=gap < FRESH_FOR)


def _cancelled(c: Connection) -> bool:
    return c.status.upper() == "CANCELLED" or any(
        leg.cancelled or leg.board.cancelled or leg.alight.cancelled for leg in c.legs
    )


def _usual_problem(
    candidates: list[Connection], threshold: timedelta
) -> tuple[WakeReason, datetime | None] | None:
    if not candidates:
        return None
    stopped = [_cancelled(c) for c in candidates]
    if all(stopped):
        return "cancelled", None
    if any(stopped):
        return None
    arrivals = []
    for c in candidates:
        stop = c.legs[-1].alight
        if stop.planned_arrival is None or not is_reported(stop.arrival):
            return None
        arrival = to_utc(present(present(stop.arrival).value))
        if arrival - to_utc(stop.planned_arrival) < threshold:
            return None
        arrivals.append(arrival)
    return "delayed", min(arrivals)


def usual_problem(ctx: Ctx, *boards: ProviderAnswer) -> tuple[WakeReason, datetime | None] | None:
    profile, direction = ctx.profile, ctx.direction
    matches = [
        c
        for board in boards
        for c in board.connections
        if c.origin == profile.origin(direction)
        and c.destination == profile.destination(direction)
        and floor_to_minute(planned_departure(c.legs[0]))
        == floor_to_minute(ctx.schedule.first_departure)
    ]
    if not matches and _adopted_departure(ctx) is not None:
        return "timetable", None
    return _usual_problem(matches, timedelta(minutes=profile.alerts.earlier_train_threshold_min))


def _adopted_departure(ctx: Ctx) -> datetime | None:
    if ctx.signature is None or not ctx.signature.legs or ctx.signature.legs[0].day_offset != 0:
        return None
    adopted = resolve_local(
        ctx.schedule.key.service_date, ctx.signature.legs[0].departure, edge="start"
    )
    return to_utc(adopted) if to_utc(adopted) < to_utc(ctx.schedule.first_departure) else None


def earlier_wake(
    ctx: Ctx, usual: ProviderAnswer, earlier: ProviderAnswer, scheduled_wake: datetime
) -> WakeCandidate | None:
    """Change the alarm only for dated, affirmative NS evidence and a viable earlier ride."""
    profile, direction, schedule = ctx.profile, ctx.direction, ctx.schedule
    plan = profile.journey_on(
        direction, commute_day_for(profile, direction, schedule.key.service_date)
    )
    now, planned, normal = map(to_utc, (ctx.now, schedule.first_departure, scheduled_wake))
    buffer = timedelta(minutes=plan.buffer_min)
    if (
        schedule.mode != "fixed"
        or not now < normal < planned
        or any(
            not timedelta() <= now - to_utc(board.fetched_at) < FRESH_FOR
            for board in (usual, earlier)
        )
    ):
        return None
    origin, destination = profile.origin(direction), profile.destination(direction)
    problem = usual_problem(ctx, usual, earlier)
    if problem is None:
        return None
    reason, usual_arrival = problem
    adopted = _adopted_departure(ctx) if reason == "timetable" else None
    lead = max(buffer, planned - normal)
    saving = timedelta(minutes=profile.alerts.earlier_train_min_saving_min)
    available = []
    cancelled_stops = {
        (leg.train, stop.code)
        for board in (usual, earlier)
        for connection in board.connections
        for leg in connection.legs
        for stop in leg.stops
        if leg.cancelled or stop.cancelled
    }
    contradicted = {
        tuple(g.train for g in c.legs)
        for board in (usual, earlier)
        for c in board.connections
        if _cancelled(c) or any(g.reachable is False for g in c.legs)
    }
    for c in earlier.connections:
        current = current_connection(evidence(earlier), c)
        dep = expected_departure(current)
        wake = dep - lead
        terminal = current.legs[-1].alight.arrival
        trains = tuple(g.train for g in current.legs)
        if (
            dep.astimezone(TZ).date() != schedule.key.service_date
            or (
                adopted is not None
                and floor_to_minute(planned_departure(c.legs[0])) != floor_to_minute(adopted)
            )
            or dep < to_utc(schedule.window_start)
            or dep >= planned
            or wake >= normal
            or now >= wake + WAKE_GRACE
            or trains in contradicted
            or any(
                (leg.train, stop.code) in cancelled_stops
                for leg in current.legs
                for stop in (leg.board, leg.alight)
            )
            or _cancelled(current)
            or any(leg.reachable is False for leg in current.legs)
            or not is_reported(terminal)
            or (
                usual_arrival is not None
                and to_utc(present(present(terminal).value)) + saving > usual_arrival
            )
            or not check_good(
                c,
                earlier,
                board_station=origin,
                destination=destination,
                catch=Catchability(now + buffer),
                kept_incoming=None,
                done_transfers=0,
                max_transfers=max_transfers(plan.transfers),
                profile_min=profile.transfer_min,
            ).ok
        ):
            continue
        available.append(WakeCandidate(trains, wake, dep, dep - buffer, reason))
    return max(available, key=lambda option: option.departure, default=None)
