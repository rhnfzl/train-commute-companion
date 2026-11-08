"""The runtime around the pure core: polling, the independent watchdog, the shared request
budget, look-ahead, transactional reload and the restart reconcile (7.1, 7.11, 7.12 rules 5,
10, 11, 12, 14, 21, 22, 5.2, 7.13). The pure modules take time and data as arguments; I/O lives
here, in store.py, budget.py, providers/ns.py, webhook_send.py, cli.py and replay.py."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from train_commute_companion import works_digest
from train_commute_companion.advice import (
    advice_query,
    advise,
    usual_arrival,
)
from train_commute_companion.budget import POLL_DEADLINE, POLL_EVERY_S, RequestGate
from train_commute_companion.calendar import TZ, Clock, resolve_local, to_utc
from train_commute_companion.choice import ChoiceTarget, choice_target, named_connections
from train_commute_companion.classify import current_run, evidence, find_leg
from train_commute_companion.compensation import ArrivalError, arrival_text, confirm
from train_commute_companion.compensation_state import JourneyRecord
from train_commute_companion.delivery import (
    BindingPhase,
    binding_phase,
    deliver,
    pending_journey_warnings,
)
from train_commute_companion.disruptions import (
    LEGACY_PRIMARY,
    PENDING_PRIMARY,
    context_enabled,
    disruptions_received,
    disruptions_told,
    notice_events,
    update_disruptions,
)
from train_commute_companion.health import (
    WatchedLeg,
    blindness,
    board_health,
    offline,
)
from train_commute_companion.ledger import (
    OBSERVED,
    EpisodeTransition,
    LiveLedger,
    PollMemory,
)
from train_commute_companion.lifecycle import (
    Choose,
    PollAnswers,
    UserAction,
    apply_action,
    is_resolved,
    location_queries,
    make_event,
    merged_pool,
    on_location,
    on_poll,
    origin_answer,
    queries_needed,
    target_occurrence,
    whole_day_keys,
)
from train_commute_companion.lifecycle_end import may_archive, on_tick
from train_commute_companion.lifecycle_lock import unfinished_leg
from train_commute_companion.location import LocationEvent
from train_commute_companion.lookahead import forget_pending, rebase_on_edit, reset_on_edit
from train_commute_companion.lookahead_probe import service_date
from train_commute_companion.lookahead_run import apply_run, lookahead_done, probe_direction
from train_commute_companion.match import usual_connection
from train_commute_companion.messages import CHECK_APP, RefusalKind, Refused, no_journey
from train_commute_companion.model import (
    AlertEvent,
    AlertFacts,
    AlertSettings,
    Config,
    Ctx,
    Direction,
    DirectionState,
    EventKind,
    Leaving,
    LegKind,
    LegState,
    LocationSettings,
    NotificationChannel,
    NotTravelling,
    Obligation,
    Observation,
    OccurrenceKey,
    Profile,
    ProfileProblem,
    Provenance,
    Severity,
    TrainId,
    alert_facts,
    direction_key,
    display_name,
    okey,
    present,
)
from train_commute_companion.news import all_good, forget_ended, news_of, severity
from train_commute_companion.notification_policy import channels_for, routing_ready
from train_commute_companion.notify import Notifier, WebhookPort, destination_id
from train_commute_companion.profile import load_config
from train_commute_companion.providers.base import (
    ActiveDisruptions,
    PlannedWorks,
    Pool,
    Provider,
    ProviderAnswer,
    Query,
)
from train_commute_companion.recovery_runtime import RecoveryController
from train_commute_companion.recovery_state import ORIGINAL_ROUTINE, RecoveryOffer, SelectedRecovery
from train_commute_companion.revisions import (
    baseline_on,
    effective_profile,
    find_profile,
    frozen,
    occurrence_ctx,
    profile_for,
    same_route,
    signature_for,
)
from train_commute_companion.runtime_state import (
    AuthEpisode,
    AuthPhase,
    OccurrenceRequest,
    PendingEditPhase,
    ReloadControl,
    ReloadPhase,
    ResultPhase,
    RouteDecision,
    RuntimeLifecycle,
    RuntimePhase,
    route_decision,
)
from train_commute_companion.schedule import schedules_active
from train_commute_companion.state import (
    BaselineHistory,
    LookAheadLedger,
    OccurrenceState,
    PollFailure,
    PollRecord,
    mark_sent,
    mark_sent_live,
)
from train_commute_companion.state_writer import Change, StateWriter
from train_commute_companion.store import PersistedState, StorageError, Store
from train_commute_companion.tasks import RefreshTask
from train_commute_companion.vehicle import VehicleProvider, VehicleWatch
from train_commute_companion.vehicle_monitor import VehicleMonitor
from train_commute_companion.view import RuntimeView, runtime_view
from train_commute_companion.webhook_queue import CopyQueue

log = logging.getLogger(__name__)

WATCHDOG_EVERY_S = 30
FILE_EVERY_S = 10
LOOKAHEAD_AT = time(6, 0)
_APP = "app"  # profile name on application warnings, which belong to no profile


def _reasons(errors: Sequence[ProfileProblem]) -> str:
    return "; ".join(f"{e.path}: {e.message}" for e in errors)


def fresh_state(key: OccurrenceKey, revision: int, baseline: int | None) -> OccurrenceState:
    return OccurrenceState(
        key=key,
        generation=0,
        route_revision=revision,
        settings_revision=revision,
        baseline_version=baseline,
        direction_state=DirectionState.IDLE,
        lock=None,
        retained=(),
        travelling=False,
        tap_at=None,
        at_station=False,
        errand_deadline=None,
        not_travelling=False,
        cancellation_history=(),
        missing_sent=False,
        all_good_evaluated=False,
    )


class _LoggingNotifier:
    def __init__(self, inner: Notifier, log_to: list[AlertEvent]) -> None:
        self._inner, self._log = inner, log_to

    async def send(self, event: AlertEvent) -> bool:
        try:
            ok = await self._inner.send(event)
        except Exception:  # a raising shell is a failed attempt, retried by the watchdog
            log.exception("notifier failed")
            return False
        if ok:
            self._log.append(event)
        return ok


def _advice_told(st: PersistedState, told: Sequence[tuple[Obligation, bool]]) -> PersistedState:
    """7.6: the advice episode a finished LEAVE_NOW belongs to is delivered once sent, and
    ended silently once dropped unsent, so the same train can be advised again."""
    occs = dict(st.occurrences)
    for o, sent in told:
        k = None if o.occurrence is None else okey(o.occurrence)
        s = None if k is None else occs.get(k)
        a = None if s is None else s.advice
        if k is None or s is None or a is None or a.sent_at is None:
            continue
        if a.advised_train != o.event.train or to_utc(a.sent_at) != to_utc(o.event.created_at):
            continue  # an older episode's message
        a = a.accept_delivery() if sent else a.withdraw()
        occs[k] = replace(s, advice=a)
    return replace(st, occurrences=occs)


def _restarted(st: PersistedState) -> PersistedState:
    """A loaded state as a start runs it (rule 11). Every delivered fact counts as last reported,
    so the reconcile poll ends the ones that ended (a recurrence is news again): one told outside
    a healthy poll has no `live:` mark yet, and neither has any fact of a file written before those
    marks. Every occurrence gets its own fixed schedule, which is a no-op once it has one."""
    occs = {}
    for k, s in st.occurrences.items():
        keys = dict(s.alert_keys)
        mark_sent_live(keys)
        occs[k] = frozen(st, replace(s, alert_keys=keys))

    def primary_only(obligation: Obligation) -> Obligation:
        if obligation.target_ids is not None:
            return obligation
        # Old state proves no auxiliary authentication scope. Keep only deferred phone binding.
        return replace(obligation, target_ids=frozenset(), primary_target_pending=True)

    return replace(
        st,
        occurrences=occs,
        obligations=tuple(primary_only(o) for o in st.obligations),
        lookahead={
            key: replace(ledger, pending=tuple(primary_only(o) for o in ledger.pending))
            for key, ledger in st.lookahead.items()
        },
    )


async def every(clock: Clock, seconds: float, step: Callable[[], Awaitable[None]]) -> None:
    """Runs `step` on fixed start times `seconds` apart (7.1: a poll every 2 min, not 2 min
    after the last one ended). A step that overran a whole interval starts again at once."""
    due = to_utc(clock.now())
    while True:
        try:
            await step()
        except Exception:  # one failing loop must not stop the others (rule 12)
            log.exception("runtime step failed")
        due += timedelta(seconds=seconds)
        now = to_utc(clock.now())
        due = max(due, now)
        await clock.sleep((due - now).total_seconds())


async def _run_together(*steps: Coroutine[Any, Any, None]) -> None:
    """`gather` that owns its children: when one raises, the error propagates as it is and the
    others are cancelled and awaited first; a plain gather would leave them running."""
    tasks = [asyncio.create_task(s) for s in steps]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _rebased(
    st: PersistedState, old: Config | None, new: Config, today: date
) -> tuple[dict[str, BaselineHistory], dict[str, LookAheadLedger]]:
    """The baselines and look-ahead ledgers after a profile file edit."""
    baselines, ledgers = dict(st.baselines), dict(st.lookahead)
    for p in new.profiles:
        q = find_profile(old, p.id)
        # removed and added again: its old history goes, so it starts from the file like a
        # new profile (a first configuration has no earlier profiles to forget)
        if q is None:
            for d in Direction if old is not None else ():
                baselines.pop(direction_key(p.id, d), None)
                ledgers.pop(direction_key(p.id, d), None)
            continue
        for d in Direction:
            dk = direction_key(p.id, d)
            hist = baselines.get(dk)
            if hist is None:
                continue
            if dk in ledgers:
                ledgers[dk] = reset_on_edit(q, p, d, hist, ledgers[dk], today)
                if not same_route(q, p):  # rule 5(c) drops its pending messages
                    ledgers[dk] = forget_pending(ledgers[dk])
            baselines[dk] = rebase_on_edit(q, p, d, hist, today)
    return baselines, ledgers


def _survivors(
    xs: Iterable[Obligation],
    finished: set[str],
    kept: Mapping[str, Obligation],
    marks: set[str],
) -> tuple[Obligation, ...]:
    return tuple(
        replace(o, webhook_copied=True) if o.id in marks else o
        for o in (kept.get(x.id, x) for x in xs if x.id not in finished)
    )


class Runtime:
    """What its public methods return: a user action (`act`, `leaving`, `not_travelling`,
    `choose_at`) None when written, else its `Refused`; a query (`stations`) None for no answer."""

    def __init__(
        self,
        *,
        profile_path: Path,
        store: Store,
        provider: Provider,
        notifier: Notifier,
        clock: Clock,
        # True: with no station list, codes are never accepted unchecked (5.2, 5.3): the stored
        # configuration runs and an edit waits. False: replays and tests, which have no list
        require_stations: bool,
        known_stations: frozenset[str] | None = None,
        on_stations: Callable[[frozenset[str]], object] | None = None,
        lookahead_at: time = LOOKAHEAD_AT,
        webhooks: WebhookPort | None = None,
        primary_destination: Callable[[], str | None] | None = None,
    ) -> None:
        # Omitted only for legacy clients and replay rigs. Shells supply actual recipient IDs.
        self._primary_destination = primary_destination or (lambda: LEGACY_PRIMARY)
        self._target_pins: dict[str, Obligation] = {}
        self._path = profile_path
        self._queue = CopyQueue(
            webhooks,
            clock.now,
            self._save_rest,
            self._waits,
            lambda held: self._held_ledger if held else self._ledger,
            lambda copy: self._writer.copy_audience_written(copy),
        )
        self._writer = StateWriter(store, self._queue, self._storage_note)
        self._lookahead_at = lookahead_at
        self._reload = ReloadControl()
        self._lifecycle = RuntimeLifecycle()
        self._store = store
        self._provider = provider
        self.events_log: list[AlertEvent] = []
        self._notifier = _LoggingNotifier(notifier, self.events_log)
        self._clock = clock
        self.known_stations = known_stations
        self._gate = RequestGate(provider, clock.now)
        self._mtime: int | None = None
        self._auth = AuthEpisode()
        self._memory = PollMemory()
        self._vehicle_monitor = (
            VehicleMonitor(provider, self._gate, clock.now, self._vehicle_watches)
            if isinstance(provider, VehicleProvider)
            else None
        )
        self._vehicle_refresh = RefreshTask(self._refresh_vehicles, coalesce=True)
        self._digest_refresh = RefreshTask(self.works_digest_once, coalesce=False)
        self._digest_attempts: dict[str, datetime] = {}
        self._sending = asyncio.Lock()

        def read() -> PersistedState:
            return self._writer.st

        self._ledger = LiveLedger(
            read, read, self._memory, clock.now, lambda: self.available_channels
        )
        self._held_ledger = LiveLedger(
            read,
            self._writer.judged_state,
            self._memory,
            clock.now,
            lambda: self.available_channels,
        )
        # an edit waiting for the station list retries it under the shared budget
        self._require_stations = require_stations
        self._on_stations = on_stations
        self._stations_tried: datetime | None = None
        # profile id -> location source epoch, bumped on every accepted location change (8):
        # an event read under an older epoch is dropped, even after a switch back (A, B, A)
        self._loc_epoch: dict[str, int] = {}
        self._recovery = RecoveryController(
            self._writer,
            self._gate,
            clock.now,
            self._ctx,
            self.view,
            self._recovery_notice,
            self._deliver,
        )

    # --- configuration and context ---

    @property
    def available_channels(self) -> frozenset[NotificationChannel]:
        channels: set[NotificationChannel] = {"primary"}
        if self._queue.port is not None:
            channels.update(self._queue.port.channels.values())
        return frozenset(channels)

    def _routing_ready_in(self, state: PersistedState, o: Obligation) -> bool:
        p = find_profile(state.config, o.event.profile_id)
        return p is None or routing_ready(
            p.alerts, o.event.kind, self.available_channels, look_ahead=o.look_ahead is not None
        )

    @property
    def config(self) -> Config | None:
        return self._writer.st.config

    @property
    def state(self) -> PersistedState:
        return self._writer.st

    @property
    def refusal(self) -> str | None:
        """Why the last reload refused the file (or waits for the station list); None if not."""
        return self._reload.refusal

    @property
    def store_ok(self) -> bool:
        return self._writer.store_ok

    @property
    def key_rejected(self) -> bool:
        return self._auth.phase is AuthPhase.REJECTED

    @property
    def started(self) -> bool:
        return self._lifecycle.phase is RuntimePhase.RUNNING

    @property
    def requests(self) -> int:
        return self._gate.requests

    def now(self) -> datetime:
        return self._clock.now()

    def _profile_for(self, s: OccurrenceState) -> Profile | None:
        return profile_for(self._writer.st, s)

    def _ctx(self, s: OccurrenceState, now: datetime) -> Ctx | None:
        return occurrence_ctx(self._writer.st, s, now)

    def view(self) -> RuntimeView:
        """A frozen snapshot for shells (5.4): nothing in it changes as the runtime goes on. It
        shows the written state (rule 22); an unsent alert counts whether written or not."""
        now = self._clock.now()
        snapshot = runtime_view(
            self._writer.st,
            self._memory.boards,
            now,
            started=self.started,
            refusal=self.refusal,
            store_ok=self._writer.store_ok,
            key_rejected=self.key_rejected,
            undelivered=self._writer.undelivered(),
            requests=self.requests,
            location_epochs=self._loc_epoch,
            positions=self._provider.positions,
        )
        snapshot = replace(
            snapshot,
            occurrences=tuple(
                replace(
                    o,
                    recovery_offer=self._recovery.offer_for(o.key),
                    replacement_plans=self._recovery.plans_for(o.key) or o.replacement_plans,
                )
                for o in snapshot.occurrences
            ),
        )
        if self._vehicle_monitor is None:
            return snapshot
        watches = {
            okey(state.key): self._vehicle_watch(state, now)
            for state in self._writer.st.occurrences.values()
        }
        return replace(
            snapshot,
            occurrences=tuple(
                replace(occurrence, train_context=self._vehicle_monitor.snapshot(watch, now))
                if (watch := watches.get(okey(occurrence.key))) is not None
                else occurrence
                for occurrence in snapshot.occurrences
            ),
        )

    def _vehicle_watch(self, state: OccurrenceState, now: datetime) -> VehicleWatch | None:
        if state.not_travelling or is_resolved(state) or state.lock is None:
            return None
        health = board_health(state.poll_records, now)
        watched = unfinished_leg(state.lock)
        if (
            not health.fresh
            or health.consecutive_failures
            or watched is None
            or watched.state not in (LegState.PENDING, LegState.RIDING)
        ):
            return None
        answers = self._memory.boards.get(okey(state.key), ())
        run = current_run(answers, watched.train, watched.board_station, watched.alight_stop)
        if (
            run is None
            or run[1].kind not in (LegKind.OK, LegKind.DELAYED)
            or run[1].leg.alternative_transport
        ):
            return None
        checked = max(
            (
                answer.fetched_at
                for answer in answers
                if any(
                    leg.train == watched.train and leg.board.code == watched.board_station
                    for connection in answer.connections
                    for leg in connection.legs
                )
            ),
            key=to_utc,
            default=None,
        )
        return (
            None if checked is None else VehicleWatch(watched.train, watched.board_station, checked)
        )

    def _vehicle_watches(self) -> tuple[VehicleWatch, ...]:
        now = self._clock.now()
        return tuple(
            watch
            for state in self._writer.st.occurrences.values()
            if (watch := self._vehicle_watch(state, now)) is not None
        )

    def _refresh_vehicles_soon(self) -> None:
        if self._vehicle_monitor is None or self._lifecycle.phase in (
            RuntimePhase.STOPPING,
            RuntimePhase.STOPPED,
            RuntimePhase.FAILED,
        ):
            return
        self._vehicle_refresh.request()

    @property
    def _vehicle_task(self) -> asyncio.Task[None] | None:
        return self._vehicle_refresh.task

    async def _refresh_vehicles(self) -> None:
        monitor = self._vehicle_monitor
        if monitor is None:
            return
        try:
            await monitor.refresh(self._vehicle_watches())
        except Exception:
            log.exception("optional train context refresh failed")

    async def vehicle_context_idle(self) -> None:
        await self._vehicle_refresh.idle()

    async def stop_vehicle_context(self) -> None:
        await self._vehicle_refresh.stop()

    def _refresh_digest_soon(self) -> None:
        if self._lifecycle.phase not in (
            RuntimePhase.STOPPING,
            RuntimePhase.STOPPED,
            RuntimePhase.FAILED,
        ):
            self._digest_refresh.request()

    @property
    def _digest_task(self) -> asyncio.Task[None] | None:
        return self._digest_refresh.task

    async def works_digest_once(self) -> None:
        """Optional weekly checks share the NS budget and never block live polling."""
        config = self._writer.st.config
        if config is None or self._writer.waiting:
            return
        for profile in config.profiles:
            start = self._clock.now()
            decision = works_digest.digest_decision(
                profile,
                start,
                self._writer.st.works_digest_weeks.get(profile.id),
                self._digest_attempts.get(profile.id),
            )
            if decision.phase is not works_digest.DigestPhase.DUE:
                continue
            week = present(decision.week)
            self._digest_attempts[profile.id] = start
            token = works_digest.profile_token(profile)
            deadline = to_utc(start) + timedelta(seconds=works_digest.TIME_LIMIT_S)

            async def fetch(
                queries: Sequence[Query], bound: datetime = deadline
            ) -> tuple[ProviderAnswer, ...] | PollFailure:
                return await self._gate.all_trips(queries, False, None, deadline=bound)

            try:
                rows = await works_digest.check_week(
                    self._writer.st, profile, start, self._fetch_works, fetch, self._clock.now
                )
                current = find_profile(self._writer.st.config, profile.id)
                now = self._clock.now()
                if (
                    current is None
                    or works_digest.profile_token(current) != token
                    or works_digest.due_week(
                        current, now, self._writer.st.works_digest_weeks.get(profile.id)
                    )
                    != week
                ):
                    continue
                message = works_digest.obligation(
                    profile, present(self._writer.st.config).revision, week, start, rows
                )
                candidate = replace(
                    self._writer.st,
                    works_digest_weeks={**self._writer.st.works_digest_weeks, profile.id: week},
                    obligations=(*self._writer.st.obligations, message),
                )
                if self._writer.commit_strict(candidate):
                    await self._deliver()
            except Exception:
                log.exception("optional weekly planned-works digest failed")

    async def works_digest_idle(self) -> None:
        await self._digest_refresh.idle()

    async def stop_works_digest(self) -> None:
        await self._digest_refresh.stop()

    # --- the serialised writer: state, ledger and obligations in one save (rule 5, 22) ---

    def _obligation(self, e: AlertEvent, s: OccurrenceState | None, now: datetime) -> Obligation:
        # a LEAVE_NOW has no fixed expiry: still_advised checks its current departure minus
        # buffer at send time (check_good), so a delayed escape train still catchable goes out
        if s is not None:
            revision = s.route_revision
        else:
            revision = self._writer.st.config.revision if self._writer.st.config else 0
        # one poll shares `now` and the key names neither profile nor direction (7.12 rule 6)
        d = e.direction.value if e.direction else ""
        return Obligation(
            id=f"{e.profile_id}|{d}|{e.key}|{e.severity.value}|{now.isoformat()}",
            event=e,
            occurrence=None if s is None else s.key,
            look_ahead=None,
            route_revision=revision,
            baseline_version=None if s is None else s.baseline_version,
            fact_key=e.key,
            expires_at=None,
            attempts=0,
        )

    def _app_obligation(self, kind: EventKind, body: str, now: datetime, tag: str) -> Obligation:
        e = AlertEvent(
            profile_id="",
            profile_name=_APP,
            direction=None,
            service_date=None,
            leg_index=None,
            kind=kind,
            severity=Severity.LOUD,
            train=None,
            text=f"{display_name(_APP)}: {body}",
            key=f"{kind.value}:{tag}:{now.isoformat()}",
            created_at=now,
        )
        return self._obligation(e, None, now)

    def _app_warning(self, kind: EventKind, body: str, now: datetime, tag: str = "") -> None:
        o = self._app_obligation(kind, body, now, tag)
        st = self._writer.st
        self._writer.propose(replace(st, obligations=(*st.obligations, o)))

    def _record(
        self,
        s: OccurrenceState,
        events: Sequence[AlertEvent],
        now: datetime,
        polled: bool,
        occs: Mapping[str, OccurrenceState],
    ) -> tuple[OccurrenceState, list[Obligation]]:
        """News (7.5) and escalation (rule 4) against the occurrence's delivery ledger.
        `polled`: the events are everything a healthy poll reports, so a reported fact it no
        longer reports has ended and leaves the ledger (a recurrence is news again).
        `occs`: the occurrences its alerts are told beside."""
        if s.selected_recovery is not None and s.selected_recovery.status == "active":
            events = tuple(e for e in events if e.kind not in ORIGINAL_ROUTINE)
        keys = dict(s.alert_keys)
        if polled:
            forget_ended(keys, events)
        out = news_of(keys, events, now)
        if not out:
            return (s if keys == s.alert_keys else replace(s, alert_keys=keys)), []
        twins = [
            x for x in occs.values()
            if x.key != s.key and x.key.profile_id == s.key.profile_id
            and x.key.direction is s.key.direction and not is_resolved(x)
        ]  # fmt: skip
        obligations = []
        for e in out:
            mark_sent(keys, e.key, e.severity)
            if twins:  # 7.13: alerts name the service date while two occurrences exist
                note = f" (service date {s.key.service_date.isoformat()})"
                e = replace(e, text=e.text + note, facts=replace(alert_facts(e), twin_note=note))
            obligations.append(self._obligation(e, s, now))
        return replace(s, alert_keys=keys), obligations

    def _apply(
        self,
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]],
        polled: bool = False,
        kind: Change = Change.RECOMPUTED,
    ) -> None:
        now = self._clock.now()
        st = self._writer.st
        occs = dict(st.occurrences)
        obligations = list(st.obligations)
        for k, (s, events) in changes.items():
            occs[k] = s  # twins see the occurrences written so far
            s, new = self._record(s, events, now, polled, occs)
            occs[k] = s
            obligations += new
        st = replace(st, occurrences=occs, obligations=tuple(obligations))
        records = dict(st.journey_records)
        for k, state in occs.items():
            profile = self._profile_for(state)
            if profile is None:
                continue
            previous = records.get(k)
            if previous is None:
                records[k] = JourneyRecord(
                    state.key,
                    profile.destination(state.key.direction),
                    state.original_plan,
                    state.route_revision,
                )
            elif previous.plan is None and state.original_plan is not None:
                records[k] = replace(previous, plan=state.original_plan)
        st = replace(st, journey_records=records)
        self._writer.commit(self._archived(st, now), kind)

    def _archived(self, st: PersistedState, now: datetime) -> PersistedState:
        """Rule 6: an occurrence leaves the store once it ended and its pending journey warnings
        were delivered; it stays until its window closed so it is not created again."""
        occs = {}
        for k, s in st.occurrences.items():
            ctx = occurrence_ctx(st, s, now)
            closed = ctx is None or to_utc(now) >= to_utc(ctx.schedule.window_end)
            if closed and may_archive(s, now, pending_journey_warnings(st.obligations, s.key)):
                self._memory.forget(k)
                continue
            occs[k] = s
        # snapshots are pruned on write
        held = {
            alert_facts(o.event).arrival_record
            for o in (*st.obligations, *(copy.obligation for copy in st.webhook_copies))
        }
        records = {
            key: record
            for key, record in st.journey_records.items()
            if key in occs
            or key in held
            or (now.astimezone(TZ).date() - record.key.service_date).days <= 120
        }
        return replace(st, occurrences=occs, journey_records=records)

    async def _deliver(self) -> None:
        """Every pending obligation, revalidated right before sending (rule 5). Merges by id,
        so obligations added by another task while a send awaited are kept. Serialised: the
        poll, watchdog and file watch all deliver, and two passes would send one alert twice.
        The webhook copies go after, in a task of their own: a slow receiver never holds up a
        phone send or the loop that delivers (an alert's advice can expire)."""
        async with self._sending:
            await self._deliver_pass()
        self._queue.post_soon()

    def _storage_note(self) -> Obligation:
        return self._app_obligation(
            EventKind.STORAGE_FAILED,
            "could not save state, alerts may repeat or be lost.",
            self._clock.now(),
            "",
        )

    def _save_rest(self, accepted: Sequence[tuple[Obligation, str]] = ()) -> None:
        """Writes the webhook rests now, synchronously, before a try's request leaves (rule 22:
        a failed write takes the same path as any other)."""
        accepted = tuple(
            (o, target)
            for o, target in accepted
            if o.event.kind in (EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED)
        )
        self._record_delivery(set(), {}, (), True, accepted=accepted)

    async def webhooks_idle(self) -> None:
        """Waits until no webhook pass runs (a clean shutdown, tests)."""
        await self._queue.idle()

    def _waits(self, o: Obligation) -> bool:
        """Not judged yet: written before this start (judged once the reconcile ran, rule 11),
        or judged on observations while its occurrence has no healthy answer (rule 5b)."""
        if not self._routing_ready_in(self._writer.st, o):
            return True
        if self._lifecycle.holds(o.event.created_at):
            return True
        k = None if o.occurrence is None else okey(o.occurrence)
        return (
            o.event.kind in OBSERVED
            and k in self._writer.st.occurrences
            and k not in self._memory.boards
        )

    def _pin_targets(self, o: Obligation, ledger: LiveLedger) -> Obligation:
        remembered = self._target_pins.get(o.id)
        if binding_phase(o) is BindingPhase.UNBOUND and remembered is not None:
            o = replace(
                o,
                target_ids=remembered.target_ids,
                primary_target_pending=remembered.primary_target_pending,
                event=replace(
                    o.event,
                    facts=replace(
                        alert_facts(o.event),
                        disruption_targets=alert_facts(remembered.event).disruption_targets,
                    ),
                ),
            )
        selected = ledger.selected_channels(o)
        primary = self._primary_destination()
        facts = alert_facts(o.event)
        source_targets = facts.disruption_targets
        event = o.event
        if source_targets is not None and PENDING_PRIMARY in source_targets and primary is not None:
            source_targets = (source_targets - {PENDING_PRIMARY}) | {primary}
            event = replace(event, facts=replace(facts, disruption_targets=source_targets))
        if binding_phase(o) is BindingPhase.UNBOUND:
            targets = frozenset(
                destination_id(self._queue.port, u)
                for u in (() if self._queue.port is None else self._queue.port.urls)
                if self._queue.port is not None and self._queue.port.channels[u] in selected
            )
            if primary is not None and "primary" in selected:
                targets |= {primary}
            if source_targets is not None:
                targets &= source_targets
            pending = (
                "primary" in selected
                and primary is None
                and (source_targets is None or PENDING_PRIMARY in source_targets)
            )
            pinned = replace(o, event=event, target_ids=targets, primary_target_pending=pending)
            self._target_pins[o.id] = pinned
            return pinned
        if binding_phase(o) is BindingPhase.PRIMARY_PENDING and (
            primary is not None or "primary" not in selected
        ):
            targets = present(o.target_ids) | (
                {primary} if primary is not None and "primary" in selected else set()
            )
            pinned = replace(o, event=event, target_ids=targets, primary_target_pending=False)
            self._target_pins[o.id] = pinned
            return pinned
        if remembered is not None:
            self._target_pins[o.id] = o
        return o

    async def _deliver_pass(self) -> None:
        finished: set[str] = set()
        kept: dict[str, Obligation] = {}
        told: list[tuple[Obligation, bool]] = []  # finished advice/context, sent (else dropped)
        accepted: list[tuple[Obligation, str]] = []
        copies = False  # the webhook copies changed
        try:
            for o, held in self._writer.pending():
                if self._waits(o):
                    continue
                v = self._held_ledger if held else self._ledger
                pinned = self._pin_targets(o, v)
                staged = self._queue.stage(pinned, held)
                copies |= staged
                if pinned != o:
                    o = pinned
                    self._writer.settle(o, o, False)
                    # Persist the audience before a request can disclose the alert to a recipient.
                    self._record_delivery(
                        finished, {**kept, o.id: o}, told, copies, accepted=accepted
                    )
                    finished.clear()
                    kept.clear()
                    told.clear()
                    accepted.clear()
                    copies = False
                # a fresh clock per message: a slow send must not leave the next one judged early
                destination = self._primary_destination()
                r, sent = await deliver(
                    o,
                    v,
                    self._notifier,
                    self._clock.now(),
                    destination,
                    audience_written=self._writer.audience_written(o),
                )
                if (
                    sent
                    and destination is not None
                    and o.event.kind in (EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED)
                ):
                    accepted.append((o, destination))
                if r is not None:
                    r = self._queue.after_send(r)
                if r is None and o.event.kind in (
                    EventKind.LEAVE_NOW,
                    EventKind.DISRUPTION,
                    EventKind.DISRUPTION_CLEARED,
                ):
                    told.append((o, sent))
                if self._writer.settle(o, r, sent):
                    continue
                if r is None:
                    finished.add(o.id)
                elif r != o:
                    kept[o.id] = r
        finally:  # each acknowledged send is recorded, also when shutdown cancels the pass
            self._record_delivery(finished, kept, told, copies, accepted=accepted)

    def _record_delivery(
        self,
        finished: set[str],
        kept: Mapping[str, Obligation],
        told: Sequence[tuple[Obligation, bool]],
        copies: bool = False,
        *,
        accepted: Sequence[tuple[Obligation, str]] = (),
    ) -> None:
        if not finished and not kept and not told and not copies and not accepted:
            return

        # Failed writes replay this outcome after the caller resumes and changes its accumulators.
        finished, kept, told, accepted = set(finished), dict(kept), tuple(told), tuple(accepted)
        receipt_time = self._clock.now()
        marks = set(self._queue.copied)  # written too: a phone send in flight is not in `kept` yet

        def outcome(st: PersistedState) -> PersistedState:
            # look-ahead messages live in their ledger too, so a delivered one counts as told
            ledgers = {
                k: replace(v, pending=_survivors(v.pending, finished, kept, marks))
                for k, v in st.lookahead.items()
            }
            st = _advice_told(st, told)
            st = disruptions_told(st, told)
            st = disruptions_received(st, accepted)
            obligations = _survivors(st.obligations, finished, kept, marks)
            st = replace(st, obligations=obligations, lookahead=ledgers)
            return self._receipt_followups(st, receipt_time) if accepted else st

        st = self._archived(outcome(self._writer.st), self._clock.now())
        self._writer.commit_delivery(st, outcome, finished)
        if self._writer.store_ok:
            self._target_pins.clear()

    def _receipt_followups(self, st: PersistedState, now: datetime) -> PersistedState:
        """A late accepted start can owe its own clearance, even after the phone's clear ended."""
        occurrences, obligations = dict(st.occurrences), list(st.obligations)
        for key, occurrence in tuple(occurrences.items()):
            context = occurrence_ctx(st, occurrence, now)
            if context is None:
                continue
            events = tuple(
                e
                for e in notice_events(occurrence, context)
                if e.kind is EventKind.DISRUPTION_CLEARED
            )
            if events:
                updated, owed = self._record(occurrence, events, now, False, occurrences)
                occurrences[key] = updated
                obligations.extend(owed)
        return replace(st, occurrences=occurrences, obligations=tuple(obligations))

    def _disruption_audience(self, settings: AlertSettings) -> frozenset[str]:
        selected = channels_for(settings, EventKind.DISRUPTION)
        primary = self._primary_destination()
        return frozenset(
            () if "primary" not in selected else (primary or PENDING_PRIMARY,)
        ) | frozenset(
            destination_id(self._queue.port, u)
            for u in (() if self._queue.port is None else self._queue.port.urls)
            if self._queue.port is not None and self._queue.port.channels[u] in selected
        )

    def _track_key_rejection(self, error: PollFailure | None, who: str, now: datetime) -> None:
        """`key_rejected` once per outage and again when each window opens while it lasts."""
        if self._auth.observe(error, who):
            self._app_warning(EventKind.KEY_REJECTED, "your NS key was rejected.", now, who)

    # --- polling ---

    def _create_occurrences(self, now: datetime) -> None:
        cfg = self._writer.st.config
        if cfg is None:
            return

        def first(p: Profile, d: Direction, commute_day: date) -> time | None:
            # each occurrence's window from the baseline in force on its own service date
            sig = signature_for(self._writer.st, p, d, service_date(p, d, commute_day))
            return sig.legs[0].departure if sig is not None else None

        occs = dict(self._writer.st.occurrences)
        for sch in schedules_active(cfg, {}, now, first):
            k = okey(sch.key)
            if k not in occs:
                hist = self._writer.st.baselines.get(
                    direction_key(sch.key.profile_id, sch.key.direction)
                )
                profile = find_profile(cfg, sch.key.profile_id)
                v = (
                    baseline_on(hist, profile, sch.key.direction, sch.key.service_date)
                    if hist is not None and profile is not None
                    else None
                )
                fresh = fresh_state(sch.key, cfg.revision, v.version if v else None)
                own = v.signature if v else None
                occs[k] = replace(
                    fresh,
                    baseline_signature=own,
                    baseline_frozen=True,
                    alert_keys=self._writer.marks_of(k),  # told while the store was down (r22)
                )
        self._writer.propose(replace(self._writer.st, occurrences=occs))

    async def poll_once(self) -> None:
        self._create_occurrences(self._clock.now())
        await self._poll_occurrences()
        await self._deliver()
        self._refresh_vehicles_soon()
        self._refresh_digest_soon()
        self._recovery.refresh_soon()

    async def _poll_occurrences(self) -> None:
        for k in sorted(self._writer.st.occurrences):
            try:
                await self._poll_occurrence(k)
            except Exception:  # one failing occurrence must not starve the others (rule 12)
                log.exception("poll of %s failed", k)
        await self._poll_active_disruptions()

    def _disruption_context(self, state: OccurrenceState, now: datetime) -> Ctx | None:
        context = self._ctx(state, now)
        current = find_profile(self._writer.st.config, state.key.profile_id)
        if context is None or current is None or current.paused_on(state.key.service_date):
            return None
        return context if context_enabled(state, context) else None

    async def _poll_active_disruptions(self) -> None:
        if not self._writer.store_ok:
            return  # an auxiliary proposal must not replace an unwritten train safety decision
        now = self._clock.now()
        if not any(
            self._disruption_context(s, now) is not None
            for s in self._writer.st.occurrences.values()
        ):
            return
        got = await self._gate.listing(self._provider.active_disruptions, "active disruptions")
        if not self._writer.store_ok:
            return
        if got == "auth":
            self._track_key_rejection("auth", "active disruptions", self._clock.now())
        feed = got if isinstance(got, ActiveDisruptions) else ActiveDisruptions((), "unknown")
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]] = {}
        for key, state in self._writer.st.occurrences.items():
            context = self._disruption_context(state, self._clock.now())
            if context is None:
                continue
            transition = update_disruptions(
                state,
                context,
                feed,
                self._memory.boards.get(key, ()),
                audience=self._disruption_audience(context.profile.alerts),
            )
            changes[key] = (transition.state, transition.events)
        self._apply(changes)

    async def _poll_occurrence(self, k: str) -> None:
        s = self._writer.st.occurrences.get(k)
        now = self._clock.now()
        ctx = None if s is None else self._ctx(s, now)
        if s is None or ctx is None or is_resolved(s):
            return
        followed = s.lock is not None or bool(s.retained)
        if not followed and to_utc(now) >= to_utc(ctx.schedule.window_end):
            t = on_tick(s, ctx)
            self._apply({k: (t.state, t.events)})
            return  # the window only selects the first train (rule 19)
        gen, end = s.generation, ctx.schedule.window_end
        deadline = to_utc(now) + POLL_DEADLINE  # one hard deadline for every request (7.1)
        got = await self._gate.all_trips(queries_needed(s, ctx), True, end, deadline)
        error = got if isinstance(got, str) else None
        answers = () if isinstance(got, str) else got
        advice_error: PollFailure | None = None
        if (
            error is None
            and not (s.selected_recovery is not None and s.selected_recovery.status == "active")
            and (usual := origin_answer(answers, ctx)) is not None
        ):
            # 7.6: an earlier catchable train is before the time the poll asked from; a failed
            # search leaves advice to the poll's own answers and never fails the poll
            q = advice_query(s, ctx, usual, usual_arrival(s, ctx, usual))
            extra = None if q is None else await self._gate.trips(q, True, end, deadline)
            if isinstance(extra, str):
                advice_error = extra
            elif extra is not None:
                answers = (*answers, extra)
        now = self._clock.now()
        s = self._writer.st.occurrences.get(k)
        ctx = None if s is None else self._ctx(s, now)
        if s is None or ctx is None:
            return
        if advice_error == "auth":
            # 7.11: a rejected key on the advice search is reported like any other, loudly;
            # the main answers still count as observations
            error = "auth"
        elif advice_error is not None:
            log.warning("advice search for %s failed (%s)", k, advice_error)
        self._track_key_rejection(error, k, now)
        poll = PollAnswers(answers, (PollRecord(now, error is None, error),), gen)
        t = on_poll(s, ctx, poll)
        st, events = t.state, list(t.events)
        current = s.generation == gen
        if current:  # a choice since the poll started makes the answer stale
            st, events = self._judge_poll(k, s.lock is None, st, events, ctx, poll)
        context = self._disruption_context(st, now)
        if context is not None:
            events += notice_events(st, context)
        self._apply({k: (st, events)}, polled=current and bool(answers))

    def _judge_poll(
        self,
        k: str,
        unlocked: bool,
        st: OccurrenceState,
        events: list[AlertEvent],
        ctx: Ctx,
        poll: PollAnswers,
    ) -> tuple[OccurrenceState, list[AlertEvent]]:
        answers = poll.answers
        # the whole poll's evidence for the watchdog, the auto lock and advice (6.2)
        merged = origin_answer(answers, ctx)
        if merged is not None and unlocked:
            self._remember_usual(k, ctx, merged)
        tick = on_tick(st, ctx, merged)
        st, events = tick.state, [*events, *tick.events]
        if st.lock is not None and not st.all_good_evaluated:
            good = all_good(st, poll, ctx)  # judged once, at the first poll with a lock
            st = replace(st, all_good_evaluated=True)
            events += [good] if good else []
        if merged is not None:
            # no reference: an absent or broken usual still makes waiting bad (7.6)
            arrival = usual_arrival(st, ctx, merged)
            record, more = advise(st, ctx, merged, arrival)
            if ctx.signature is None and arrival is not None:  # never a relock's arrival
                record = replace(record, usual_arrival=arrival)
            st, events = replace(st, advice=record), [*events, *more]
        if answers:
            self._remember(k, st, answers)
            self._memory.boards[k] = answers
        return st, events

    def _remember_usual(self, k: str, ctx: Ctx, merged: Pool) -> None:
        """Before a lock: what the poll shows of every usual leg (rules 13, 20), each as its
        current copy, so the watchdog judges it on the same terms as a locked leg, never on
        invented timing."""
        p, d, first = ctx.profile, ctx.direction, ctx.schedule.first_departure
        old = self._memory.before.get(k, ())
        c = usual_connection(merged, p.origin(d), first, p.journey(d).transfers, ctx.signature)
        none = Provenance.MISSING
        if c is None:
            train = TrainId(ctx.schedule.key.service_date, "", first)
            seen = old[0].last_seen_at if old else None
            missing = WatchedLeg(train, first, False, Observation.MISSING, none, none, seen)
            self._memory.before[k] = (missing,)
            return
        out = []
        for i, g in enumerate(c.legs):
            ob = find_leg(merged, g.train, g.board.code)[1]
            run = current_run(evidence(merged), g.train, g.board.code, g.alight.code)
            b = g.board if run is None else run[1].leg.board
            dep = b.departure.provenance if b.departure else none
            if ob is Observation.SEEN and run is not None:
                seen = run[0]
            else:
                seen = old[i].last_seen_at if i < len(old) else None
            at = first if i == 0 else g.train.planned_departure
            out.append(WatchedLeg(g.train, at, False, ob, dep, dep, seen))
        self._memory.before[k] = tuple(out)

    def _remember(self, k: str, s: OccurrenceState, answers: Sequence[ProviderAnswer]) -> None:
        pool = merged_pool(answers)
        seen = self._memory.seen.setdefault(k, {})
        for j in (*((s.lock,) if s.lock else ()), *s.retained):
            for x in j.legs:
                leg, ob = find_leg(pool, x.train, x.board_station)
                old = seen.get(x.train)
                dep = arr = Provenance.MISSING
                # the current copy of the found train (6.2), never the first copy met
                run = None
                if leg is not None:
                    run = current_run(answers, leg.train, x.board_station, x.intended_alight)
                if run is not None:
                    g = run[1].leg
                    dep = g.board.departure.provenance if g.board.departure else dep
                    # after a cut short the user rides to the last served stop, `alight_stop`
                    stop = next((st for st in g.stops if st.code == x.alight_stop), g.alight)
                    arr = stop.arrival.provenance if stop.arrival else arr
                # an uncertain or missing identity never refreshes it (rules 12, 13, 6.1)
                last = (
                    run[0]
                    if ob is Observation.SEEN and run is not None
                    else (old.last_seen_at if old else None)
                )
                seen[x.train] = WatchedLeg(
                    x.train, x.train.planned_departure, False, ob, dep, arr, last
                )

    # --- the watchdog (rule 12) ---

    def _watched(self, k: str, s: OccurrenceState, ctx: Ctx, now: datetime) -> list[WatchedLeg]:
        seen = self._memory.seen.get(k, {})
        out = []
        main = (s.lock,) if s.lock and s.direction_state is not DirectionState.DONE else ()
        for j in (*main, *s.retained):
            for x in j.legs:
                if x.state in (LegState.CANCELLED, LegState.RELOCKED):
                    continue
                w = x.watch_until
                watched = x.state is LegState.DONE and (w is None or to_utc(now) < to_utc(w))
                if x.state is LegState.DONE and not watched:
                    continue
                gone = to_utc(now) >= to_utc(x.train.planned_departure)  # may be on it (rule 7)
                riding = (
                    watched
                    or x.state in (LegState.RIDING, LegState.MISSED)
                    or j in s.retained
                    or gone
                )
                base = seen.get(x.train) or WatchedLeg(
                    x.train, x.train.planned_departure, False, Observation.SEEN,
                    Provenance.REPORTED, Provenance.REPORTED, None,
                )  # fmt: skip
                out.append(replace(base, riding_or_watched=riding))
        if (
            s.lock is None and not s.retained and k in self._memory.before
        ):  # before a lock (rule 13)
            head, *onward = self._memory.before[k]  # any usual leg (rule 13)
            out += [replace(head, departure=ctx.schedule.first_departure), *onward]
        elif s.lock is None and not s.retained:  # never answered yet: by board age only
            first = ctx.schedule.first_departure
            last_ok = board_health(s.poll_records, now).last_ok_at
            ok = Provenance.REPORTED
            usual = TrainId(s.key.service_date, "", first)
            out.append(WatchedLeg(usual, first, False, Observation.SEEN, ok, ok, last_ok))
        return out

    def _health_events(
        self, k: str, s: OccurrenceState, ctx: Ctx, now: datetime
    ) -> list[AlertEvent]:
        legs = self._watched(k, s, ctx, now)
        health = board_health(s.poll_records, now)
        blind, reason = blindness(legs, health, now)
        name = display_name(ctx.profile.name)
        out = []

        def event(kind: EventKind, body: str, tag: str) -> AlertEvent:
            sev = severity(
                kind,
                minutes_to_departure=None,
                breaks_transfer=False,
                past_latest_arrival=False,
                is_tomorrow=False,
            )
            return AlertEvent(
                profile_id=ctx.profile.id,
                profile_name=ctx.profile.name,
                direction=ctx.direction,
                service_date=s.key.service_date,
                leg_index=None,
                kind=kind,
                severity=sev,
                train=None,
                text=f"{name}: {body}",
                key=f"{kind.value}:{k}:{tag}",
                created_at=now,
            )

        change = self._memory.observe_blind(k, blind, now)
        if change.transition is EpisodeTransition.ENTERED:
            text = f"cannot see your train in NS data ({reason}), {CHECK_APP}."
            out.append(event(EventKind.CANNOT_SEE_TRAIN, text, now.isoformat()))
        elif change.transition is EpisodeTransition.EXITED:
            since = present(change.since)
            text = "your train is visible in NS data again."
            out.append(event(EventKind.CAN_SEE_TRAIN_AGAIN, text, since.isoformat()))
        zone = timedelta(minutes=30)
        near = any(
            x.riding_or_watched or timedelta(0) <= to_utc(x.departure) - to_utc(now) <= zone
            for x in legs
        )
        unavailable = offline(health, near)
        wanted = unavailable if s.poll_records else unavailable and k in self._memory.offline
        if self._memory.observe_offline(k, wanted) is EpisodeTransition.ENTERED:
            out.append(
                event(EventKind.OFFLINE, f"NS data is unavailable, {CHECK_APP}.", now.isoformat())
            )
        return out

    async def watchdog_once(self) -> None:
        now = self._clock.now()
        self._writer.retry()  # the waiting changes, written again (rule 22)
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]] = {}
        for k, s in self._writer.st.occurrences.items():
            ctx = self._ctx(s, now)
            if ctx is None or s.not_travelling:
                continue
            t = on_tick(s, ctx)
            events = list(t.events)
            if not is_resolved(t.state):
                events += self._health_events(k, t.state, ctx, now)
            if t.state != s or events:
                changes[k] = (t.state, events)
        before = self._writer.st.occurrences
        # a window closing is itself no change
        self._writer.propose(self._archived(self._writer.st, now))
        if changes:
            self._apply(changes, kind=Change.TICK)
        elif self._writer.st.occurrences != before:
            self._writer.commit(self._writer.st, Change.TICK)
        await self._deliver()

    def _recovery_notice(
        self,
        stored: PersistedState,
        state: OccurrenceState,
        old: SelectedRecovery | None,
        selected: SelectedRecovery,
    ) -> PersistedState:
        if selected.status != "active":
            return stored
        changed = old is None or (old.monitoring_issue, old.revision) != (
            selected.monitoring_issue,
            selected.revision,
        )
        if not changed or (
            selected.monitoring_issue == "current"
            and (old is None or old.monitoring_issue == "current")
        ):
            return stored
        context = self._ctx(state, self._clock.now())
        if context is None:
            return stored
        kind = (
            EventKind.RECOVERY_UPDATE
            if selected.monitoring_issue == "changed"
            else EventKind.RECOVERY_MONITORING
        )
        text = (
            selected.monitoring_error
            or "NS recovery monitoring is current again. Progress still needs your confirmations."
        )
        event = make_event(
            context,
            kind,
            Severity.SILENT if selected.monitoring_issue == "current" else Severity.LOUD,
            text,
            key_facts=f"recovery:{selected.id}:{selected.revision}:{selected.monitoring_issue}:{selected.monitoring_revision}",
            stated=AlertFacts(
                recovery_selection=selected.id,
                recovery_revision=selected.revision,
                recovery_issue=f"{selected.monitoring_issue}:{selected.monitoring_revision}",
            ),
        )
        recorded, obligations = self._record(
            state, (event,), self._clock.now(), False, stored.occurrences
        )
        return replace(
            stored,
            occurrences={**stored.occurrences, okey(state.key): recorded},
            obligations=(*stored.obligations, *obligations),
        )

    def arrival_records(self) -> tuple[JourneyRecord, ...]:
        """Saved destinations and explicit arrivals, including archived occurrences."""
        return tuple(
            sorted(
                self._writer.st.journey_records.values(),
                key=lambda record: (
                    record.key.service_date,
                    record.key.profile_id,
                    record.key.direction.value,
                ),
                reverse=True,
            )
        )

    async def confirm_arrival(
        self,
        key: OccurrenceKey,
        destination: str,
        arrived_at: datetime,
        *,
        expected_revision: int,
        action_id: str,
        original_planned_arrival: datetime | None = None,
    ) -> JourneyRecord | Refused:
        stored = self._writer.st
        record = stored.journey_records.get(okey(key))
        if record is None:
            return Refused("No saved journey reference exists for that service date.")
        try:
            updated = confirm(
                record,
                destination,
                arrived_at,
                now=self._clock.now(),
                expected_revision=expected_revision,
                action_id=action_id,
                original_planned_arrival=original_planned_arrival,
            )
        except ArrivalError as error:
            return Refused(str(error))
        if updated == record:
            return record
        obligations = stored.obligations
        profile = find_profile(stored.config, key.profile_id)
        if profile is not None and updated.reminder_due(profile.alerts.compensation_reminder):
            now = self._clock.now()
            event = AlertEvent(
                profile.id,
                profile.name,
                key.direction,
                key.service_date,
                None,
                EventKind.COMPENSATION_REMINDER,
                Severity.SILENT,
                None,
                arrival_text(updated),
                f"arrival:{okey(key)}:{updated.revision}",
                now,
                AlertFacts(arrival_record=okey(key), arrival_revision=updated.revision),
            )
            obligation = replace(
                self._obligation(event, None, now), expires_at=now + timedelta(days=7)
            )
            obligations = (*obligations, obligation)
            updated = updated.queue_reminder()
        candidate = replace(
            stored,
            journey_records={**stored.journey_records, okey(key): updated},
            obligations=obligations,
        )
        if not self._writer.commit_strict(candidate):
            return Refused("Arrival was not saved. Free storage and review this action again.")
        await self._deliver()
        return updated

    async def prepare_recovery(
        self, key: OccurrenceKey, proposal_id: str, origin_place_id: str
    ) -> RecoveryOffer | Refused:
        return await self._recovery.prepare(key, proposal_id, origin_place_id)

    async def select_recovery(
        self,
        key: OccurrenceKey,
        offer_id: str,
        *,
        acknowledge_planned_times: bool = False,
        acknowledge_accessibility_unknown: bool = False,
    ) -> SelectedRecovery | Refused:
        return self._recovery.select(
            key,
            offer_id,
            acknowledge_planned_times=acknowledge_planned_times,
            acknowledge_accessibility_unknown=acknowledge_accessibility_unknown,
        )

    async def confirm_recovery_step(
        self,
        key: OccurrenceKey,
        selection_id: str,
        revision: int,
        step_index: int,
        kind: str,
        place_id: str | None,
        action_id: str,
    ) -> SelectedRecovery | Refused:
        return self._recovery.confirm(
            key, selection_id, revision, step_index, kind, place_id, action_id
        )

    async def stop_recovery(
        self, key: OccurrenceKey, selection_id: str, revision: int
    ) -> SelectedRecovery | Refused:
        state = self._writer.st.occurrences.get(okey(key))
        if state is None or state.selected_recovery is None:
            return Refused("No selected recovery is available.")
        return self._recovery.confirm(
            key, selection_id, revision, state.selected_recovery.index, "stop", None, uuid4().hex
        )

    async def recovery_idle(self) -> None:
        await self._recovery.idle()

    # --- user actions and location ---

    async def act(
        self, profile_id: str, action: UserAction, direction: Direction | None = None
    ) -> Refused | None:
        """`choose`, `leaving` and `not travelling` through the serialised writer (7.13).
        `direction`: only that direction's occurrences are targeted (both share a date).
        Refused when nothing was targeted: an unknown profile or no occurrence to act on. A
        removed profile still running on an occurrence is known (rule 10)."""
        now = self._clock.now()
        self._create_occurrences(now)
        mine = [
            s for s in self._writer.st.occurrences.values()
            if s.key.profile_id == profile_id and direction in (None, s.key.direction)
        ]  # fmt: skip
        target = target_occurrence(mine, action)
        p = None if target is None else self._profile_for(target)
        if target is None or p is None:
            return no_journey(display_name(self._shown_name(profile_id)))
        keys = [target.key]
        if isinstance(action, NotTravelling) and action.whole_day:
            keys = list(whole_day_keys(p, target.key))  # its own route revision's day_offset
        if isinstance(action, NotTravelling):  # written with the skip: an archived occurrence
            self._queue.drop_warnings(keys)  # would no longer read as skipped
        cfg = self._writer.st.config
        revision = cfg.revision if cfg else target.route_revision
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]] = {}
        for key in keys:
            k = okey(key)
            s = self._writer.st.occurrences.get(k) or frozen(
                self._writer.st, fresh_state(key, revision, None)
            )
            ctx = self._ctx(s, now)
            if ctx is not None:
                t = apply_action(s, ctx, action)
                changed = t.state
                if isinstance(action, NotTravelling):
                    self._recovery.invalidate(key)
                    recovery = changed.selected_recovery
                    if recovery is not None and recovery.status == "active":
                        changed = replace(
                            changed,
                            selected_recovery=replace(
                                recovery,
                                status="stopped",
                                ended_at=now,
                                revision=recovery.revision + 1,
                            ),
                        )
                changes[k] = (changed, t.events)
        self._apply(changes, kind=Change.ACTION)
        await self._deliver()
        return None

    async def leaving(
        self, profile_id: str, service_date: date | None = None, direction: Direction | None = None
    ) -> Refused | None:
        """`act` with a `Leaving` tapped now, by the runtime's own clock."""
        return await self.act(profile_id, Leaving(service_date, self._clock.now()), direction)

    async def not_travelling(
        self,
        profile_id: str,
        service_date: date | None = None,
        direction: Direction | None = None,
        *,
        whole_day: bool = False,
    ) -> Refused | None:
        """`act` with a `NotTravelling`."""
        return await self.act(profile_id, NotTravelling(service_date, whole_day), direction)

    def _shown_name(self, profile_id: str) -> str:
        """The name `view` shows for the profile: a removed one keeps its newest occurrence's."""
        p = find_profile(self._writer.st.config, profile_id)
        if p is None:
            mine = [
                s for s in self._writer.st.occurrences.values() if s.key.profile_id == profile_id
            ]
            newest = max(mine, key=lambda s: s.key.service_date, default=None)
            p = None if newest is None else self._profile_for(newest)
        return profile_id if p is None else p.name

    async def choose_at(
        self,
        profile_id: str,
        departure: time,
        train_number: str | None = None,
        service_date: date | None = None,
        direction: Direction | None = None,
    ) -> Refused | None:
        """A train named by its clock time (shells): the core finds the occurrence, the station
        and the connection, with one live request under the shared budget (rule 14). A refusal
        of the choice itself is also pushed, loudly."""
        now = self._clock.now()
        self._create_occurrences(now)
        cands = sorted(
            (
                s for s in self._writer.st.occurrences.values()
                if s.key.profile_id == profile_id and not is_resolved(s)
                and service_date in (None, s.key.service_date)
                and direction in (None, s.key.direction)
            ),
            key=lambda s: s.key.service_date,
        )  # fmt: skip
        named = find_profile(self._writer.st.config, profile_id) or next(
            (p for s in cands if (p := self._profile_for(s)) is not None), None
        )
        gone = no_journey(display_name(named.name if named else profile_id))
        target = choice_target(
            cands, departure, train_number, service_date, now, self._writer.st, gone
        )
        if isinstance(target, Refused):
            return target
        occ = target.occ
        k = okey(occ.key)
        # rule 21: what the choice is judged on must still hold when it is written
        request = OccurrenceRequest.capture(occ)
        answers = await self._fetch_choice(target, k)
        if isinstance(answers, Refused):
            return answers
        ranked = named_connections(answers[0], target, departure, train_number)
        if isinstance(ranked, Refused):
            return ranked
        # rule 21: a reload during the request may have removed or archived the occurrence
        now_occ = self._writer.st.occurrences.get(k)
        if now_occ is None or is_resolved(now_occ):
            return gone
        if request.phase_for(now_occ) is ResultPhase.STALE:
            return Refused("your journey changed while NS answered; try again", RefusalKind.RETRY)
        choices = [Choose(occ.key.service_date, c, answers) for c in ranked]
        d = occ.key.direction
        why = [self._choice_refusal(profile_id, d, c, gone) for c in choices]
        # the earliest arrival that passes; when none does, the earliest is refused loudly
        i = next((n for n, r in enumerate(why) if r is None), 0)
        return why[i] if await self.act(profile_id, choices[i], d) is None else gone

    async def _fetch_choice(self, t: ChoiceTarget, k: str) -> tuple[ProviderAnswer, ...] | Refused:
        """The answer naming the train first, then what judges the change to it: from a transfer
        station, a fresh observation of the kept incoming leg, never an older poll's answer
        (rule 1). One deadline for every request: the shell waits 120 s in all."""
        ctx = t.ctx
        end = ctx.schedule.window_end
        deadline = to_utc(self._clock.now()) + POLL_DEADLINE
        asks: list[Sequence[Query]] = [
            (
                Query.for_profile(
                    ctx.profile, t.station, ctx.profile.destination(ctx.direction), t.when
                ),
            )
        ]
        if t.station != ctx.profile.origin(ctx.direction):
            asks.insert(0, queries_needed(t.occ, ctx))
        out: tuple[ProviderAnswer, ...] = ()
        for queries in asks:
            got = await self._gate.all_trips(queries, True, end, deadline)
            self._track_key_rejection(got if isinstance(got, str) else None, k, self._clock.now())
            if isinstance(got, str):
                await self._deliver()
                return Refused(f"NS did not answer ({got}); try again", RefusalKind.RETRY)
            out = (*got, *out)
        return out

    def _choice_refusal(
        self, profile_id: str, d: Direction, choice: Choose, gone: Refused
    ) -> Refused | None:
        """The check `act` applies, ridden prefix included, as a dry run (rule 1)."""
        mine = [
            s for s in self._writer.st.occurrences.values()
            if s.key.profile_id == profile_id and s.key.direction is d
        ]  # fmt: skip
        t = target_occurrence(mine, choice)
        ctx = None if t is None else self._ctx(t, self._clock.now())
        if t is None or ctx is None:
            return gone
        events = apply_action(t, ctx, choice).events
        return next((Refused(e.text) for e in events if e.kind is EventKind.CHOOSE_REFUSED), None)

    async def report_location(
        self,
        profile_id: str,
        event: LocationEvent,
        source: LocationSettings | None = None,
        epoch: int | None = None,
    ) -> bool:
        """Every unresolved occurrence of the profile gets the event with a fresh origin answer;
        False when NS failed one of those requests, so the event was judged without it.
        With `source` (the settings the event was read under), a profile whose location settings
        differ by the time it would be written drops the event: a switched, disabled or replaced
        source supplies no evidence (8). With `epoch` (`RuntimeView.location_epochs` when the fix
        arrived), any location change since then drops it too, a switch back included."""

        def stale(ctx: Ctx) -> bool:
            if epoch is not None and epoch != self._loc_epoch.get(profile_id, 0):
                return True
            return source is not None and ctx.profile.location != source

        answered = True
        mine = [k for k, s in self._writer.st.occurrences.items() if s.key.profile_id == profile_id]
        for k in mine:
            s = self._writer.st.occurrences.get(k)
            now = self._clock.now()
            ctx = None if s is None else self._ctx(s, now)
            if s is None or ctx is None or is_resolved(s) or stale(ctx):
                continue
            gen, p, d = s.generation, ctx.profile, ctx.direction
            got = await self._gate.trips(
                Query.for_profile(p, p.origin(d), p.destination(d), now), True, None
            )
            qs = location_queries(s, ctx, event)  # the locked trains, from where they leave
            more = await self._gate.all_trips(qs, True, None) if qs else ()
            failed = got if isinstance(got, str) else None
            answer = None if isinstance(got, str) else got
            failed_more = more if isinstance(more, str) else None
            watched = () if isinstance(more, str) else more
            now = self._clock.now()
            # a rejected key on either request is reported; a healthy first answer clears an outage
            self._track_key_rejection("auth" if failed_more == "auth" else failed, k, now)
            # re-read after the request: an occurrence skipped, archived or edited meanwhile is
            # never written back from the old copy (rule 21)
            s = self._writer.st.occurrences.get(k)
            ctx = None if s is None else self._ctx(s, now)
            if s is None or ctx is None or is_resolved(s) or stale(ctx):
                continue
            answered = answered and failed is None and failed_more is None
            if s.generation != gen:
                # an answer asked under another generation is stale; the report itself still counts
                answer, watched = None, ()
            t = on_location(s, ctx, event, answer, watched)
            self._apply({k: (t.state, t.events)})
        await self._deliver()
        return answered

    # --- reload (5.2, rule 10) ---

    def _refuse(self, errors: Sequence[ProfileProblem] | str, now: datetime) -> None:
        reason = errors if isinstance(errors, str) else _reasons(errors)
        self._reload.reject(reason)
        if not self._writer.store_ok and reason == self._reload.announced:
            return  # the same file retried while its write fails (rule 22): told once
        self._reload.announced = reason
        self._app_warning(EventKind.PROFILE_REFUSED, f"profile change refused: {reason}.", now)

    async def reload(self) -> None:
        request = self._reload.begin()
        now = self._clock.now()
        applied: Config | None = None
        try:
            # off the loop (rule 12); a view taken during the read keeps the old refusal
            text = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            if not self._reload.current(request):
                return
            self._refuse(f"cannot read {self._path.name} ({e})", now)
        else:
            if not self._reload.current(request):
                return
            self._reload.refusal = None
            old, today = self._writer.st.config, now.astimezone(TZ).date()
            if self._require_stations and self.known_stations is None:
                await self._fetch_stations()
                if not self._reload.current(request):
                    return
                old = self._writer.st.config
            cfg, errors = load_config(
                text, self.known_stations, old, today, available_channels=self.available_channels
            )
            unchecked = self._require_stations and self.known_stations is None
            if unchecked and cfg is not None and cfg is not old:
                # 5.3: an edit (or a first configuration, even one with a refused profile) whose
                # codes cannot be checked waits; the last valid configuration keeps running (5.2)
                reason = (
                    "the NS station list is unavailable, so station codes cannot be checked;"
                    " the last valid configuration keeps running and the change is tried again"
                )
                if errors:
                    reason = f"{_reasons(errors)}; {reason}"
                if request.pending is not PendingEditPhase.STATIONS:
                    self._refuse(reason, now)
                self._reload.reject(reason, waiting_stations=True)
            else:
                self._reload.phase = ReloadPhase.READY
                self._reload.pending = PendingEditPhase.NONE
                if errors:
                    self._refuse(errors, now)
                if cfg is not None and cfg is not old:
                    self._apply_config(cfg, now)
                    applied = cfg
        self._apply({})
        if applied is not None and self._writer.waiting:
            self._mtime = None  # not written, so not active: the file watch tries again (r22)
        await self._deliver()

    def _stations_due(self) -> bool:
        last = self._stations_tried
        gap = timedelta(seconds=POLL_EVERY_S)
        return last is None or to_utc(self._clock.now()) - to_utc(last) >= gap

    async def _fetch_stations(self) -> None:
        """At most once per poll interval: the station list through the shared budget."""
        if not self._stations_due():
            return
        self._stations_tried = self._clock.now()
        got = await self.stations()
        if got:  # an empty list checks nothing
            self.known_stations = got
            if self._on_stations is not None:
                self._on_stations(got)

    def _apply_config(self, new: Config, now: datetime) -> None:
        self._recovery.invalidate()
        old, today = self._writer.st.config, now.astimezone(TZ).date()
        was = {p.id: p.location for p in (old.profiles if old else ())}
        now_loc = {p.id: p.location for p in new.profiles}
        for pid in was.keys() | now_loc.keys():
            if was.get(pid) != now_loc.get(pid):
                self._loc_epoch[pid] = self._loc_epoch.get(pid, 0) + 1
        snaps = {**self._writer.st.config_snapshots, new.revision: new}
        occs = {}
        for k, s in self._writer.st.occurrences.items():
            # its schedule, read before the history is re-seeded below
            s = frozen(self._writer.st, s)
            route = find_profile(snaps.get(s.route_revision) or old, s.key.profile_id)
            p = find_profile(new, s.key.profile_id)
            decision = route_decision(
                s, route is not None and p is not None and same_route(route, p)
            )
            if decision is RouteDecision.ADOPT:
                rev = new.revision
            elif decision is RouteDecision.RETAIN:
                rev = s.route_revision  # locked or riding: runs on its route revision (rule 10)
            else:  # not locked yet: created again from the new route when its window opens
                self._memory.forget(k)  # what was seen of the old route is no evidence for it
                continue
            moved = replace(s, route_revision=rev, settings_revision=new.revision)
            if effective_profile(snaps, new, moved) != self._profile_for(s):
                moved = replace(moved, generation=s.generation + 1)  # in-flight results are stale
            occs[k] = moved
        baselines, ledgers = _rebased(self._writer.st, old, new, today)
        self._writer.propose(replace(
            self._writer.st, config=new, config_snapshots=snaps, occurrences=occs,
            baselines=baselines, lookahead=ledgers,
        ))  # fmt: skip

    async def reload_if_changed(self) -> None:
        """The file watch: reload when the file's mtime changes (or it disappears)."""
        try:
            mtime: int | None = (await asyncio.to_thread(self._path.stat)).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._mtime or (
            self._reload.pending is PendingEditPhase.STATIONS and self._stations_due()
        ):
            self._mtime = mtime
            await self.reload()
        elif self._require_stations and self.known_stations is None and self._stations_due():
            # nothing waits, but station zones need the list (8): a failed fetch is retried (7.11)
            await self._fetch_stations()
            self._apply({})
            await self._deliver()

    # --- look-ahead (7.10, rule 21) ---

    async def lookahead_once(self) -> None:
        cfg = self._writer.st.config
        if cfg is None:
            return
        works = await self._fetch_works()
        if works is None:  # refused by the shared cooldown: retried by `daily`
            return
        for pid, d in [(p.id, d) for p in cfg.profiles for d in Direction]:
            # the profile as configured now: one held across an earlier direction's requests
            # could be stamped with a newer revision than its own (rule 21)
            p = find_profile(self._writer.st.config, pid)
            if p is not None:
                await self._lookahead_direction(p, d, works)
        await self._deliver()

    async def _listing[T: (PlannedWorks, frozenset[str])](
        self, ask: Callable[[], Awaitable[T]], who: str
    ) -> T | PollFailure | None:
        """A list from the provider under the shared budget, a rejected key reported as `who`;
        None when the budget refuses (an accepted key ends the outage, 7.11)."""
        now = self._clock.now()
        got = await self._gate.listing(ask, who)
        if got is not None:
            self._track_key_rejection(got if isinstance(got, str) else None, who, now)
        return got

    async def _fetch_works(self) -> PlannedWorks | None:
        """None only when the shared budget refuses; a failed lookup is coverage `unknown`, so
        the probes still run and none is clean (7.10)."""
        got = await self._listing(self._provider.planned_works, "lookahead")
        return PlannedWorks((), "unknown") if isinstance(got, str) else got

    async def stations(self) -> frozenset[str] | None:
        """The NS station list through the shared budget (rule 14); None when the shared budget
        refuses or the request fails."""
        got = await self._listing(self._provider.stations, "stations")
        return None if isinstance(got, str) else got

    async def _lookahead_direction(self, p: Profile, d: Direction, works: PlannedWorks) -> None:
        start = self._clock.now()

        async def probe(queries: Sequence[Query]) -> tuple[ProviderAnswer, ...] | PollFailure:
            got = await self._gate.all_trips(queries, False, None)
            self._track_key_rejection(got if isinstance(got, str) else None, "lookahead", start)
            return got

        run = await probe_direction(self._writer.st, p, d, works, start, probe)
        st = None if run is None else apply_run(self._writer.st, self._queue, p, d, run)
        if st is not None:
            self._writer.commit(st, Change.LOOKAHEAD)

    # --- start, reconcile and the loops ---

    async def start(self) -> None:
        now = self._clock.now()
        self._lifecycle.begin(now, asyncio.current_task())
        try:
            await self._load_and_reconcile(now)
            self._lifecycle.reconciled()
            self._apply({})
            await self._deliver()
            self._lifecycle.running()
        except BaseException:
            self._lifecycle.phase = RuntimePhase.FAILED
            self._lifecycle.reconcile_started_at = None
            raise
        self._refresh_vehicles_soon()
        self._refresh_digest_soon()
        self._recovery.refresh_soon()

    async def _load_and_reconcile(self, now: datetime) -> None:
        lost = None
        try:
            loaded = self._store.load()
        except StorageError as e:
            loaded, lost = None, str(e)
        st = self._writer.st
        if loaded is not None:
            st = _restarted(loaded)
            self._queue.restore(loaded, preserve=lambda o: not self._routing_ready_in(loaded, o))
        self._writer.begin(replace(st, started_before=True))
        if lost is not None:  # rule 22: fresh state, loud, and a write clears "state lost"
            self._app_warning(EventKind.STORAGE_FAILED, f"saved state was lost ({lost}).", now)
        try:
            self._mtime = self._path.stat().st_mtime_ns
        except OSError:
            self._mtime = None
        await self.reload()
        self._create_occurrences(now)
        # deterministic reconcile: one fresh poll per unarchived occurrence (rule 11)
        await self._poll_occurrences()

    async def run_forever(self, *, started: bool = False) -> None:
        """Starts (unless `started`) with the watchdog already running, so a slow reconcile is
        watched too (rule 12); every loop keeps fixed start times, a slow step eats into its own
        interval instead of adding to it (7.1)."""
        owner = present(asyncio.current_task())
        self._lifecycle.claim_runner(started, owner)
        c = self._clock

        async def daily() -> None:
            """Today's run once the look-ahead time (LOOKAHEAD_AT by default) has passed, also
            after a start past it; retried each poll interval until every direction's run has
            written (7.10)."""
            while True:
                now = self._clock.now().astimezone(TZ)
                six = resolve_local(now.date(), self._lookahead_at, edge="start")
                if to_utc(six) <= to_utc(now) and lookahead_done(self._writer.st, six):
                    six = resolve_local(
                        now.date() + timedelta(days=1), self._lookahead_at, edge="start"
                    )
                if to_utc(six) > to_utc(now):
                    await self._clock.sleep((to_utc(six) - to_utc(now)).total_seconds())
                    continue
                try:
                    await self.lookahead_once()
                except Exception:
                    log.exception("look-ahead failed")
                if not lookahead_done(self._writer.st, six):
                    await self._clock.sleep(POLL_EVERY_S)

        async def main() -> None:
            if not started:
                self._lifecycle.initialize(owner, present(asyncio.current_task()))
                await self.start()
            await _run_together(
                every(c, POLL_EVERY_S, self.poll_once),
                every(c, FILE_EVERY_S, self.reload_if_changed),
                daily(),
            )

        # main first: start() loads the store before its first await, so the watchdog never
        # judges the empty state
        try:
            await _run_together(main(), every(c, WATCHDOG_EVERY_S, self.watchdog_once))
        finally:
            self._lifecycle.phase = RuntimePhase.STOPPING
            try:
                try:
                    await self._queue.stop()
                finally:
                    await self.stop_vehicle_context()
                    await self.stop_works_digest()
                    await self._recovery.stop()
                    self._lifecycle.phase = RuntimePhase.STOPPED
            finally:
                self._lifecycle.release_runner(owner)
