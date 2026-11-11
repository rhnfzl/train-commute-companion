"""Resume an explicitly skipped journey without restarting completed travel."""

from dataclasses import replace
from datetime import date, datetime, timedelta

from train_commute_companion.calendar import TZ, is_commute_day, to_utc
from train_commute_companion.lifecycle import whole_day_keys
from train_commute_companion.messages import Refused, no_journey
from train_commute_companion.model import Ctx, Direction, DirectionState, okey
from train_commute_companion.revisions import (
    find_profile,
    occurrence_ctx,
    signature_for,
)
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.store import PersistedState


def resume(
    stored: PersistedState,
    profile_id: str,
    service_date: date | None,
    direction: Direction | None,
    whole_day: bool,
    now: datetime,
) -> PersistedState | Refused:
    profile = find_profile(stored.config, profile_id)
    if profile is None:
        return no_journey(profile_id)
    today, instant = now.astimezone(TZ).date(), to_utc(now)

    def eligible(ctx: Ctx) -> bool:
        key = ctx.schedule.key
        day = key.service_date - timedelta(days=ctx.profile.journey(ctx.direction).day_offset)
        return (
            direction in (None, ctx.direction)
            and service_date in (None, key.service_date)
            and not profile.paused_on(key.service_date)
            and instant < to_utc(ctx.schedule.window_end)
            and (day == today or instant >= to_utc(ctx.schedule.window_start))
        )

    contexts: list[Ctx] = []
    for existing in stored.occurrences.values():
        if existing.key.profile_id == profile_id:
            ctx = occurrence_ctx(stored, existing, now)
            if ctx is not None and eligible(ctx):
                contexts.append(ctx)
    for d in Direction:
        if direction is not None and d is not direction:
            continue
        for back in (-1, 0, 1, 2):
            day = today - timedelta(days=back)
            if not is_commute_day(
                day, profile.days, profile.extra_dates, profile.skip_public_holidays
            ):
                continue
            sd = day + timedelta(days=profile.journey(d).day_offset)
            if service_date not in (None, sd) or profile.paused_on(sd):
                continue
            signature = signature_for(stored, profile, d, sd)
            first = signature.legs[0].departure if signature else None
            schedule = dated_schedule(profile, d, day, first)
            if okey(schedule.key) in stored.occurrences:
                continue
            ctx = Ctx(profile, d, schedule, signature, now)
            if eligible(ctx):
                contexts.append(ctx)
    if not contexts:
        return no_journey(profile.name)
    anchor = min(contexts, key=lambda ctx: to_utc(ctx.schedule.window_start))
    keys = (
        whole_day_keys(anchor.profile, anchor.schedule.key) if whole_day else (anchor.schedule.key,)
    )
    occurrences = dict(stored.occurrences)
    for key in keys:
        state = occurrences.get(okey(key))
        if state is None or not state.not_travelling:
            continue
        ctx = occurrence_ctx(stored, state, now)
        if ctx is None or instant >= to_utc(ctx.schedule.window_end):
            continue
        record = stored.journey_records.get(okey(key))
        if record is not None and record.arrived_at is not None:
            continue
        previous = state.skipped_direction_state
        if previous is None:
            return Refused(
                "This older skip has no saved journey state and cannot be resumed safely."
            )
        if previous in (DirectionState.DONE, DirectionState.EXPIRED):
            continue
        occurrences[okey(key)] = replace(
            state,
            not_travelling=False,
            skipped_direction_state=None,
            direction_state=previous,
            generation=state.generation + 1,
        )
    return replace(stored, occurrences=occurrences)
