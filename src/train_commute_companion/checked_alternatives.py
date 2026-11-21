"""Fresh NS evidence for the normal train alternatives named in alerts."""

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from train_commute_companion.advice import leave_now_text, still_advised, usual_arrival
from train_commute_companion.budget import POLL_EVERY_S, RequestGate
from train_commute_companion.calendar import TZ, hhmm, to_utc
from train_commute_companion.classify import classify_leg, current_connection, expected_departure
from train_commute_companion.health import FRESH_FOR
from train_commute_companion.lifecycle import (
    is_resolved,
    merged_pool,
    option_fits_plan,
    option_rules,
    origin_answer,
    progress_route,
)
from train_commute_companion.messages import crowd_text, leave_by_text, status_text
from train_commute_companion.model import (
    FOLLOW_LIMIT,
    AlertEvent,
    Alternative,
    Connection,
    Ctx,
    EventKind,
    Leg,
    LegKind,
    LegState,
    OccurrenceKey,
    alert_facts,
    connection_route,
    display_name,
)
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.replacement import GoodKwargs, check_good
from train_commute_companion.runtime_state import OccurrenceRequest
from train_commute_companion.state import OccurrenceState
from train_commute_companion.transfer import is_reported

CHECK_FRESH_FOR = timedelta(seconds=POLL_EVERY_S)
CHECK_DEADLINE = timedelta(seconds=10)


def _fresh(now: datetime, then: datetime, limit: timedelta) -> bool:
    return timedelta() <= to_utc(now) - to_utc(then) < limit


@dataclass(frozen=True, slots=True)
class Check:
    state: OccurrenceState
    ctx: Ctx
    option: Alternative
    boards: tuple[ProviderAnswer, ...]
    rules: GoodKwargs
    day: date

    @property
    def query(self) -> Query | None:
        if not self.option.route:
            return None
        departure = self.option.route[0][0].planned_departure
        return Query.for_profile(
            self.ctx.profile, self.option.station, self.rules["destination"], departure
        )

    @property
    def scope(self) -> tuple[OccurrenceKey, str, str]:
        return self.state.key, self.option.station, self.rules["destination"]

    def same_revision(self, other: "Check") -> bool:
        return (
            self.state.route_revision == other.state.route_revision
            and self.state.settings_revision == other.state.settings_revision
            and self.state.baseline_version == other.state.baseline_version
        )

    def same_state(self, other: "Check") -> bool:
        return (
            self.same_revision(other)
            and self.boards is other.boards
            and OccurrenceRequest.capture(self.state) == OccurrenceRequest.capture(other.state)
            and self.option == other.option
        )

    def eligible(self) -> bool:
        pool = merged_pool(self.boards)
        return any(
            connection_route(c) == self.option.route
            and _normal(c)
            and check_good(c, pool, **self.rules).ok
            and option_fits_plan(c, self.ctx, self.option, pool)
            for c in pool.connections
        )

    def confirmed(self, answer: ProviderAnswer, *, advice: bool) -> Connection | None:
        """Check a whole advertised itinerary, then retain the usual policy on its fresh times."""
        exact = [c for c in answer.connections if connection_route(c) == self.option.route]
        if (
            not exact
            or len({_times(c) for c in exact}) != 1
            or not all(_normal(c) and check_good(c, answer, **self.rules).ok for c in exact)
        ):
            return None
        # The suffix request cannot see the train the user is changing from. Its last
        # observation and transfer rules still apply, along with the fresh suffix's own data.
        pool = merged_pool((answer, *self.boards))
        c = exact[0]
        if not check_good(c, pool, **self.rules).ok:
            return None
        if not option_fits_plan(c, self.ctx, self.option, answer):
            return None
        if advice:
            origin = origin_answer(pool.answers, self.ctx, self.state)
            if origin is None:
                return None
            still = still_advised(
                self.state,
                self.ctx,
                origin,
                c.legs[0].train,
                usual_arrival(self.state, self.ctx, origin),
            )
            if still is None or connection_route(still) != self.option.route:
                return None
        current = current_connection((answer,), c)
        legs = [
            replace(
                leg,
                stops=(
                    replace(leg.board, actual_track=actual, planned_track=planned),
                    *leg.stops[1:],
                ),
            )
            for leg, (actual, planned) in zip(
                current.legs, _platforms(current, (answer,)), strict=True
            )
        ]
        return replace(current, legs=tuple(legs))

    def cancellations(self, answers: Sequence[ProviderAnswer]) -> str | None:
        station, destination = self.option.station, self.rules["destination"]
        direct = [
            a for a in answers if (a.query.origin, a.query.destination) == (station, destination)
        ]
        if not direct:
            return None
        latest = max(to_utc(a.fetched_at) for a in direct)
        if not _fresh(self.ctx.now, latest, CHECK_FRESH_FOR):
            return None
        candidates = [
            c
            for a in direct
            if to_utc(a.fetched_at) == latest
            for c in a.connections
            if c.is_train_only
            and (c.origin, c.destination) == (station, destination)
            and c.legs[0].train.planned_departure.astimezone(TZ).date() == self.day
            and expected_departure(c) >= self.rules["catch"].not_before
        ]
        if not candidates or not all(_cancelled(c) for c in candidates):
            return None
        return (
            f"NS shows cancellations on every train option checked from {station} "
            f"to {destination} for {self.day.isoformat()}. Check the NS app for other options."
        )


