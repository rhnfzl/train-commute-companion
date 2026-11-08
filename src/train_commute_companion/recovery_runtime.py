"""Refresh and persist deliberate recovery choices without changing the watched train journey."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

from train_commute_companion.budget import RequestGate
from train_commute_companion.calendar import to_utc
from train_commute_companion.health import FRESH_FOR
from train_commute_companion.messages import Refused
from train_commute_companion.model import Ctx, OccurrenceKey, okey
from train_commute_companion.recovery import RecoveryItinerary, RecoveryStep
from train_commute_companion.recovery_candidates import RecoveryCandidate, recovery_candidates
from train_commute_companion.recovery_lifecycle import (
    RecoveryActionError,
    apply_manual,
    select_recovery,
)
from train_commute_companion.recovery_state import (
    RecoveryAction,
    RecoveryActionKind,
    RecoveryOffer,
    RecoveryOfferPhase,
    RecoverySourcePhase,
    SelectedRecovery,
)
from train_commute_companion.state import OccurrenceState
from train_commute_companion.state_writer import StateWriter
from train_commute_companion.store import PersistedState
from train_commute_companion.view import RecoveryPlanView, RuntimeView

log = logging.getLogger(__name__)
_REFRESH_GAP = timedelta(seconds=60)


def _route(step: RecoveryStep) -> object:
    return (
        step.service_key
        if step.mode != "WALK"
        else (
            "WALK",
            step.origin.physical_key,
            step.destination.physical_key,
        )
    )


def _suffix(itinerary: RecoveryItinerary, index: int) -> RecoveryItinerary:
    return RecoveryItinerary(
        itinerary.steps[index:],
        tuple(
            replace(
                change,
                incoming_step=change.incoming_step - index,
                outgoing_step=change.outgoing_step - index,
                walk_steps=tuple(i - index for i in change.walk_steps),
            )
            for change in itinerary.transfers
            if change.incoming_step >= index
        ),
    )


class RecoveryController:
    def __init__(
        self,
        writer: StateWriter,
        gate: RequestGate,
        now: Callable[[], datetime],
        context: Callable[[OccurrenceState, datetime], Ctx | None],
        view: Callable[[], RuntimeView],
        changed: Callable[
            [PersistedState, OccurrenceState, SelectedRecovery | None, SelectedRecovery],
            PersistedState,
        ],
        deliver: Callable[[], Awaitable[None]],
    ) -> None:
        self._writer, self._gate, self._now, self._context, self._view = (
            writer,
            gate,
            now,
            context,
            view,
        )
        self._changed, self._deliver = changed, deliver
        self._offers: dict[str, tuple[RecoveryOffer, datetime]] = {}
        self._preparing: dict[str, object] = {}
        self._task: asyncio.Task[None] | None = None
        self._latest: dict[str, tuple[RecoveryCandidate, ...]] = {}

    def invalidate(self, key: OccurrenceKey | None = None) -> None:
        if key is None:
            self._offers.clear()
            self._preparing.clear()
            self._latest.clear()
        else:
            self._offers.pop(okey(key), None)
            self._preparing.pop(okey(key), None)
            self._latest.pop(okey(key), None)

    def offer_for(self, key: OccurrenceKey) -> RecoveryOffer | None:
        cached = self._offers.get(okey(key))
        if cached is None:
            return None
        offer = cached[0]
        state = self._writer.st.occurrences.get(okey(key))
        if (
            state is None
            or state.not_travelling
            or offer.phase_at(self._now(), state.generation, state.route_revision)
            is not RecoveryOfferPhase.CURRENT
        ):
            self.invalidate(key)
            return None
        return offer

    def _state(self, key: OccurrenceKey) -> tuple[OccurrenceState, Ctx] | Refused:
        state = self._writer.st.occurrences.get(okey(key))
        context = None if state is None else self._context(state, self._now())
        if state is None or context is None or state.not_travelling:
            return Refused("No recovery journey is available for this occurrence.")
        return state, context

    async def prepare(
        self, key: OccurrenceKey, proposal_id: str, origin_id: str
    ) -> RecoveryOffer | Refused:
        self._offers.pop(okey(key), None)
        self._preparing.pop(okey(key), None)
        found = self._state(key)
        if isinstance(found, Refused):
            return found
        state, context = found
        shown = next((o for o in self._view().occurrences if o.key == key), None)
        plan = (
            None
            if shown is None
            else next((p for p in shown.replacement_plans if p.proposal_id == proposal_id), None)
        )
        if plan is None or not plan.selectable or plan.source_query is None:
            return Refused("That recovery proposal changed. Refresh status and choose again.")
        origins = [
            i for i, step in enumerate(plan.itinerary.steps) if step.origin.physical_id == origin_id
        ]
        if len(origins) != 1:
            return Refused("That physical starting place is missing or ambiguous.")
        expected = _suffix(plan.itinerary, origins[0])
        confirmed_at = self._now()
        mark = (state.generation, state.route_revision)
        attempt = object()
        self._preparing[okey(key)] = attempt
        try:
            got = await self._gate.trips(plan.source_query, True, context.schedule.window_end)
        finally:
            current_attempt = self._preparing.get(okey(key)) is attempt
            if current_attempt:
                self._preparing.pop(okey(key), None)
        current = self._state(key)
        if (
            isinstance(current, Refused)
            or (current[0].generation, current[0].route_revision) != mark
            or not current_attempt
        ):
            return Refused("Your journey changed during the refresh. Choose again.")
        if isinstance(got, str) or got.query != plan.source_query:
            return Refused("NS could not refresh this recovery plan. No recovery was selected.")
        candidates = recovery_candidates((got,), current[1], future_first=False)
        matches = self._matching(candidates, expected)
        if len(matches) != 1:
            return Refused("NS no longer confirms that exact physical route. Check the NS planner.")
        refreshed = matches[0]
        offer = RecoveryOffer(
            proposal_id,
            key,
            mark[0],
            mark[1],
            refreshed,
            plan.source_query,
            got.fetched_at,
            self._now(),
        )
        try:
            # Validate all source and physical constraints before asking the user to approve.
            select_recovery(
                offer,
                selection_id="validation",
                fingerprint=offer.fingerprint,
                confirmed_origin=refreshed.steps[0].origin,
                confirmed_at=confirmed_at,
                now=self._now(),
                generation=mark[0],
                route_revision=mark[1],
                require_accessible=current[1].profile.accessibility.require_accessible,
                acknowledge_planned_times=True,
                acknowledge_accessibility_unknown=True,
            )
        except RecoveryActionError as error:
            return Refused(str(error))
        # A bound per occurrence; prune expired entries and cap unrelated occurrences too.
        for cached_key, (old, _) in tuple(self._offers.items()):
            if not timedelta() <= to_utc(self._now()) - to_utc(old.checked_at) < FRESH_FOR:
                self._offers.pop(cached_key, None)
        if len(self._offers) >= 64:
            self._offers.pop(next(iter(self._offers)))
        self._offers[okey(key)] = (offer, confirmed_at)
        return offer

    @staticmethod
    def _matching(
        candidates: Sequence[RecoveryCandidate], expected: RecoveryItinerary
    ) -> tuple[RecoveryItinerary, ...]:
        routes = tuple(_route(step) for step in expected.steps)
        matches = set()
        for candidate in candidates:
            for i, step in enumerate(candidate.itinerary.steps):
                if step.origin.physical_key != expected.steps[0].origin.physical_key:
                    continue
                tail = _suffix(candidate.itinerary, i)
                if tuple(_route(s) for s in tail.steps) == routes:
                    matches.add(tail)
        return tuple(matches)

    def _save(
        self, state: OccurrenceState, selected: SelectedRecovery
    ) -> SelectedRecovery | Refused:
        stored = self._writer.st
        updated = replace(state, selected_recovery=selected)
        candidate = replace(stored, occurrences={**stored.occurrences, okey(state.key): updated})
        candidate = self._changed(candidate, updated, state.selected_recovery, selected)
        if not self._writer.commit_strict(candidate):
            return Refused("Recovery change was not saved. Free storage and try this action again.")
        return selected

    def select(
        self,
        key: OccurrenceKey,
        offer_id: str,
        *,
        acknowledge_planned_times: bool,
        acknowledge_accessibility_unknown: bool,
    ) -> SelectedRecovery | Refused:
        offer = self.offer_for(key)
        found = self._state(key)
        if offer is None or offer.id != offer_id or isinstance(found, Refused):
            return Refused("That recovery offer expired or changed. Prepare it again.")
        state, context = found
        try:
            selected = select_recovery(
                offer,
                selection_id=uuid4().hex,
                fingerprint=offer.fingerprint,
                confirmed_origin=offer.itinerary.steps[0].origin,
                confirmed_at=self._offers[okey(key)][1],
                now=self._now(),
                generation=state.generation,
                route_revision=state.route_revision,
                require_accessible=context.profile.accessibility.require_accessible,
                acknowledge_planned_times=acknowledge_planned_times,
                acknowledge_accessibility_unknown=acknowledge_accessibility_unknown,
            )
        except RecoveryActionError as error:
            return Refused(str(error))
        result = self._save(state, selected)
        if isinstance(result, SelectedRecovery):
            self.invalidate(key)
        return result

    def confirm(
        self,
        key: OccurrenceKey,
        selection_id: str,
        revision: int,
        step_index: int,
        kind: str,
        place_id: str | None,
        action_id: str,
    ) -> SelectedRecovery | Refused:
        found = self._state(key)
        if isinstance(found, Refused):
            return found
        state, _ = found
        selected = state.selected_recovery
        if selected is None or selected.id != selection_id:
            return Refused("That recovery selection changed.")
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (revision, step_index)
        ):
            return Refused("Recovery revision and step index must be whole numbers.")
        old = next((a for a in selected.confirmations if a.id == action_id), None)
        if old is not None:
            if (
                old.revision == revision
                and old.step_index == step_index
                and old.kind == kind
                and (None if old.place is None else old.place.physical_id) == place_id
            ):
                return selected
            return Refused("That confirmation identity was already used for another action.")
        if kind not in ("boarded", "arrived", "walk_arrived", "stop"):
            return Refused("Unknown recovery action.")
        place = None
        if kind != "stop":
            if not 0 <= step_index < len(selected.itinerary.steps):
                return Refused("That recovery step changed.")
            step = selected.itinerary.steps[step_index]
            place = step.origin if kind == "boarded" else step.destination
            if place.physical_id != place_id:
                return Refused("Confirm the exact physical place shown for this step.")
        action = RecoveryAction(
            action_id,
            selection_id,
            revision,
            step_index,
            cast(RecoveryActionKind, kind),
            place,
            self._now(),
        )
        try:
            updated = apply_manual(selected, action, now=self._now())
        except RecoveryActionError as error:
            return Refused(str(error))
        return self._save(state, updated)

    def plans_for(self, key: OccurrenceKey) -> tuple[RecoveryPlanView, ...]:
        state = self._writer.st.occurrences.get(okey(key))
        if state is None or state.selected_recovery is None or not state.selected_recovery.active:
            return ()
        plans: dict[str, RecoveryPlanView] = {}
        for candidate in self._latest.get(okey(key), ()):
            if not timedelta() <= to_utc(self._now()) - to_utc(candidate.checked_at) < FRESH_FOR:
                continue
            for index, step in enumerate(candidate.itinerary.steps):
                if to_utc(step.departure) < to_utc(self._now()):
                    continue
                itinerary = _suffix(candidate.itinerary, index)
                plan = RecoveryPlanView(
                    candidate.checked_at,
                    itinerary,
                    candidate.source_query,
                    key,
                    state.generation,
                    state.route_revision,
                )
                if plan.selectable:
                    plans.setdefault(plan.proposal_id, plan)
        return tuple(plans.values())[:3]

    def refresh_soon(self) -> None:
        live = {
            key
            for key, state in self._writer.st.occurrences.items()
            if state.selected_recovery is not None and state.selected_recovery.active
        }
        self._latest = {key: value for key, value in self._latest.items() if key in live}
        if not live:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh())

    async def _refresh(self) -> None:
        for key, old_state in tuple(self._writer.st.occurrences.items()):
            selected = old_state.selected_recovery
            if selected is None or not selected.active or not self._writer.store_ok:
                continue
            if (
                selected.last_attempt is not None
                and timedelta()
                <= to_utc(self._now()) - to_utc(selected.last_attempt)
                < _REFRESH_GAP
            ):
                continue
            context = self._context(old_state, self._now())
            if context is None:
                continue
            got = await self._gate.trips(selected.source_query, False, context.schedule.window_end)
            state = self._writer.st.occurrences.get(key)
            if state is None or state.not_travelling or state.selected_recovery != selected:
                continue
            context = self._context(state, self._now())
            if context is None:
                continue
            self._latest.pop(key, None)
            error: str | None = (
                "NS recovery data is unavailable. Manual confirmations remain available."
            )
            checked = selected.checked_at
            issue = RecoverySourcePhase.UNAVAILABLE
            if not isinstance(got, str) and got.query == selected.source_query:
                candidates = recovery_candidates((got,), context, future_first=False)
                self._latest[key] = candidates
                matches = self._matching(candidates, selected.itinerary)
                if len(matches) == 1:
                    checked = got.fetched_at
                    if matches[0] == selected.itinerary and not selected.review_required:
                        error = None
                        issue = RecoverySourcePhase.CURRENT
                    else:
                        error = "NS changed this recovery plan. Review a new plan before relying on its times."
                        issue = RecoverySourcePhase.CHANGED
                else:
                    error = "NS no longer confirms this recovery route. Check the NS planner."
                    issue = RecoverySourcePhase.CHANGED
            updated = selected.update_monitoring(
                issue,
                error=error,
                checked_at=checked,
                attempted_at=self._now(),
            )
            self._save(state, updated)
        await self._deliver()

    async def idle(self) -> None:
        if self._task is not None:
            await asyncio.wait({self._task})

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
