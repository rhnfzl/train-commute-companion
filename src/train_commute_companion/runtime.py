"""The runtime around the pure core: polling, the independent watchdog, the shared request
budget, look-ahead, transactional reload and the restart reconcile (7.1, 7.11, 7.12 rules 5,
10, 11, 12, 14, 21, 22, 5.2, 7.13). The pure modules take time and data as arguments; I/O lives
here, in store.py, providers/ns.py, webhook_send.py, cli.py and replay.py."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from train_commute_companion.advice import (
    advice_query,
    advise,
    usual_arrival,
)
from train_commute_companion.budget import POLL_EVERY_S, RequestBudget
from train_commute_companion.calendar import TZ, resolve_local, to_utc
from train_commute_companion.choice import ChoiceTarget, choice_target, named_connections
from train_commute_companion.classify import current_run, evidence, find_leg
from train_commute_companion.delivery import (
    deliver,
    pending_journey_warnings,
)
from train_commute_companion.health import (
    PollFailure,
    PollRecord,
    WatchedLeg,
    blindness,
    board_health,
    offline,
)
from train_commute_companion.lifecycle import (
    Choose,
    OccurrenceState,
    PollAnswers,
    UserAction,
    apply_action,
    is_resolved,
    location_queries,
    may_archive,
    merged_pool,
    on_location,
    on_poll,
    on_tick,
    origin_answer,
    queries_needed,
    target_occurrence,
    whole_day_keys,
)
from train_commute_companion.location import LocationEvent
from train_commute_companion.lookahead import forget_pending, rebase_on_edit, reset_on_edit
from train_commute_companion.lookahead_probe import BaselineHistory, LookAheadLedger, service_date
from train_commute_companion.lookahead_run import applied_run, lookahead_done, probe_direction
from train_commute_companion.match import usual_connection
from train_commute_companion.messages import CHECK_APP, no_journey_text
from train_commute_companion.model import (
    LIVE_PREFIX,
    SENT_PREFIX,
    AlertEvent,
    Config,
    Ctx,
    Direction,
    DirectionState,
    EventKind,
    Leaving,
    LegState,
    LocationSettings,
    NotTravelling,
    Obligation,
    Observation,
    OccurrenceKey,
    Profile,
    ProfileProblem,
    Provenance,
    Severity,
    TrainId,
    direction_key,
    display_name,
    okey,
    present,
)
from train_commute_companion.news import Fact, all_good, escalations, severity, unsent
from train_commute_companion.notify import Notifier, WebhookPort
from train_commute_companion.profile import load_config
from train_commute_companion.providers.base import (
    AuthError,
    MalformedAnswer,
    PlannedWorks,
    Pool,
    Provider,
    ProviderAnswer,
    ProviderError,
    Query,
    RateLimited,
)
from train_commute_companion.revalidation import (
    OBSERVED,
    LiveLedger,
    PollMemory,
    alert_facts,
)
from train_commute_companion.revisions import (
    effective_profile,
    find_profile,
    frozen,
    occurrence_ctx,
    profile_for,
    same_route,
    signature_for,
)
from train_commute_companion.schedule import schedules_active
from train_commute_companion.state_writer import StateWriter
from train_commute_companion.store import PersistedState, StorageError, Store
from train_commute_companion.view import RuntimeView, runtime_view
from train_commute_companion.webhook_queue import CopyQueue

log = logging.getLogger(__name__)

POLL_DEADLINE = timedelta(seconds=90)
WATCHDOG_EVERY_S = 30
FILE_EVERY_S = 10
LOOKAHEAD_AT = time(6, 0)
_APP = "app"  # profile name on application warnings, which belong to no profile


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(TZ)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _reasons(errors: Sequence[ProfileProblem]) -> str:
    return "; ".join(f"{e.path}: {e.message}" for e in errors)


def fresh_state(key: OccurrenceKey, revision: int, baseline: int | None) -> OccurrenceState:
    return OccurrenceState(
        key, 0, revision, revision, baseline, DirectionState.IDLE, None, (), False, None,
        False, None, False, (), False, False,
    )  # fmt: skip


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
        a = replace(a, delivered=True) if sent else replace(a, withdrawn=True)
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
        for x in s.alert_keys:
            if x.startswith(SENT_PREFIX):
                keys.setdefault(LIVE_PREFIX + x[len(SENT_PREFIX) :], "1")
        occs[k] = frozen(st, replace(s, alert_keys=keys))
    return replace(st, occurrences=occs)


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


def _forget_ended(keys: dict[str, str], events: Sequence[AlertEvent]) -> None:
    emitted = {e.key for e in events}
    for k in [k for k in keys if k.startswith(LIVE_PREFIX)]:
        if k[len(LIVE_PREFIX) :] not in emitted:
            keys.pop(k)
            keys.pop(SENT_PREFIX + k[len(LIVE_PREFIX) :], None)
    keys.update({LIVE_PREFIX + k: "1" for k in emitted})


def _news(keys: Mapping[str, str], events: Sequence[AlertEvent], now: datetime) -> list[AlertEvent]:
    """The events not told yet, and the told silent facts that now go loud."""
    sent = {
        k[len(SENT_PREFIX) :]: Severity(v) for k, v in keys.items() if k.startswith(SENT_PREFIX)
    }
    out = list(unsent(sent, events))
    facts = [
        Fact(e.key, e.kind, e.train.planned_departure if e.train else None)
        for e in events
        if sent.get(e.key) is Severity.SILENT
    ]
    loud = set(escalations(sent, facts, now))
    # a known fact that turned loud itself (an arrival now past the limit) goes once more
    loud |= {e.key for e in events if e.severity is Severity.LOUD}
    out += [
        replace(e, severity=Severity.LOUD)
        for e in {e.key: e for e in events}.values()
        if e.key in loud and sent.get(e.key) is Severity.SILENT
    ]
    return out


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
    ) -> None:
        self._path = profile_path
        self._queue = CopyQueue(
            webhooks,
            clock.now,
            self._save_rest,
            self._waits,
            lambda held: self._held_ledger if held else self._ledger,
        )
        self._writer = StateWriter(store, self._queue, self._storage_note)
        self._lookahead_at = lookahead_at
        self._refusal: str | None = None
        self._started = False
        self._store = store
        self._provider = provider
        self.events_log: list[AlertEvent] = []
        self._notifier = _LoggingNotifier(notifier, self.events_log)
        self._clock = clock
        self.known_stations = known_stations
        self._budget = RequestBudget()
        self._refused_told: str | None = None
        self._mtime: int | None = None
        self._auth_outage = False
        self._auth_told: set[str] = set()
        self._memory = PollMemory()
        self._sending = asyncio.Lock()
        self.requests = 0  # NS publishes no quota (7.1): counted and logged
        # set while start() reconciles: messages stored before it wait for the fresh poll
        self._since: datetime | None = None

        def read() -> PersistedState:
            return self._writer.st

        self._ledger = LiveLedger(read, read, self._memory, clock.now)
        self._held_ledger = LiveLedger(read, self._writer.judged, self._memory, clock.now)
        # an edit waiting for the station list retries it under the shared budget
        self._require_stations = require_stations
        self._on_stations = on_stations
        self._unchecked = False
        self._stations_tried: datetime | None = None
        # profile id -> location source epoch, bumped on every accepted location change (8):
        # an event read under an older epoch is dropped, even after a switch back (A, B, A)
        self._loc_epoch: dict[str, int] = {}

    # --- configuration and context ---

    @property
    def config(self) -> Config | None:
        return self._writer.st.config

    @property
    def state(self) -> PersistedState:
        return self._writer.st

    @property
    def positions(self) -> Mapping[str, tuple[float, float]]:
        return self._provider.positions

    @property
    def refusal(self) -> str | None:
        """Why the last reload refused the file (or waits for the station list); None if not."""
        return self._refusal

    @property
    def store_ok(self) -> bool:
        return self._writer.store_ok

    @property
    def key_rejected(self) -> bool:
        return self._auth_outage

    @property
    def started(self) -> bool:
        return self._started

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
        return runtime_view(
            self._writer.st,
            self._memory.boards,
            now,
            lambda s: self._ctx(s, now),
            started=self._started,
            refusal=self._refusal,
            store_ok=self._writer.store_ok,
            key_rejected=self._auth_outage,
            undelivered=self._writer.undelivered(),
            requests=self.requests,
            location_epochs=self._loc_epoch,
        )

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
        self._writer.st = replace(self._writer.st, obligations=(*self._writer.st.obligations, o))

    def _record(
        self, s: OccurrenceState, events: Sequence[AlertEvent], now: datetime, polled: bool
    ) -> tuple[OccurrenceState, list[Obligation]]:
        """News (7.5) and escalation (rule 4) against the occurrence's delivery ledger.
        `polled`: the events are everything a healthy poll reports, so a reported fact it no
        longer reports has ended and leaves the ledger (a recurrence is news again)."""
        keys = dict(s.alert_keys)
        if polled:
            _forget_ended(keys, events)
        out = _news(keys, events, now)
        if not out:
            return (s if keys == s.alert_keys else replace(s, alert_keys=keys)), []
        twins = [
            x for x in self._writer.st.occurrences.values()
            if x.key != s.key and x.key.profile_id == s.key.profile_id
            and x.key.direction is s.key.direction and not is_resolved(x)
        ]  # fmt: skip
        obligations = []
        for e in out:
            keys[SENT_PREFIX + e.key] = e.severity.value
            if twins:  # 7.13: alerts name the service date while two occurrences exist
                note = f" (service date {s.key.service_date.isoformat()})"
                e = replace(e, text=e.text + note, facts=replace(alert_facts(e), twin_note=note))
            obligations.append(self._obligation(e, s, now))
        return replace(s, alert_keys=keys), obligations

    def _apply(
        self,
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]],
        polled: bool = False,
        keep: bool = False,
        stage: bool = True,
    ) -> None:
        now = self._clock.now()
        occs = dict(self._writer.st.occurrences)
        obligations = list(self._writer.st.obligations)
        for k, (s, events) in changes.items():
            occs[k] = s  # twins see the occurrences written so far
            self._writer.st = replace(self._writer.st, occurrences=occs)
            s, new = self._record(s, events, now, polled)
            occs[k] = s
            obligations += new
        self._writer.st = replace(self._writer.st, occurrences=occs, obligations=tuple(obligations))
        self._archive(now)
        self._writer.save(keep, stage)

    def _archive(self, now: datetime) -> None:
        """Rule 6: an occurrence leaves the store once it ended and its pending journey warnings
        were delivered; it stays until its window closed so it is not created again."""
        occs = {}
        for k, s in self._writer.st.occurrences.items():
            ctx = self._ctx(s, now)
            closed = ctx is None or to_utc(now) >= to_utc(ctx.schedule.window_end)
            if closed and may_archive(
                s, now, pending_journey_warnings(self._writer.st.obligations, s.key)
            ):
                self._memory.forget(k)
                continue
            occs[k] = s
        # snapshots are pruned on write
        self._writer.st = replace(self._writer.st, occurrences=occs)

    async def _deliver(self) -> None:
        """Every pending obligation, revalidated right before sending (rule 5). Merges by id,
        so obligations added by another task while a send awaited are kept. Serialised: the
        poll, watchdog and file watch all deliver, and two passes would send one alert twice.
        The webhook copies go after, in a task of their own: a slow receiver never holds up a
        phone send or the loop that delivers (an alert's advice can expire)."""
        async with self._sending:
            await self._deliver_pass()
        self._queue.soon()

    def _storage_note(self) -> Obligation:
        return self._app_obligation(
            EventKind.STORAGE_FAILED,
            "could not save state, alerts may repeat or be lost.",
            self._clock.now(),
            "",
        )

    def _save_rest(self) -> None:
        """Writes the webhook rests now, synchronously, before a try's request leaves (rule 22:
        a failed write takes the same path as any other)."""
        self._record_delivery(set(), {}, (), True)

    async def webhooks_idle(self) -> None:
        """Waits until no webhook pass runs (a clean shutdown, tests)."""
        await self._queue.idle()

    def _waits(self, o: Obligation) -> bool:
        """Not judged yet: written before this start (judged once the reconcile ran, rule 11),
        or judged on observations while its occurrence has no healthy answer (rule 5b)."""
        if self._since is not None and to_utc(o.event.created_at) < to_utc(self._since):
            return True
        k = None if o.occurrence is None else okey(o.occurrence)
        return (
            o.event.kind in OBSERVED
            and k in self._writer.st.occurrences
            and k not in self._memory.boards
        )

    def _due(self, o: Obligation) -> bool:
        w = self._writer
        if o.id in w.gone:
            return False  # finished while the store was down (rule 22)
        mem = o.id in w.held or o.id in w.notes
        if not mem and all(x.id != o.id for x in w.st.obligations):
            return False  # removed by a change written while an earlier send awaited
        return not self._waits(o)

    async def _deliver_pass(self) -> None:
        finished: set[str] = set()
        kept: dict[str, Obligation] = {}
        told: list[tuple[Obligation, bool]] = []  # finished LEAVE_NOW, sent (else dropped)
        w, port = self._writer, self._queue.port
        copies = False  # the webhook copies changed
        try:
            for o in (*w.st.obligations, *w.held.values(), *w.notes.values()):
                if not self._due(o):
                    continue
                if port is not None and not o.webhook_copied and o.id not in self._queue.copied:
                    copies |= self._queue.stage(o, o.id in w.held)
                # a fresh clock per message: a slow send must not leave the next one judged early
                v = self._held_ledger if o.id in w.held else self._ledger
                r, sent = await deliver(o, v, self._notifier, self._clock.now())
                if r is not None and port is not None and not r.webhook_copied:
                    r = replace(r, webhook_copied=True)
                if r is None and o.event.kind is EventKind.LEAVE_NOW:
                    told.append((o, sent))
                if w.settle(o, r, sent):
                    continue
                if r is None:
                    finished.add(o.id)
                elif r != o:
                    kept[o.id] = r
        finally:  # each acknowledged send is recorded, also when shutdown cancels the pass
            self._record_delivery(finished, kept, told, copies)

    def _record_delivery(
        self,
        finished: set[str],
        kept: Mapping[str, Obligation],
        told: Sequence[tuple[Obligation, bool]],
        copies: bool = False,
    ) -> None:
        if not finished and not kept and not told and not copies:
            return

        marks = set(self._queue.copied)  # written too: a phone send in flight is not in `kept` yet

        def outcome(st: PersistedState) -> PersistedState:
            # look-ahead messages live in their ledger too, so a delivered one counts as told
            ledgers = {
                k: replace(v, pending=_survivors(v.pending, finished, kept, marks))
                for k, v in st.lookahead.items()
            }
            st = _advice_told(st, told)
            obligations = _survivors(st.obligations, finished, kept, marks)
            return replace(st, obligations=obligations, lookahead=ledgers)

        self._writer.st = outcome(self._writer.st)
        self._archive(self._clock.now())
        w = self._writer
        w.finish(finished)
        mem = set(w.held) | set(w.notes)  # read before a landed write empties `held`
        if w.save(stage=False, op=outcome):
            self._queue.delivery_written(mem)

    # --- provider access under the shared budget ---

    def _update_budget(
        self, before: RequestBudget, error: BaseException | None, window_end: datetime | None
    ) -> None:
        """The shared budget after one request (rule 14). A success clears only the state it
        started under: an older request finishing late never clears a newer 429 cooldown."""
        if error is not None:
            self._budget = self._budget.on_result(error, self._clock.now(), window_end)
        elif self._budget is before:
            self._budget = RequestBudget()

    async def _ask(
        self, q: Query, left: float
    ) -> tuple[ProviderAnswer | None, PollFailure | None, BaseException | None]:
        try:
            return await asyncio.wait_for(self._provider.trips(q), left), None, None
        except TimeoutError:
            return None, "timeout", None
        except AuthError as e:
            return None, "auth", e
        except RateLimited as e:
            return None, "rate", e
        except MalformedAnswer as e:
            return None, "parse", e
        except ProviderError as e:
            return None, "provider", e

    async def _fetch(
        self,
        q: Query,
        live: bool,
        window_end: datetime | None,
        deadline: datetime | None = None,
    ) -> ProviderAnswer | PollFailure:
        """`deadline`: the poll's own hard deadline (7.1), shared by all its requests."""
        if not self._budget.allow(self._clock.now(), live):
            # skipped by the shared backoff: counts toward board health (7.1), and a rejected
            # key still reads as one through its cooldown (7.11)
            return self._budget.reason
        started = self._clock.now()
        deadline = deadline or to_utc(started) + POLL_DEADLINE
        left = (to_utc(deadline) - to_utc(started)).total_seconds()
        if left <= 0:
            return "timeout"
        before = self._budget
        self.requests += 1
        log.debug("request %d: %s", self.requests, q)
        answer, error, exc = await self._ask(q, left)
        now = self._clock.now()
        if error is None and to_utc(now) > to_utc(deadline):
            error = "timeout"  # a late answer never overwrites a newer one (7.1)
        if exc is not None or error is None:  # a timeout neither backs off nor resets it
            self._update_budget(before, exc, window_end)
        if error is not None:
            return error
        return present(answer)  # _ask gives an answer or a failure, never neither

    async def _fetch_all(
        self,
        queries: Sequence[Query],
        live: bool,
        window_end: datetime | None,
        deadline: datetime | None = None,
    ) -> tuple[ProviderAnswer, ...] | PollFailure:
        """All or nothing: a poll missing one answer cannot judge the legs it was for (rule 2).
        One hard deadline for the whole poll, not one per request (7.1)."""
        answers = []
        deadline = deadline or to_utc(self._clock.now()) + POLL_DEADLINE
        for q in queries:
            a = await self._fetch(q, live, window_end, deadline)
            if isinstance(a, str):
                return a
            answers.append(a)
        return tuple(answers)

    def _auth(self, error: PollFailure | None, who: str, now: datetime) -> None:
        """`key_rejected` once per outage and again when each window opens while it lasts."""
        if error is None:
            self._auth_outage, self._auth_told = False, set()
            return
        if error != "auth" or (self._auth_outage and who in self._auth_told):
            return
        self._auth_outage = True
        self._auth_told.add(who)
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
                v = hist.in_force(sch.key.service_date) if hist else None
                fresh = fresh_state(sch.key, cfg.revision, v.version if v else None)
                own = v.signature if v else None
                occs[k] = replace(
                    fresh,
                    baseline_signature=own,
                    baseline_frozen=True,
                    alert_keys=self._writer.marks.get(k, {}),  # told while the store was down (r22)
                )
        self._writer.st = replace(self._writer.st, occurrences=occs)

    async def poll_once(self) -> None:
        self._create_occurrences(self._clock.now())
        await self._poll_occurrences()
        await self._deliver()

    async def _poll_occurrences(self) -> None:
        for k in sorted(self._writer.st.occurrences):
            try:
                await self._poll_occurrence(k)
            except Exception:  # one failing occurrence must not starve the others (rule 12)
                log.exception("poll of %s failed", k)

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
        got = await self._fetch_all(queries_needed(s, ctx), True, end, deadline)
        error = got if isinstance(got, str) else None
        answers = () if isinstance(got, str) else got
        advice_error: PollFailure | None = None
        if error is None and (usual := origin_answer(answers, ctx)) is not None:
            # 7.6: an earlier catchable train is before the time the poll asked from; a failed
            # search leaves advice to the poll's own answers and never fails the poll
            q = advice_query(s, ctx, usual, usual_arrival(s, ctx, usual))
            extra = None if q is None else await self._fetch(q, True, end, deadline)
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
        self._auth(error, k, now)
        poll = PollAnswers(answers, (PollRecord(now, error is None, error),), gen)
        t = on_poll(s, ctx, poll)
        st, events = t.state, list(t.events)
        current = s.generation == gen
        if current:  # a choice since the poll started makes the answer stale
            st, events = self._judge_poll(k, s.lock is None, st, events, ctx, poll)
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
        if s.lock is None and not s.retained and k in self._memory.before:  # before a lock (rule 13)
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

        if blind and k not in self._memory.blind:
            self._memory.blind[k] = now
            text = f"cannot see your train in NS data ({reason}), {CHECK_APP}."
            out.append(event(EventKind.CANNOT_SEE_TRAIN, text, now.isoformat()))
        elif not blind and k in self._memory.blind:
            since = self._memory.blind.pop(k)
            text = "your train is visible in NS data again."
            out.append(event(EventKind.CAN_SEE_TRAIN_AGAIN, text, since.isoformat()))
        zone = timedelta(minutes=30)
        near = any(
            x.riding_or_watched or timedelta(0) <= to_utc(x.departure) - to_utc(now) <= zone
            for x in legs
        )
        if s.poll_records and offline(health, near) and k not in self._memory.offline:
            self._memory.offline.add(k)
            out.append(
                event(EventKind.OFFLINE, f"NS data is unavailable, {CHECK_APP}.", now.isoformat())
            )
        elif not offline(health, near):
            self._memory.offline.discard(k)
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
        self._archive(now)  # a window closing is itself no change
        if changes:
            self._apply(changes, stage=False)
        elif self._writer.st.occurrences != before:
            self._writer.save(stage=False)
        await self._deliver()

    # --- user actions and location ---

    async def act(
        self, profile_id: str, action: UserAction, direction: Direction | None = None
    ) -> str | None:
        """`choose`, `leaving` and `not travelling` through the serialised writer (7.13).
        `direction`: only that direction's occurrences are targeted (both share a date).
        None when written; else why not, as `choose_at`: nothing was targeted, an unknown
        profile or no occurrence to act on. A removed profile still running on an occurrence
        is known (rule 10)."""
        now = self._clock.now()
        self._create_occurrences(now)
        mine = [
            s for s in self._writer.st.occurrences.values()
            if s.key.profile_id == profile_id and direction in (None, s.key.direction)
        ]  # fmt: skip
        target = target_occurrence(mine, action)
        p = None if target is None else self._profile_for(target)
        if target is None or p is None:
            return no_journey_text(display_name(self._shown_name(profile_id)))
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
                changes[k] = (t.state, t.events)
        self._apply(changes, keep=True)  # nothing recomputes a user action (rule 22)
        await self._deliver()
        return None

    async def leaving(
        self, profile_id: str, service_date: date | None = None, direction: Direction | None = None
    ) -> str | None:
        """`act` with a `Leaving` tapped now, by the runtime's own clock."""
        return await self.act(profile_id, Leaving(service_date, self._clock.now()), direction)

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
    ) -> str | None:
        """A train named by its clock time (shells): the core finds the occurrence, the station
        and the connection, with one live request under the shared budget (rule 14). None when
        the choice was written; else why not (a refusal is also pushed, loudly)."""
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
        gone = no_journey_text(display_name(named.name if named else profile_id))
        target = choice_target(
            cands, departure, train_number, service_date, now, lambda s: self._ctx(s, now)
        )
        if target is None:
            return gone
        if isinstance(target, str):
            return target
        occ = target.occ
        k = okey(occ.key)
        # rule 21: what the choice is judged on must still hold when it is written
        mark = (occ.generation, occ.route_revision, occ.baseline_version)
        answers = await self._fetch_choice(target, k)
        if isinstance(answers, str):
            return answers
        ranked = named_connections(answers[0], target, departure, train_number)
        if isinstance(ranked, str):
            return ranked
        # rule 21: a reload during the request may have removed or archived the occurrence
        now_occ = self._writer.st.occurrences.get(k)
        if now_occ is None or is_resolved(now_occ):
            return gone
        if (now_occ.generation, now_occ.route_revision, now_occ.baseline_version) != mark:
            return "your journey changed while NS answered; try again"
        choices = [Choose(occ.key.service_date, c, answers) for c in ranked]
        d = occ.key.direction
        why = [self._choice_refusal(profile_id, d, c, gone) for c in choices]
        # the earliest arrival that passes; when none does, the earliest is refused loudly
        i = next((n for n, r in enumerate(why) if r is None), 0)
        return why[i] if await self.act(profile_id, choices[i], d) is None else gone

    async def _fetch_choice(self, t: ChoiceTarget, k: str) -> tuple[ProviderAnswer, ...] | str:
        """The answer naming the train first, then what judges the change to it: from a transfer
        station, a fresh observation of the kept incoming leg, never an older poll's answer
        (rule 1). One deadline for every request: the shell waits 120 s in all. A string: why not."""
        ctx = t.ctx
        end = ctx.schedule.window_end
        deadline = to_utc(self._clock.now()) + POLL_DEADLINE
        asks: list[Sequence[Query]] = [
            (Query(t.station, ctx.profile.destination(ctx.direction), t.when),)
        ]
        if t.station != ctx.profile.origin(ctx.direction):
            asks.insert(0, queries_needed(t.occ, ctx))
        out: tuple[ProviderAnswer, ...] = ()
        for queries in asks:
            got = await self._fetch_all(queries, True, end, deadline)
            self._auth(got if isinstance(got, str) else None, k, self._clock.now())
            if isinstance(got, str):
                await self._deliver()
                return f"NS did not answer ({got}); try again"
            out = (*got, *out)
        return out

    def _choice_refusal(
        self, profile_id: str, d: Direction, choice: Choose, gone: str
    ) -> str | None:
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
        return next((e.text for e in events if e.kind is EventKind.CHOOSE_REFUSED), None)

    async def location(
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
            got = await self._fetch(Query(p.origin(d), p.destination(d), now), True, None)
            qs = location_queries(s, ctx, event)  # the locked trains, from where they leave
            more = await self._fetch_all(qs, True, None) if qs else ()
            failed = got if isinstance(got, str) else None
            answer = None if isinstance(got, str) else got
            failed_more = more if isinstance(more, str) else None
            watched = () if isinstance(more, str) else more
            now = self._clock.now()
            # a rejected key on either request is reported; a healthy first answer clears an outage
            self._auth("auth" if failed_more == "auth" else failed, k, now)
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
        self._refusal = reason
        if not self._writer.store_ok and reason == self._refused_told:
            return  # the same file retried while its write fails (rule 22): told once
        self._refused_told = reason
        self._app_warning(EventKind.PROFILE_REFUSED, f"profile change refused: {reason}.", now)

    async def reload(self) -> None:
        now = self._clock.now()
        applied: Config | None = None
        try:
            # off the loop (rule 12); a view taken during the read keeps the old refusal
            text = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
            self._refusal = None
        except (OSError, UnicodeDecodeError) as e:
            self._refuse(f"cannot read {self._path.name} ({e})", now)
        else:
            old, today = self._writer.st.config, now.astimezone(TZ).date()
            if self._require_stations and self.known_stations is None:
                await self._fetch_stations()
            cfg, errors = load_config(text, self.known_stations, old, today)
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
                if not self._unchecked:
                    self._refuse(reason, now)
                self._refusal = reason
                self._unchecked = True
            else:
                self._unchecked = False
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
            if route is not None and p is not None and same_route(route, p):
                rev = new.revision
            elif s.lock is not None or s.retained or is_resolved(s):
                rev = s.route_revision  # locked or riding: runs on its route revision (rule 10)
            else:  # not locked yet: created again from the new route when its window opens
                self._memory.forget(k)  # what was seen of the old route is no evidence for it
                continue
            moved = replace(s, route_revision=rev, settings_revision=new.revision)
            if effective_profile(snaps, new, moved) != self._profile_for(s):
                moved = replace(moved, generation=s.generation + 1)  # in-flight results are stale
            occs[k] = moved
        baselines, ledgers = _rebased(self._writer.st, old, new, today)
        self._writer.st = replace(
            self._writer.st, config=new, config_snapshots=snaps, occurrences=occs,
            baselines=baselines, lookahead=ledgers,
        )  # fmt: skip

    async def reload_if_changed(self) -> None:
        """The file watch: reload when the file's mtime changes (or it disappears)."""
        try:
            mtime: int | None = (await asyncio.to_thread(self._path.stat)).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._mtime or (self._unchecked and self._stations_due()):  # tried again
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

    async def _fetch_works(self) -> PlannedWorks | None:
        """None only when the shared budget refuses; a failed lookup is coverage `unknown`, so
        the probes still run and none is clean (7.10)."""
        now = self._clock.now()
        if not self._budget.allow(now, live=False):
            return None
        before = self._budget
        self.requests += 1
        try:
            works = await asyncio.wait_for(
                self._provider.planned_works(), POLL_DEADLINE.total_seconds()
            )
        except (ProviderError, TimeoutError) as e:
            self._update_budget(before, e, None)
            if isinstance(e, AuthError):
                self._auth("auth", "lookahead", now)
            return PlannedWorks((), "unknown")
        self._update_budget(before, None, None)
        self._auth(None, "lookahead", now)  # an accepted key ends the outage (7.11)
        return works

    async def stations(self) -> frozenset[str] | None:
        """The NS station list through the shared budget (rule 14); None when the shared budget refuses or the
        request fails."""
        now = self._clock.now()
        if not self._budget.allow(now, live=False):
            return None
        before = self._budget
        self.requests += 1
        try:
            got = await asyncio.wait_for(self._provider.stations(), POLL_DEADLINE.total_seconds())
        except (ProviderError, TimeoutError) as e:
            self._update_budget(before, e, None)
            if isinstance(e, AuthError):
                self._auth("auth", "stations", now)
            return None
        self._update_budget(before, None, None)
        self._auth(None, "stations", now)  # an accepted key ends the outage (7.11)
        return got

    async def _lookahead_direction(self, p: Profile, d: Direction, works: PlannedWorks) -> None:
        run = await probe_direction(
            self._writer.st, p, d, works, self._clock.now(), self._fetch_all, self._auth
        )
        st = None if run is None else applied_run(self._writer.st, self._queue, p, d, run)
        if st is not None:
            self._writer.st = st
            self._writer.save(keep=True, stage=False)

    # --- start, reconcile and the loops ---

    async def start(self) -> None:
        now = self._clock.now()
        self._since = now  # stored messages wait for the reconcile poll (rules 5, 11)
        try:
            await self._load_and_reconcile(now)
        finally:
            self._since = None
        self._apply({})
        await self._deliver()
        self._started = True

    async def _load_and_reconcile(self, now: datetime) -> None:
        lost = None
        try:
            loaded = self._store.load()
        except StorageError as e:
            loaded, lost = None, str(e)
        if loaded is not None:
            self._writer.st = _restarted(loaded)
            self._queue.restore(loaded)
        self._writer.begin(replace(self._writer.st, started_before=True))
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
            await self._queue.stop()