def _cancelled(c: Connection) -> bool:
    return c.status == "CANCELLED" or any(
        g.cancelled or g.board.cancelled or g.alight.cancelled for g in c.legs
    )


def _normal(c: Connection) -> bool:
    return (
        c.status == "NORMAL"
        and c.is_train_only
        and not _cancelled(c)
        and all(
            g.reachable is not False
            and g.train.train_number
            and classify_leg(g, g.alight.code).kind in (LegKind.OK, LegKind.DELAYED)
            and is_reported(g.board.departure)
            and is_reported(g.alight.arrival)
            for g in c.legs
        )
    )


def option_check(
    event: AlertEvent,
    state: OccurrenceState,
    ctx: Ctx,
    boards: tuple[ProviderAnswer, ...],
) -> Check | None:
    facts = alert_facts(event)
    option = facts.alternative
    if option is None or is_resolved(state) or state.not_travelling or not boards:
        return None
    newest = max(to_utc(a.fetched_at) for a in boards)
    if not _fresh(ctx.now, newest, FRESH_FOR):
        return None
    if state.poll_records:
        poll = state.poll_records[-1]
        if not poll.ok and to_utc(poll.at) >= newest:
            return None
    rules = option_rules(state, ctx, option, merged_pool(boards))
    if rules is None:
        return None
    if (
        option.route
        and event.kind is not EventKind.LEAVE_NOW
        and option.style in ("connection", "catch", "station", "onward")
    ):
        if facts.on:
            if not any(
                progress_route(j.legs[: len(option.prior)]) == option.prior
                and any(x.train == event.train for x in j.legs)
                for j in state.retained
            ):
                return None
        else:
            if state.lock is None:
                return None
            ahead = state.lock.legs[len(option.prior) :]
            if (
                progress_route(state.lock.legs) != option.prior + option.route
                or not ahead
                or ahead[0].state not in (LegState.PENDING, LegState.BOARDING)
            ):
                return None
    day = ctx.schedule.first_departure.astimezone(TZ).date()
    expected = None
    if option.prior:
        kept = rules["kept_incoming"]
        arrival = None if kept is None else kept.leg.alight.arrival
        expected = None if arrival is None else arrival.value
        if expected is None:
            return None
        day = expected.astimezone(TZ).date()
    if option.route:
        departure = option.route[0][0].planned_departure
        candidate_day = departure.astimezone(TZ).date()
        if option.prior:
            if expected is None or to_utc(departure) > to_utc(expected) + FOLLOW_LIMIT:
                return None
            if candidate_day < day:
                return None
            day = candidate_day
        elif candidate_day != day:
            start, end = ctx.schedule.first_departure, ctx.schedule.last_departure
            if end is None or not to_utc(start) <= to_utc(departure) <= to_utc(end):
                return None
            day = candidate_day
    return Check(state, ctx, option, boards, rules, day)


@dataclass(frozen=True, slots=True)
class Sample:
    check: Check
    attempted_at: datetime
    answer: ProviderAnswer | None


