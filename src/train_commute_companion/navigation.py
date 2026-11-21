"""Map links use only the public coordinates in the NS station catalogue."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from urllib.parse import urlencode

from train_commute_companion.advice import still_advised, usual_arrival
from train_commute_companion.calendar import to_utc
from train_commute_companion.health import FRESH_FOR
from train_commute_companion.lifecycle import is_resolved, merged_pool, observe, origin_answer
from train_commute_companion.model import (
    AlertEvent,
    Ctx,
    EventKind,
    LegKind,
    LegState,
    Observation,
    alert_facts,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.recovery_state import RecoveryProgress, RecoverySourcePhase
from train_commute_companion.state import OccurrenceState

MAP_LABELS = ("Apple Maps", "Google Maps")


def boarding_station(
    state: OccurrenceState,
    answers: tuple[ProviderAnswer, ...],
    now: datetime,
    event: AlertEvent | None,
    context: Ctx | None,
) -> str:
    if state.not_travelling:
        return ""
    now = to_utc(now)
    selected = state.selected_recovery
    facts = alert_facts(event) if event else None
    if selected is not None and selected.status == "active":
        if (
            selected.progress_phase
            not in (RecoveryProgress.WAITING_TO_BOARD, RecoveryProgress.WALKING)
            or selected.source_phase is not RecoverySourcePhase.CURRENT
            or selected.monitoring_error is not None
            or not timedelta() <= now - to_utc(selected.checked_at) < FRESH_FOR
            or (
                facts is not None
                and (facts.recovery_selection, facts.recovery_revision)
                != (selected.id, selected.revision)
            )
        ):
            return ""
        step = next(
            (x for x in selected.itinerary.steps[selected.index :] if x.mode != "WALK"), None
        )
        if step is not None and step.mode == "TRAIN" and now < to_utc(step.departure):
            return step.origin.station_code
        return ""

    if is_resolved(state) or not answers or (facts is not None and facts.recovery_selection):
        return ""
    if event is not None and event.kind is EventKind.LEAVE_NOW:
        if context is None or event.train is None:
            return ""
        origin = origin_answer(answers, context, state)
        if origin is None or not timedelta() <= now - to_utc(origin.fetched_at) < FRESH_FOR:
            return ""
        advised = still_advised(
            state, context, origin, event.train, usual_arrival(state, context, origin)
        )
        return advised.legs[0].board.code if advised is not None else ""

    if state.lock is None:
        return ""
    first = next((x for x in state.lock.legs if x.state is not LegState.MISSED), None)
    if (
        first is None
        or first.state not in (LegState.PENDING, LegState.BOARDING)
        or (event is not None and event.train != first.train)
    ):
        return ""
    leg, observation, seen = observe(merged_pool(answers), first)
    if (
        leg is None
        or observation is not Observation.SEEN
        or seen is None
        or leg.kind in (LegKind.CANCELLED, LegKind.CUT_SHORT)
        or not timedelta() <= now - to_utc(seen) < FRESH_FOR
    ):
        return ""
    obs = leg.leg.board.departure
    departure = obs.value if obs is not None else None
    return first.board_station if now < to_utc(departure or first.train.planned_departure) else ""


def station_links(
    positions: Mapping[str, tuple[float, float]], station: str
) -> tuple[str, str] | None:
    if station not in positions:
        return None
    lat, lon = positions[station]
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0, 0):
        return None
    point = f"{lat},{lon}"
    return (
        "https://maps.apple.com/place?" + urlencode({"coordinate": point}),
        "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": point}),
    )
