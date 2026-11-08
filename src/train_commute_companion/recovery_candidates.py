"""Use only unambiguous recovery evidence from the newest provider answer cohort."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from train_commute_companion.calendar import TZ, to_utc
from train_commute_companion.health import FRESH_FOR
from train_commute_companion.model import Ctx, Leg
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.recovery import RecoveryError, RecoveryItinerary, from_connection


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    checked_at: datetime
    itinerary: RecoveryItinerary
    source_query: Query


def recovery_candidates(
    answers: Sequence[ProviderAnswer], ctx: Ctx, *, future_first: bool = True
) -> tuple[RecoveryCandidate, ...]:
    if not answers:
        return ()
    newest = max(to_utc(answer.fetched_at) for answer in answers)
    current = [answer for answer in answers if to_utc(answer.fetched_at) == newest]
    # shortcut: use only consensus from the newest answers until per-service reconciliation exists.
    common = set(current[0].connections)
    for answer in current[1:]:
        common.intersection_update(answer.connections)

    def service(leg: Leg) -> tuple[object, ...]:
        departure = leg.board.planned_departure
        key: tuple[object, ...] = (
            leg.transport_mode,
            leg.operator,
            leg.service_reference,
            None if departure is None else departure.astimezone(TZ).date(),
        )
        if leg.transport_mode == "WALK" or leg.service_reference is None:
            key += (
                leg.board.code,
                leg.alight.code,
                None if departure is None else to_utc(departure),
            )
        return key

    seen: dict[tuple[object, ...], Leg] = {}
    ambiguous: set[tuple[object, ...]] = set()
    for answer in current:
        for connection in answer.connections:
            for leg in connection.legs:
                if len(leg.stops) < 2:
                    continue
                key = service(leg)
                if key in seen and seen[key] != leg:
                    ambiguous.add(key)
                seen[key] = leg
    plans: dict[RecoveryItinerary, RecoveryCandidate] = {}
    for answer in current:
        if answer.query.require_accessible != ctx.profile.accessibility.require_accessible:
            return ()
        if not timedelta() <= to_utc(ctx.now) - to_utc(answer.fetched_at) < FRESH_FOR:
            continue
        for connection in answer.connections:
            if (
                connection not in common
                or connection.is_train_only
                or any(len(leg.stops) < 2 or service(leg) in ambiguous for leg in connection.legs)
            ):
                continue
            try:
                itinerary = from_connection(connection, ctx.profile.transfer_min)
            except RecoveryError:
                continue
            if itinerary.steps[-1].destination.station_code != ctx.profile.destination(
                ctx.direction
            ):
                continue
            if future_first and (
                not to_utc(ctx.now)
                <= to_utc(itinerary.steps[0].departure)
                < to_utc(ctx.schedule.window_end)
            ):
                continue
            plans.setdefault(
                itinerary,
                RecoveryCandidate(answer.fetched_at, itinerary, answer.query),
            )
    ordered = tuple(sorted(plans.values(), key=lambda p: to_utc(p.itinerary.steps[0].departure)))
    return ordered[:3] if future_first else ordered