class CheckedAlternatives:
    def __init__(self, gate: RequestGate) -> None:
        self._gate = gate
        self._samples: dict[tuple[OccurrenceKey, str, str], Sample] = {}
        self._lock = asyncio.Lock()

    async def prepare(self, check: Check, current: Callable[[], Check | None]) -> bool:
        async with self._lock:
            latest = current()
            if latest is None or not check.same_state(latest):
                return False
            check = latest
            now, query = check.ctx.now, check.query
            self._samples = {
                scope: sample
                for scope, sample in self._samples.items()
                if _fresh(now, sample.attempted_at, CHECK_FRESH_FOR)
            }
            if (
                query is None
                or check.scope in self._samples
                or not self._gate.budget.allow(now, live=False)
                or not check.eligible()
            ):
                return False
            answer = await self._gate.trips(query, False, None, to_utc(now) + CHECK_DEADLINE)
            got = answer if isinstance(answer, ProviderAnswer) else None
            latest = current()
            if got is not None and (
                latest is None
                or not check.same_state(latest)
                or got.query != query
                or to_utc(got.query.at) != to_utc(query.at)
                or to_utc(got.fetched_at) < to_utc(now)
            ):
                got = None
            self._samples[check.scope] = Sample(check, now, got)
            return answer == "auth"

    def answer(self, check: Check) -> ProviderAnswer | None:
        sample = self._samples.get(check.scope)
        if (
            sample is None
            or not check.option.route
            or not check.same_revision(sample.check)
            or check.option.route != sample.check.option.route
            or check.option.prior != sample.check.option.prior
            or sample.answer is None
            or not self._gate.budget.allow(check.ctx.now, live=False)
            or not _fresh(check.ctx.now, sample.answer.fetched_at, CHECK_FRESH_FOR)
        ):
            return None
        if check.boards is not sample.check.boards and not _agrees_with_board(check, sample.answer):
            return None
        return sample.answer


def _agrees_with_board(check: Check, answer: ProviderAnswer) -> bool:
    # A newer board may confirm the same operating facts. A changed route, time or platform
    # needs another check before it can be repeated.
    newer = [a for a in check.boards if to_utc(a.fetched_at) >= to_utc(answer.fetched_at)]
    if not newer:
        return True
    pool = merged_pool(newer)
    matches = [c for c in pool.connections if connection_route(c) == check.option.route]
    confirmed = [c for c in answer.connections if connection_route(c) == check.option.route]
    if not matches or not confirmed or not check.eligible():
        return False
    latest = current_connection(pool.answers, matches[0])
    checked = current_connection((answer,), confirmed[0])

    return (
        all(
            _normal(c) == _normal(checked) and _cancelled(c) == _cancelled(checked)
            for c in (*matches, latest)
        )
        and _times(latest) == _times(checked)
        and _platforms(latest, newer) == _platforms(checked, (answer,))
    )


def _platforms(
    c: Connection, answers: Sequence[ProviderAnswer]
) -> tuple[tuple[str | None, str | None], ...]:
    result = []
    for leg in c.legs:
        boards = [
            g.board
            for answer in answers
            for x in answer.connections
            for g in x.legs
            if g.train == leg.train and g.board.code == leg.board.code
        ]
        actual, planned = {x.actual_track for x in boards}, {x.planned_track for x in boards}
        result.append(
            (
                next(iter(actual)) if len(actual) == 1 else None,
                next(iter(planned)) if len(actual) == len(planned) == 1 else None,
            )
        )
    return tuple(result)


def _times(c: Connection) -> tuple[datetime | None, ...]:
    return tuple(
        to_utc(obs.value) if obs is not None and obs.value is not None else None
        for leg in c.legs
        for obs in (leg.board.departure, leg.alight.arrival)
    )


def _track(leg: Leg) -> str:
    if leg.board.actual_track:
        return f"platform {leg.board.actual_track}"
    if leg.board.planned_track:
        return f"scheduled platform {leg.board.planned_track}"
    return ""


def _check_note(now: datetime, fetched_at: datetime, track: str) -> str:
    minutes = (to_utc(now) - to_utc(fetched_at)) // timedelta(minutes=1)
    ago = "just now" if not minutes else f"{minutes} min ago"
    return f"NS checked {ago}: runs" + (f", {track}." if track else ".")


