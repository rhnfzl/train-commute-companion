"""A private estimate for a partner using the train's reported arrival."""

from datetime import timedelta

from train_commute_companion.calendar import hhmm, to_utc
from train_commute_companion.catalogue import message
from train_commute_companion.health import BoardPhase
from train_commute_companion.model import Ctx, Direction, LegKind, LegState, TransferState
from train_commute_companion.state import OccurrenceState
from train_commute_companion.view import OccurrenceView

MIN_DELAY = timedelta(minutes=10)


def partner_text(ctx: Ctx, state: OccurrenceState, view: OccurrenceView) -> str | None:
    if (
        ctx.direction is not Direction.RETURN
        or ctx.profile.travel_plan
        or not state.travelling
        or state.not_travelling
        or view.resolved
        or view.selected_recovery is not None
        or view.health.phase is not BoardPhase.HEALTHY
    ):
        return None
    riding = next((i for i, leg in enumerate(view.legs) if leg.progress is LegState.RIDING), None)
    if riding is None:
        return None
    last = view.legs[-1]
    if (
        last.alight != ctx.profile.home_station
        or last.arrival is None
        or last.planned_arrival is None
        or to_utc(last.arrival) <= to_utc(ctx.now)
        or any(leg.state not in (LegKind.OK, LegKind.DELAYED) for leg in view.legs[riding:])
        or any(t.state is not TransferState.OK for t in view.transfers[riding:])
    ):
        return None
    delay = to_utc(last.arrival) - to_utc(last.planned_arrival)
    if delay < MIN_DELAY:
        return None
    eta = to_utc(last.arrival) + timedelta(minutes=ctx.schedule.walk_after_min)
    return message(
        "partner.eta",
        ctx.profile.alerts.language,
        minutes=int(delay / timedelta(minutes=1)),
        time=hhmm(eta),
    )