def checked_text(event: AlertEvent, check: Check, c: Connection, fetched_at: datetime) -> str:
    facts = alert_facts(event)
    state, ctx, option = check.state, check.ctx, check.option
    first = c.legs[0]
    buffer = ctx.profile.journey(ctx.direction).buffer_min if ctx.profile.alerts.leave_by else None
    note = _check_note(ctx.now, fetched_at, "" if option.style == "onward" else _track(first))
    if event.kind in (EventKind.ALL_GOOD, EventKind.AT_STATION):
        board = replace(first.board, actual_track=None, planned_track=None)
        late = classify_leg(first, first.alight.code).dep_delay_min
        legs = (replace(first, stops=(board, *first.stops[1:])), *c.legs[1:])
        prefix = "your "
        if event.kind is EventKind.AT_STATION:
            previous, sep, _ = event.body.partition(f"{hhmm(first.train.planned_departure)} to ")
            prefix = previous if sep else "at the station: your "
        leave = leave_by_text(first, buffer) if event.kind is EventKind.ALL_GOOD else ""
        return (
            f"{display_name(event.profile_name)}: {prefix}{status_text(legs, late)}."
            + (leave + "." if leave else "")
            + f" {note}{facts.twin_note}"
        )
    if event.kind is EventKind.LEAVE_NOW:
        body = leave_now_text(c, ctx.now.tzinfo, buffer)
        return f"{display_name(event.profile_name)}: {body}. {note}{facts.twin_note}"
    head = event.text.removesuffix(facts.twin_note).removesuffix(facts.next_clause).rstrip()
    if event.kind is EventKind.ROUTE_CHANGED and facts.next_clause.startswith(" via "):
        via = ", ".join(leg.alight.name or leg.alight.code for leg in c.legs[:-1])
        via = via or "a direct train"
        return f"{head} via {via}, following it. {note}{facts.twin_note}"
    if event.kind is EventKind.ROUTE_CHANGED and not head.endswith("."):
        head += "."
    dep = hhmm(expected_departure(c))
    if facts.next_clause.startswith(" Take the "):
        which = "onward train" if option.prior else "earlier train"
        leave = (
            leave_by_text(first, buffer)
            if not option.prior and not state.travelling and not state.at_station
            else ""
        )
        clause = f"Take the {which} at {dep} from {first.board.code}"
        clause += ("." + leave if leave else "") + crowd_text(c.legs) + "."
    elif option.style == "station":
        board = replace(first.board, actual_track=None, planned_track=None)
        first = replace(first, stops=(board, *first.stops[1:]))
        late = classify_leg(first, first.alight.code).dep_delay_min
        clause = f"Next: {status_text((first, *c.legs[1:]), late)}."
    elif option.style == "onward":
        current = hhmm(option.prior[-1][0].planned_departure)
        clause = f"Now following your {current} train"
        for leg in c.legs:
            departure = leg.board.departure
            when = hhmm(
                departure.value if departure and departure.value else leg.train.planned_departure
            )
            track = _track(leg)
            clause += f", then the {when} from {leg.board.code}" + (f" ({track})" if track else "")
        clause += crowd_text(c.legs) + "."
    elif option.style == "option":
        arr = c.legs[-1].alight.arrival
        arrival = None if arr is None else arr.value
        ending = (
            f", arrives {hhmm(arrival)}. Choose it to follow it"
            if event.train is not None and arrival is not None
            else ""
        )
        clause = f"Next option: {dep}{ending}."
    elif option.style == "conditional":
        clause = f"next good connection: {dep}."
    else:
        label = "Next you can catch" if option.style == "catch" else "Next good connection"
        via = ", ".join(c.transfer_stations) or "a direct train"
        route = f", new route via {via}" if option.moved_route else ""
        leave = (
            leave_by_text(first, buffer)
            if not option.prior and not state.travelling and not state.at_station
            else ""
        )
        clause = (
            f"{label}: {dep}{route}" + ("." + leave if leave else "") + crowd_text(c.legs) + "."
        )
    return f"{head} {clause} {note}{facts.twin_note}"


def option_clause(event: AlertEvent) -> str:
    facts = alert_facts(event)
    body = event.text.removesuffix(facts.twin_note)
    legacy = re.search(r" Next option: \d{2}:\d{2}(?:, arrives \d{2}:\d{2})?\..*$", body)
    return facts.next_clause or (legacy[0] if legacy else "")


def without_option(event: AlertEvent, cancellation: str | None) -> str:
    facts = alert_facts(event)
    clause = option_clause(event)
    head = event.text.removesuffix(facts.twin_note).removesuffix(clause).rstrip()
    detail = cancellation or "Check the NS app for a current train option."
    if event.kind in (EventKind.ALL_GOOD, EventKind.AT_STATION):
        return f"{display_name(event.profile_name)}: {detail}{facts.twin_note}"
    if event.kind is EventKind.ROUTE_CHANGED and clause.startswith(" via "):
        return f"{display_name(event.profile_name)}: NS reported a route change. {detail}{facts.twin_note}"
    if not clause and cancellation is None:
        return event.text
    if event.kind is EventKind.NO_CONNECTION:
        return f"{display_name(event.profile_name)}: {detail}{facts.twin_note}"
    if head.endswith(",") and cancellation is None:
        detail = detail[0].lower() + detail[1:]
    return f"{head} {detail}{facts.twin_note}"
