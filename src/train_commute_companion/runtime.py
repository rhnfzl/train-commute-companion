"""The runtime around the pure core: polling, the independent watchdog, the shared request
budget, look-ahead, transactional reload and the restart reconcile (7.1, 7.11, 7.12 rules 5,
10, 11, 12, 14, 21, 22, 5.2, 7.13). The only module with a clock, I/O and the notifier."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Protocol

from train_commute_companion.advice import advise, leave_now_text, needed, still_advised
from train_commute_companion.calendar import TZ, resolve_local
from train_commute_companion.classify import find_leg
from train_commute_companion.delivery import (
    JOURNEY_WARNINGS,
    Obligation,
    deliver,
    pending_journey_warnings,
    zone_report_stops_silent,
)
from train_commute_companion.health import PollRecord, WatchedLeg, board_health, cannot_see, offline
from train_commute_companion.lifecycle import (
    Board,
    Choose,
    Ctx,
    DirectionState,
    LegProgress,
    LegState,
    NotTravelling,
    OccurrenceState,
    UserAction,
    _left_note,
    _pool,
    apply_action,
    ends,
    on_location,
    on_poll,
    on_tick,
    queries_needed,
    target_occurrence,
    whole_day_keys,
)
from train_commute_companion.location import LocationEvent
from train_commute_companion.lookahead import (
    BaselineHistory,
    LookAheadLedger,
    ProbeResult,
    evaluate,
    probe_dates,
    probe_queries,
    rebase_on_edit,
    reset_on_edit,
    run_lookahead,
    service_date,
)
from train_commute_companion.match import usual_connection
from train_commute_companion.messages import CHECK_APP
from train_commute_companion.model import (
    AlertEvent,
    Config,
    Direction,
    EventKind,
    Observation,
    OccurrenceKey,
    Profile,
    Provenance,
    Severity,
    Signature,
    TrainId,
    UsualJourney,
)
from train_commute_companion.news import Fact, all_good, escalations, severity, unsent
from train_commute_companion.notify import Notifier
from train_commute_companion.profile import ProfileError, load_config
from train_commute_companion.providers.base import (
    AuthError,
    MalformedAnswer,
    PlannedWorks,
    Provider,
    ProviderAnswer,
    ProviderError,
    Query,
    RateLimited,
    ServerError,
)
from train_commute_companion.schedule import dated_schedule, schedules_active
from train_commute_companion.store import PersistedState, StorageError, Store

log = logging.getLogger(__name__)

POLL_EVERY_S = 120
POLL_DEADLINE = timedelta(seconds=90)
WATCHDOG_EVERY_S = 30
FILE_EVERY_S = 10
LOOKAHEAD_AT = time(6, 0)
BACKOFF_MIN = (2, 4, 8)
_SENT = "sent:"  # delivery-ledger entries in `alert_keys`: fact key -> severity it was sent at
# fact keys the latest healthy poll reported: a fact that a healthy poll no longer reports has
# ended, so its delivery entry goes too and the same fact coming back later is news (7.5)
_LIVE = "live:"
# ongoing facts a poll restates while they last: a held one holds while its key is live (rule 5)
_ONGOING = frozenset(
    {
        EventKind.DELAYED,
        EventKind.TRANSFER_AT_RISK,
        EventKind.PLATFORM_CHANGE,
        EventKind.NO_CONNECTION,
    }
)
_DELAY_NEWS = frozenset({EventKind.DELAYED, EventKind.NEARLY_ON_TIME, EventKind.BACK_TO_NORMAL})
# judged on the latest healthy answers (rule 5b): held while an occurrence has none yet
_OBSERVED = frozenset(
    {
        EventKind.ALL_GOOD,
        EventKind.LEAVE_NOW,
        EventKind.EARLIER_TRAIN_WITHDRAWN,
        EventKind.NEXT_CONNECTION,
    }
)
# loud warnings with a "next good connection" clause, written against the lock of their time
_NAMES_LOCK = JOURNEY_WARNINGS | {EventKind.MISSED_FIRST_TRAIN, EventKind.MISSED_TRANSFER}
_BREAKS = ("cancelled:", "cut_short:", "missing:")
_GONE = (LegState.DONE, LegState.CANCELLED, LegState.RELOCKED)
_APP = "app"  # profile name on application warnings, which belong to no profile

PollError = Literal["auth", "rate", "provider", "parse", "timeout"]


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(TZ)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RequestBudget:
    """One per API key, shared by every profile and look-ahead (7.12 rule 14)."""

    cooldown_until: datetime | None = None
    backoff_step: int = 0

    def allow(self, now: datetime, live: bool) -> bool:
        if self.cooldown_until is None:
            return True
        # live journeys first: look-ahead waits one more poll interval after a cooldown
        extra = timedelta(0) if live else timedelta(seconds=POLL_EVERY_S)
        return _utc(now) >= _utc(self.cooldown_until) + extra

    def on_result(
        self, error: BaseException | None, now: datetime, window_end: datetime | None
    ) -> "RequestBudget":
        if error is None:
            return RequestBudget()
        if not isinstance(error, (RateLimited, ServerError)):
            return self
        step = min(self.backoff_step + 1, len(BACKOFF_MIN))
        until = _utc(now) + timedelta(minutes=BACKOFF_MIN[step - 1])
        if window_end is not None and _utc(now) < _utc(window_end):
            until = min(until, _utc(window_end))
        return RequestBudget(until, step)


def _okey(k: OccurrenceKey) -> str:
    return f"{k.profile_id}|{k.direction.value}|{k.service_date.isoformat()}"


def _dkey(pid: str, d: Direction) -> str:
    return f"{pid}|{d.value}"


def _title(name: str) -> str:
    return name.replace("_", " ").title()


def _with_settings(route: Profile, settings: Profile) -> Profile:
    """`route`'s trains with `settings`' settings (7.12 rule 10)."""

    def j(d: Direction) -> UsualJourney:
        return replace(
            route.journey(d),
            buffer_min=settings.journey(d).buffer_min,
            earlier_train_advice=settings.journey(d).earlier_train_advice,
        )

    return replace(
        route,
        name=settings.name,
        alerts=settings.alerts,
        location=settings.location,
        transfer_min=settings.transfer_min,
        outbound=j(Direction.OUTBOUND),
        return_=j(Direction.RETURN),
    )


def _same_route(a: Profile, b: Profile) -> bool:
    return _with_settings(a, b) == b


def _find(cfg: Config | None, pid: str) -> Profile | None:
    return next((p for p in (cfg.profiles if cfg else ()) if p.id == pid), None)


def _effective(
    snaps: Mapping[int, Config], cfg: Config | None, s: OccurrenceState
) -> Profile | None:
    """The occurrence's route revision with the newest settings (7.12 rule 10)."""
    route = _find(snaps.get(s.route_revision), s.key.profile_id)
    cur = _find(cfg, s.key.profile_id)
    if route is None or cur is None:
        return route or cur
    return _with_settings(route, cur)


def _in_quiet(window: tuple[time, time] | None, now: datetime) -> bool:
    if window is None:
        return False
    t, (a, b) = now.astimezone(TZ).time(), window
    return a <= t < b if a <= b else (t >= a or t < b)


def _resolved(s: OccurrenceState) -> bool:
    done = s.direction_state in (DirectionState.DONE, DirectionState.EXPIRED)
    return s.not_travelling or (done and not s.retained)


def _fresh_state(key: OccurrenceKey, revision: int, baseline: int | None) -> OccurrenceState:
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


def _broken(s: OccurrenceState, t: TrainId | None) -> bool:
    """A live break (cancelled, cut short, missing) is held for train `t`."""
    ident = "" if t is None else f":{_ident(t)}:"
    return any(key.startswith(_LIVE + b) and ident in key for key in s.alert_keys for b in _BREAKS)


class _View:
    """The delivery ledger view over the runtime's current state (7.12 rule 5)."""

    def __init__(self, rt: "Runtime") -> None:
        self._rt = rt

    def _occ(self, key: OccurrenceKey) -> OccurrenceState | None:
        return self._rt._st.occurrences.get(_okey(key))

    def skipped(self, key: OccurrenceKey) -> bool:
        s = self._occ(key)
        return s is not None and s.not_travelling

    def fact_holds(self, o: Obligation) -> bool:
        rt = self._rt
        if o.look_ahead is not None:
            pid, d, _ = o.look_ahead
            then = _find(rt._st.config_snapshots.get(o.route_revision), pid)
            now = _find(rt._st.config, pid)
            if now is None or (then is not None and not _same_route(then, now)):
                return False
            if then is None and rt._st.config and o.route_revision != rt._st.config.revision:
                return False
            hist = rt._st.baselines.get(_dkey(pid, d), BaselineHistory())
            versions = {v.version for v in hist.versions}
            return o.baseline_version is None or o.baseline_version in versions
        if o.occurrence is None:
            return True
        s = self._occ(o.occurrence)
        if s is None or s.not_travelling or not self._still(o, s):
            return False
        p = rt._profile_for(s)
        if o.event.severity is Severity.SILENT and p is not None:
            d = s.key.direction
            role = "home" if p.destination(d) == p.home_station else "work"
            zone = (p.location.zones if p.location else {}).get(role, role)
            return not zone_report_stops_silent(s, {p.destination(d), zone})
        return True

    def _still(self, o: Obligation, s: OccurrenceState) -> bool:
        """Rule 5(b): the message's fact, regenerated from the current state and the latest
        healthy answers."""
        rt, e, k = self._rt, o.event, _okey(s.key)
        legs = [x for j in (*((s.lock,) if s.lock else ()), *s.retained) for x in j.legs]
        unfinished = any(x.train == e.train and x.state not in _GONE for x in legs)
        if e.kind in _DELAY_NEWS:  # the delay memory still says what the message stated
            if not _states_current(o.fact_key, e.kind, s.alert_keys):
                return False
            if e.kind is EventKind.DELAYED:
                return _SENT + o.fact_key in s.alert_keys
            return unfinished and not _broken(s, e.train)  # no all-clear for a broken train
        if e.kind in _ONGOING:  # a healthy poll that stopped reporting it ended it
            return _SENT + o.fact_key in s.alert_keys
        if e.kind is EventKind.CANNOT_SEE_TRAIN:
            return k in rt._blind
        if e.kind is EventKind.CAN_SEE_TRAIN_AGAIN:
            return k not in rt._blind
        if e.kind is EventKind.OFFLINE:
            return k in rt._offline
        if e.kind is EventKind.RESTORED:  # back in the lock, and not broken again since
            t = e.train
            on = s.lock is not None and any(
                x.train == t and x.state not in (LegState.CANCELLED, LegState.RELOCKED)
                for x in s.lock.legs
            )
            return on and not _broken(s, t)
        if e.kind in (EventKind.MISSED_FIRST_TRAIN, EventKind.MISSED_TRANSFER):
            return any(x.train == e.train and x.state is LegState.MISSED for x in legs)
        if e.kind is EventKind.NEXT_CONNECTION:  # "if you are not on it": while it is unfinished
            return unfinished
        ctx, answers = rt._ctx(s, rt._clock.now()), rt._boards.get(k, ())
        if e.kind is EventKind.ALL_GOOD:  # still proven now, on the same terms as the first time
            if ctx is None or not answers or k in rt._blind or s.lock is None:
                return False
            if s.lock.legs[0].train != e.train:  # it named the journey a relock replaced
                return False
            board = Board(answers, (), s.generation)
            return all_good(replace(s, all_good_evaluated=False), board, ctx) is not None
        if e.kind in (EventKind.LEAVE_NOW, EventKind.EARLIER_TRAIN_WITHDRAWN):
            a, origin = s.advice, None if ctx is None else _origin_answer(answers, ctx)
            if a is None or ctx is None or origin is None:
                return False
            if e.kind is EventKind.LEAVE_NOW:
                return (
                    e.train is not None
                    and a.advised_train == e.train
                    and not a.withdrawn
                    and still_advised(s, ctx, origin, e.train) is not None
                )
            return a.withdrawn and not needed(s, ctx, origin, rt._usual_arrival(s, ctx, origin))
        return True  # choose_refused: the answer to a command the user gave, true as said

    def newer_covers(self, o: Obligation) -> bool:
        if o.occurrence is None:
            return False

        def family(k: EventKind) -> frozenset[EventKind]:
            return _DELAY_NEWS if k in _DELAY_NEWS else frozenset({k})

        return any(
            x.id != o.id
            and x.occurrence == o.occurrence
            and x.event.kind in family(o.event.kind)
            and x.event.train == o.event.train
            and _utc(x.event.created_at) > _utc(o.event.created_at)
            for x in self._rt._st.obligations
        )

    def revision_live(self, key: OccurrenceKey, rev: int) -> bool:
        s = self._occ(key)
        if s is None:
            return False
        if s.route_revision == rev:
            return True
        snaps = self._rt._st.config_snapshots
        a, b = (
            _find(snaps.get(rev), key.profile_id),
            _find(snaps.get(s.route_revision), key.profile_id),
        )
        return a is not None and b is not None and _same_route(a, b)

    def in_quiet_hours(self, profile_id: str, now: datetime) -> bool:
        p = _find(self._rt._st.config, profile_id)
        return p is not None and _in_quiet(p.alerts.quiet_hours, now)

    def current(self, o: Obligation) -> AlertEvent:
        e = self._regenerated(o, self._relocked(o, o.event))
        p = _find(self._rt._st.config, e.profile_id)
        if p is None or p.name == e.profile_name:
            return e
        old, new = _title(e.profile_name), _title(p.name)
        text = new + e.text[len(old) :] if e.text.startswith(old + ":") else e.text
        return replace(e, profile_name=p.name, text=text)

    def _regenerated(self, o: Obligation, e: AlertEvent) -> AlertEvent:
        """Rule 5(b): an instruction states its connection as the latest answers show it."""
        rt = self._rt
        s = None if o.occurrence is None else rt._st.occurrences.get(_okey(o.occurrence))
        if s is None or e.train is None:
            return e
        k = _okey(s.key)
        ctx, answers = rt._ctx(s, rt._clock.now()), rt._boards.get(k, ())
        if ctx is None or not answers:
            return e
        body = None
        if e.kind is EventKind.LEAVE_NOW:
            origin = _origin_answer(answers, ctx)
            c = None if origin is None else still_advised(s, ctx, origin, e.train)
            body = None if c is None else leave_now_text(c, ctx.now.tzinfo)
        elif e.kind is EventKind.NEXT_CONNECTION:
            pool = _pool(answers)
            for j in (*((s.lock,) if s.lock else ()), *s.retained):
                i = next((i for i, x in enumerate(j.legs) if x.train == e.train), None)
                if i is not None:
                    body = _left_note(s, ctx, pool, j, i).text.partition(": ")[2]
                    break
        if body is None:
            return e
        head, tail = _twin(e.text)
        return replace(e, text=f"{head.partition(': ')[0]}: {body}{tail}")

    def _relocked(self, o: Obligation, e: AlertEvent) -> AlertEvent:
        """Rule 5(a): a journey warning names the current lock, not the replacement frozen at
        write time. Kept as written while the lock still holds the warned train (nothing better
        to name) or still ends in the whole replacement the text names. Otherwise the stale
        clause is replaced: the lock's first train as the next good connection while nothing
        of it has left, else the train now followed and every onward leg still ahead."""
        rt = self._rt
        s = None if o.occurrence is None else rt._st.occurrences.get(_okey(o.occurrence))
        if e.kind not in _NAMES_LOCK or e.train is None or s is None or s.lock is None:
            return e
        live = [x for x in s.lock.legs if x.state not in (LegState.CANCELLED, LegState.RELOCKED)]
        if not live or e.train in {x.train for x in live} or ":retained:" in e.key:
            return e  # a retained warning already names its own next connection (rule 7)
        named = _named(e.key)
        if named and [_ident(x.train) for x in live][-len(named) :] == named:
            return e
        head, tail = _twin(e.text)
        if (i := head.find(" Next good connection:")) >= 0:
            head = head[:i]
        k = _okey(s.key)
        ahead = [x for x in live if x.state in (LegState.PENDING, LegState.BOARDING)]
        left = [x for x in live if x not in ahead]
        if not left:
            nxt = "Next good connection: {}{}.".format(*rt._leg_now(k, ahead[0]))
        else:
            now = f"{left[-1].train.planned_departure.astimezone(TZ):%H:%M}"
            onward = "".join(
                f", then the {t} from {x.board_station}{pl}"
                for x in ahead
                for t, pl in (rt._leg_now(k, x),)
            )
            nxt = f"Now following your {now} train{onward}."
        return replace(e, text=f"{head.rstrip()} {nxt}{tail}")


def _ident(t: TrainId) -> str:
    return f"{t.train_number}@{t.planned_departure.isoformat()}"


def _named(key: str) -> list[str]:
    """The replacement a journey warning names, by identity, from its key (`_relock`)."""
    part = next((x for x in key.split("|")[1:] if "@" in x and not x.startswith("stated")), "")
    return [x for x in part.split(":retained:")[0].split(",") if x]


def _twin(text: str) -> tuple[str, str]:
    """The text and its 7.13 twin suffix, which stays last."""
    i = text.find(" (service date ")
    return (text, "") if i < 0 else (text[:i], text[i:])


def _states_current(key: str, kind: EventKind, keys: Mapping[str, str]) -> bool:
    """A delay message whose stated values are still the ones remembered (`_stated`)."""
    base, sep, vals = key.rpartition("|stated+")
    if not sep:
        return True
    base = EventKind.DELAYED.value + base[len(kind.value) :]
    said = vals.split(",")
    slots = [base + t for t in ("#d", "#a")] if len(said) == 2 else [base]
    return [keys.get(x) for x in slots] == said


def _origin_answer(answers: Sequence[ProviderAnswer], ctx: Ctx) -> ProviderAnswer | None:
    origin = ctx.profile.origin(ctx.direction)
    return next((a for a in answers if a.query.origin == origin), None)


async def every(clock: Clock, seconds: float, step: Callable[[], Awaitable[None]]) -> None:
    """Runs `step` on fixed start times `seconds` apart (7.1: a poll every 2 min, not 2 min
    after the last one ended). A step that overran a whole interval starts again at once."""
    due = _utc(clock.now())
    while True:
        try:
            await step()
        except Exception:  # one failing loop must not stop the others (rule 12)
            log.exception("runtime step failed")
        due += timedelta(seconds=seconds)
        now = _utc(clock.now())
        due = max(due, now)
        await clock.sleep((due - now).total_seconds())


class Runtime:
    def __init__(
        self,
        *,
        profile_path: Path,
        store: Store,
        provider: Provider,
        notifier: Notifier,
        clock: Clock,
        known_stations: frozenset[str] | None,
    ) -> None:
        self._path = profile_path
        self._store = store
        self._provider = provider
        self.events_log: list[AlertEvent] = []
        self._notifier = _LoggingNotifier(notifier, self.events_log)
        self._clock = clock
        self.known_stations = known_stations  # None: codes unchecked (replays, tests)
        self._st = PersistedState(None, {}, {}, {}, {}, (), False)
        self._budget = RequestBudget()
        self._store_ok = True
        self._dirty = False
        self._mtime: int | None = None
        self._auth_outage = False
        self._auth_told: set[str] = set()
        self._blind: dict[str, datetime] = {}  # occurrence -> start of its cannot_see outage
        # per occurrence before a lock: the usual train as the last healthy answer showed it
        self._before: dict[str, WatchedLeg] = {}
        self._offline: set[str] = set()
        # per occurrence and train: what the last healthy poll showed of it (rule 13)
        self._seen: dict[str, dict[TrainId, WatchedLeg]] = {}
        self._sending = asyncio.Lock()
        self.requests = 0  # NS publishes no quota (7.1): counted and logged
        # set while start() reconciles: messages stored before it wait for the fresh poll
        self._since: datetime | None = None
        # per occurrence: the latest healthy answers, what rule 5(b) regenerates messages from
        self._boards: dict[str, tuple[ProviderAnswer, ...]] = {}
        # 5.2, 5.3: with no station list at all, the stored configuration runs and an edit
        # waits until the list can be fetched again (retried, under the shared budget)
        self.require_stations = False
        self.on_stations: Callable[[frozenset[str]], object] | None = None
        self._unchecked = False
        self._stations_tried: datetime | None = None

    # --- configuration and context ---

    @property
    def config(self) -> Config | None:
        return self._st.config

    @property
    def state(self) -> PersistedState:
        return self._st

    def _profile_for(self, s: OccurrenceState) -> Profile | None:
        return _effective(self._st.config_snapshots, self._st.config, s)

    def _ctx(self, s: OccurrenceState, now: datetime) -> Ctx | None:
        p = self._profile_for(s)
        if p is None:
            return None
        d = s.key.direction
        commute_day = s.key.service_date - timedelta(days=p.journey(d).day_offset)
        own = s.baseline_signature
        if own is not None and len(own.legs) == len(p.journey(d).transfers) + 1:
            sig: Signature | None = own
        elif s.baseline_frozen:
            sig = None  # it started with no baseline: a later one never moves it (rule 19)
        else:
            sig = self._signature(p, d, s.key.service_date, s.baseline_version)
        first = sig.legs[0].departure if sig else None
        return Ctx(p, d, dated_schedule(p, d, commute_day, first), sig, now)

    def _frozen(self, s: OccurrenceState) -> OccurrenceState:
        """The occurrence with its own schedule fixed from the history it runs on now, before
        anything replaces that history (rules 10, 19). Unchanged when already fixed or when its
        profile is unknown."""
        if s.baseline_frozen:
            return s
        if s.baseline_signature is not None:
            return replace(s, baseline_frozen=True)
        p = self._profile_for(s)
        if p is None:
            return s
        sig = self._signature(p, s.key.direction, s.key.service_date, s.baseline_version)
        return replace(s, baseline_signature=sig, baseline_frozen=True)

    def _leg_now(self, k: str, x: LegProgress) -> tuple[str, str]:
        """A locked leg as the latest answers show it: departure (expected, else planned) and
        its platform, for a message naming it."""
        answers = self._boards.get(k, ())
        leg = find_leg(_pool(answers), x.train, x.board_station)[0] if answers else None
        dep = None if leg is None else leg.board.departure
        t = dep.value if dep is not None and dep.value is not None else x.train.planned_departure
        track = None if leg is None else leg.board.actual_track or leg.board.planned_track
        return f"{t.astimezone(TZ):%H:%M}", f" (platform {track})" if track else ""

    def _signature(
        self, p: Profile, d: Direction, sd: date, version: int | None = None
    ) -> Signature | None:
        hist = self._st.baselines.get(_dkey(p.id, d), BaselineHistory())
        v = next((x for x in hist.versions if x.version == version), None) or hist.in_force(sd)
        n = len(p.journey(d).transfers) + 1
        return v.signature if v is not None and len(v.signature.legs) == n else None

    # --- the serialised writer: state, ledger and obligations in one save (rule 5, 22) ---

    def _obligation(self, e: AlertEvent, s: OccurrenceState | None, now: datetime) -> Obligation:
        expires = None
        if e.kind is EventKind.LEAVE_NOW and e.train is not None and s is not None:
            p = self._profile_for(s)
            buffer = p.journey(s.key.direction).buffer_min if p else 0
            expires = _utc(e.train.planned_departure) - timedelta(minutes=buffer)
        if s is not None:
            revision = s.route_revision
        else:
            revision = self._st.config.revision if self._st.config else 0
        return Obligation(
            f"{e.key}|{e.severity.value}|{now.isoformat()}",
            e,
            None if s is None else s.key,
            None,
            revision,
            None if s is None else s.baseline_version,
            e.key,
            expires,
            0,
        )

    def _app_warning(self, kind: EventKind, body: str, now: datetime, tag: str = "") -> None:
        e = AlertEvent(
            "", _APP, None, None, None, kind, Severity.LOUD, None,
            f"{_title(_APP)}: {body}", f"{kind.value}:{tag}:{now.isoformat()}", now,
        )  # fmt: skip
        self._st = replace(
            self._st, obligations=(*self._st.obligations, self._obligation(e, None, now))
        )

    def _record(
        self, s: OccurrenceState, events: Sequence[AlertEvent], now: datetime, polled: bool
    ) -> tuple[OccurrenceState, list[Obligation]]:
        """News (7.5) and escalation (rule 4) against the occurrence's delivery ledger.
        `polled`: the events are everything a healthy poll reports, so a reported fact it no
        longer reports has ended and leaves the ledger (a recurrence is news again)."""
        keys = dict(s.alert_keys)
        if polled:
            emitted = {e.key for e in events}
            for k in [k for k in keys if k.startswith(_LIVE)]:
                if k[len(_LIVE) :] not in emitted:
                    keys.pop(k)
                    keys.pop(_SENT + k[len(_LIVE) :], None)
            keys.update({_LIVE + k: "1" for k in emitted})
        sent = {k[len(_SENT) :]: Severity(v) for k, v in keys.items() if k.startswith(_SENT)}
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
        if not out:
            return (s if keys == s.alert_keys else replace(s, alert_keys=keys)), []
        twins = [
            x for x in self._st.occurrences.values()
            if x.key != s.key and x.key.profile_id == s.key.profile_id
            and x.key.direction is s.key.direction and not _resolved(x)
        ]  # fmt: skip
        obligations = []
        for e in out:
            keys[_SENT + e.key] = e.severity.value
            if twins:  # 7.13: alerts name the service date while two occurrences exist
                e = replace(e, text=f"{e.text} (service date {s.key.service_date.isoformat()})")
            obligations.append(self._obligation(e, s, now))
        return replace(s, alert_keys=keys), obligations

    def _apply(
        self,
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]],
        polled: bool = False,
    ) -> None:
        now = self._clock.now()
        occs = dict(self._st.occurrences)
        obligations = list(self._st.obligations)
        for k, (s, events) in changes.items():
            occs[k] = s  # twins see the occurrences written so far
            self._st = replace(self._st, occurrences=occs)
            s, new = self._record(s, events, now, polled)
            occs[k] = s
            obligations += new
        self._st = replace(self._st, occurrences=occs, obligations=tuple(obligations))
        self._archive(now)
        self._save()

    def _archive(self, now: datetime) -> None:
        """Rule 6: an occurrence leaves the store once it ended and its pending journey warnings
        were delivered; it stays until its window closed so it is not created again."""
        occs = {}
        for k, s in self._st.occurrences.items():
            ctx = self._ctx(s, now)
            closed = ctx is None or _utc(now) >= _utc(ctx.schedule.window_end)
            if closed and ends(s, now, pending_journey_warnings(self._st.obligations, s.key)):
                self._seen.pop(k, None)
                self._before.pop(k, None)
                self._boards.pop(k, None)
                self._blind.pop(k, None)
                self._offline.discard(k)
                continue
            occs[k] = s
        cfg = self._st.config
        live = {s.route_revision for s in occs.values()} | ({cfg.revision} if cfg else set())
        live |= {o.route_revision for o in self._st.obligations}  # rule 5(c) judges them on it
        snaps = {r: c for r, c in self._st.config_snapshots.items() if r in live}
        self._st = replace(self._st, occurrences=occs, config_snapshots=snaps)

    def _save(self) -> None:
        try:
            self._store.save(self._st)
        except StorageError as e:
            self._dirty = True
            if self._store_ok:  # one loud storage_failed per outage, held in memory only
                self._store_ok = False
                log.error("store write failed: %s", e)
                self._app_warning(
                    EventKind.STORAGE_FAILED,
                    "could not save state, alerts may repeat or be lost.",
                    self._clock.now(),
                )
            return
        self._dirty = False
        self._store_ok = True

    async def _deliver(self) -> None:
        """Every pending obligation, revalidated right before sending (rule 5). Merges by id,
        so obligations added by another task while a send awaited are kept. Serialised: the
        poll, watchdog and file watch all deliver, and two passes would send one alert twice."""
        async with self._sending:
            await self._deliver_pass()

    async def _deliver_pass(self) -> None:
        view = _View(self)
        finished: set[str] = set()
        kept: dict[str, Obligation] = {}
        for o in self._st.obligations:
            if self._since is not None and _utc(o.event.created_at) < _utc(self._since):
                continue  # written before this start: judged once the reconcile poll ran (r11)
            k = None if o.occurrence is None else _okey(o.occurrence)
            if o.event.kind in _OBSERVED and k in self._st.occurrences and k not in self._boards:
                continue  # judged on observations: waits for a healthy answer (rule 5b)
            # a fresh clock per message: a slow send must not leave the next one judged early
            r = await deliver(o, view, self._notifier, self._clock.now())
            if r is None:
                finished.add(o.id)
            elif r != o:
                kept[o.id] = r
        if not finished and not kept:
            return

        def survive(xs: Iterable[Obligation]) -> tuple[Obligation, ...]:
            return tuple(kept.get(o.id, o) for o in xs if o.id not in finished)

        # look-ahead messages live in their ledger too, so a delivered one counts as told
        ledgers = {k: replace(v, pending=survive(v.pending)) for k, v in self._st.lookahead.items()}
        self._st = replace(self._st, obligations=survive(self._st.obligations), lookahead=ledgers)
        self._archive(self._clock.now())
        self._save()

    # --- provider access under the shared budget ---

    def _settle(
        self, before: RequestBudget, error: BaseException | None, window_end: datetime | None
    ) -> None:
        """The shared budget after one request (rule 14). A success clears only the state it
        started under: an older request finishing late never clears a newer 429 cooldown."""
        if error is not None:
            self._budget = self._budget.on_result(error, self._clock.now(), window_end)
        elif self._budget is before:
            self._budget = RequestBudget()

    async def _fetch(
        self,
        q: Query,
        live: bool,
        window_end: datetime | None,
        deadline: datetime | None = None,
    ) -> ProviderAnswer | PollError:
        """`deadline`: the poll's own hard deadline (7.1), shared by all its requests."""
        if not self._budget.allow(self._clock.now(), live):
            return "rate"  # skipped by the shared backoff: counts toward board health (7.1)
        started = self._clock.now()
        deadline = deadline or _utc(started) + POLL_DEADLINE
        left = (_utc(deadline) - _utc(started)).total_seconds()
        if left <= 0:
            return "timeout"
        before = self._budget
        self.requests += 1
        log.debug("request %d: %s", self.requests, q)
        error: PollError | None = None
        exc: BaseException | None = None
        answer = None
        try:
            answer = await asyncio.wait_for(self._provider.trips(q), left)
        except TimeoutError:
            error = "timeout"
        except AuthError as e:
            error, exc = "auth", e
        except RateLimited as e:
            error, exc = "rate", e
        except MalformedAnswer as e:
            error, exc = "parse", e
        except ProviderError as e:
            error, exc = "provider", e
        now = self._clock.now()
        if error is None and _utc(now) > _utc(deadline):
            error = "timeout"  # a late answer never overwrites a newer one (7.1)
        if exc is not None or error is None:  # a timeout neither backs off nor resets it
            self._settle(before, exc, window_end)
        if error is not None or answer is None:
            return error or "provider"
        return answer

    async def _fetch_all(
        self, queries: Sequence[Query], live: bool, window_end: datetime | None
    ) -> tuple[ProviderAnswer, ...] | PollError:
        """All or nothing: a poll missing one answer cannot judge the legs it was for (rule 2).
        One hard deadline for the whole poll, not one per request (7.1)."""
        answers = []
        deadline = _utc(self._clock.now()) + POLL_DEADLINE
        for q in queries:
            a = await self._fetch(q, live, window_end, deadline)
            if isinstance(a, str):
                return a
            answers.append(a)
        return tuple(answers)

    def _auth(self, error: PollError | None, who: str, now: datetime) -> None:
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
        cfg = self._st.config
        if cfg is None:
            return

        def first(p: Profile, d: Direction, commute_day: date) -> time | None:
            # each occurrence's window from the baseline in force on its own service date
            sig = self._signature(p, d, service_date(p, d, commute_day))
            return sig.legs[0].departure if sig is not None else None

        occs = dict(self._st.occurrences)
        for sch in schedules_active(cfg, {}, now, first):
            k = _okey(sch.key)
            if k not in occs:
                hist = self._st.baselines.get(_dkey(sch.key.profile_id, sch.key.direction))
                v = hist.in_force(sch.key.service_date) if hist else None
                fresh = _fresh_state(sch.key, cfg.revision, v.version if v else None)
                own = v.signature if v else None
                occs[k] = replace(fresh, baseline_signature=own, baseline_frozen=True)
        self._st = replace(self._st, occurrences=occs)

    async def poll_once(self) -> None:
        self._create_occurrences(self._clock.now())
        for k in sorted(self._st.occurrences):
            await self._poll(k)
        await self._deliver()

    async def _poll(self, k: str) -> None:
        s = self._st.occurrences.get(k)
        now = self._clock.now()
        ctx = None if s is None else self._ctx(s, now)
        if s is None or ctx is None or _resolved(s):
            return
        followed = s.lock is not None or bool(s.retained)
        if not followed and _utc(now) >= _utc(ctx.schedule.window_end):
            t = on_tick(s, ctx)
            self._apply({k: (t.state, t.events)})
            return  # the window only selects the first train (rule 19)
        gen = s.generation
        got = await self._fetch_all(queries_needed(s, ctx), True, ctx.schedule.window_end)
        now = self._clock.now()
        s = self._st.occurrences.get(k)
        ctx = None if s is None else self._ctx(s, now)
        if s is None or ctx is None:
            return
        error = got if isinstance(got, str) else None
        answers = () if isinstance(got, str) else got
        self._auth(error, k, now)
        board = Board(answers, (PollRecord(now, error is None, error),), gen)
        t = on_poll(s, ctx, board)
        st, events = t.state, list(t.events)
        current = s.generation == gen
        if current:  # a choice since the poll started makes the answer stale
            origin = ctx.profile.origin(ctx.direction)
            usual = next((a for a in answers if a.query.origin == origin), None)
            if usual is not None and s.lock is None:
                self._remember_usual(k, ctx, usual)
            tick = on_tick(st, ctx, usual)
            st, events = tick.state, [*events, *tick.events]
            if st.lock is not None and not st.all_good_evaluated:
                good = all_good(st, board, ctx)  # Q4: judged once, at the first poll with a lock
                st = replace(st, all_good_evaluated=True)
                events += [good] if good else []
            if usual is not None:
                # no reference: an absent or broken usual still makes waiting bad (7.6)
                arrival = self._usual_arrival(st, ctx, usual)
                record, more = advise(st, ctx, usual, arrival)
                if ctx.signature is None and arrival is not None:  # never a relock's arrival
                    record = replace(record, usual_arrival=arrival)
                st, events = replace(st, advice=record), [*events, *more]
            if answers:
                self._remember(k, st, answers)
                self._boards[k] = answers
        self._apply({k: (st, events)}, polled=current and bool(answers))

    def _usual_arrival(
        self, s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer | None
    ) -> datetime | None:
        """The usual journey's planned arrival (7.6): from the baseline; else as first read
        from a healthy answer (kept in the advice record); else the usual in this answer;
        else the lock while it still starts on the usual first train."""
        sig = ctx.signature
        if sig is not None:
            last = sig.legs[-1]
            day = ctx.schedule.key.service_date + timedelta(days=last.day_offset)
            dep = resolve_local(day, last.departure, edge="start")
            return _utc(dep) + timedelta(minutes=last.duration_min)
        if s.advice is not None and s.advice.usual_arrival is not None:
            return s.advice.usual_arrival
        p, d, first = ctx.profile, ctx.direction, ctx.schedule.first_departure
        if answer is not None:
            c = usual_connection(answer, p.origin(d), first, p.journey(d).transfers, None)
            if c is not None and c.legs[-1].alight.planned_arrival is not None:
                return c.legs[-1].alight.planned_arrival
        if s.lock is not None and _utc(s.lock.legs[0].train.planned_departure) == _utc(first):
            return s.lock.planned_final_arrival
        return None

    def _remember_usual(self, k: str, ctx: Ctx, answer: ProviderAnswer) -> None:
        """Before a lock: what the healthy answer shows of the usual train (rules 13, 20), so
        the watchdog judges it on the same terms as a locked leg, never on invented timing."""
        p, d, first = ctx.profile, ctx.direction, ctx.schedule.first_departure
        old = self._before.get(k)
        c = usual_connection(answer, p.origin(d), first, p.journey(d).transfers, ctx.signature)
        none = Provenance.MISSING
        if c is None:
            train, ob, dep = (
                TrainId(ctx.schedule.key.service_date, "", first),
                Observation.MISSING,
                none,
            )
        else:
            leg = c.legs[0]
            train, ob = leg.train, find_leg(answer, leg.train, p.origin(d))[1]
            dep = leg.board.departure.provenance if leg.board.departure else none
        seen = answer.fetched_at if ob is Observation.SEEN else (old.last_seen_at if old else None)
        self._before[k] = WatchedLeg(train, first, False, ob, dep, dep, seen)

    def _remember(self, k: str, s: OccurrenceState, answers: Sequence[ProviderAnswer]) -> None:
        newest = max(answers, key=lambda a: _utc(a.fetched_at))
        merged = ProviderAnswer(
            newest.query, tuple(c for a in answers for c in a.connections), newest.fetched_at
        )
        seen = self._seen.setdefault(k, {})
        for j in (*((s.lock,) if s.lock else ()), *s.retained):
            for x in j.legs:
                leg, ob = find_leg(merged, x.train, x.board_station)
                old = seen.get(x.train)
                dep = arr = Provenance.MISSING
                if leg is not None:
                    dep = leg.board.departure.provenance if leg.board.departure else dep
                    stop = next((st for st in leg.stops if st.code == x.alight_stop), leg.alight)
                    arr = stop.arrival.provenance if stop.arrival else arr
                last = (
                    newest.fetched_at
                    if ob is Observation.SEEN
                    else (old.last_seen_at if old else None)
                )
                seen[x.train] = WatchedLeg(
                    x.train, x.train.planned_departure, False, ob, dep, arr, last
                )

    # --- the watchdog (rule 12) ---

    def _watched(self, k: str, s: OccurrenceState, ctx: Ctx, now: datetime) -> list[WatchedLeg]:
        seen = self._seen.get(k, {})
        out = []
        main = (s.lock,) if s.lock and s.direction_state is not DirectionState.DONE else ()
        for j in (*main, *s.retained):
            for x in j.legs:
                if x.state in (LegState.CANCELLED, LegState.RELOCKED):
                    continue
                w = x.watch_until
                watched = x.state is LegState.DONE and (w is None or _utc(now) < _utc(w))
                if x.state is LegState.DONE and not watched:
                    continue
                gone = _utc(now) >= _utc(x.train.planned_departure)  # may be on it (rule 7)
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
        if s.lock is None and not s.retained and k in self._before:  # before a lock (rule 13)
            out.append(replace(self._before[k], departure=ctx.schedule.first_departure))
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
        blind, reason = cannot_see(legs, health, now)
        name = _title(ctx.profile.name)
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
                ctx.profile.id, ctx.profile.name, ctx.direction, s.key.service_date, None, kind,
                sev, None, f"{name}: {body}", f"{kind.value}:{k}:{tag}", now,
            )  # fmt: skip

        if blind and k not in self._blind:
            self._blind[k] = now
            text = f"cannot see your train in NS data ({reason}), {CHECK_APP}."
            out.append(event(EventKind.CANNOT_SEE_TRAIN, text, now.isoformat()))
        elif not blind and k in self._blind:
            since = self._blind.pop(k)
            text = "your train is visible in NS data again."
            out.append(event(EventKind.CAN_SEE_TRAIN_AGAIN, text, since.isoformat()))
        zone = timedelta(minutes=30)
        near = any(
            x.riding_or_watched or timedelta(0) <= _utc(x.departure) - _utc(now) <= zone
            for x in legs
        )
        if s.poll_records and offline(health, near) and k not in self._offline:
            self._offline.add(k)
            out.append(
                event(EventKind.OFFLINE, f"NS data is unavailable, {CHECK_APP}.", now.isoformat())
            )
        elif not offline(health, near):
            self._offline.discard(k)
        return out

    async def watchdog_once(self) -> None:
        now = self._clock.now()
        if self._dirty:
            self._save()  # retried writes (rule 22)
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]] = {}
        for k, s in self._st.occurrences.items():
            ctx = self._ctx(s, now)
            if ctx is None or s.not_travelling:
                continue
            t = on_tick(s, ctx)
            events = list(t.events)
            if not _resolved(t.state):
                events += self._health_events(k, t.state, ctx, now)
            if t.state != s or events:
                changes[k] = (t.state, events)
        before = self._st.occurrences
        self._archive(now)  # a window closing is itself no change
        if changes:
            self._apply(changes)
        elif self._st.occurrences != before:
            self._save()
        await self._deliver()

    # --- user actions and location ---

    async def act(self, profile_id: str, action: UserAction) -> None:
        """`choose`, `leaving` and `not travelling` through the serialised writer (7.13)."""
        now = self._clock.now()
        self._create_occurrences(now)
        cfg = self._st.config
        p = _find(cfg, profile_id)
        if p is None or cfg is None:
            return
        mine = [s for s in self._st.occurrences.values() if s.key.profile_id == profile_id]
        target = target_occurrence(mine, action)
        if target is None:
            return
        keys = [target.key]
        if isinstance(action, NotTravelling) and action.whole_day:
            keys = list(whole_day_keys(p, target.key))
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]] = {}
        for key in keys:
            k = _okey(key)
            s = self._st.occurrences.get(k) or self._frozen(_fresh_state(key, cfg.revision, None))
            ctx = self._ctx(s, now)
            if ctx is not None:
                t = apply_action(s, ctx, action)
                changes[k] = (t.state, t.events)
        self._apply(changes)
        await self._deliver()

    async def choose(self, profile_id: str, choice: Choose) -> None:
        await self.act(profile_id, choice)

    async def location(self, profile_id: str, event: LocationEvent) -> None:
        """Every unresolved occurrence of the profile gets the event with a fresh origin answer."""
        mine = [k for k, s in self._st.occurrences.items() if s.key.profile_id == profile_id]
        for k in mine:
            s = self._st.occurrences.get(k)
            now = self._clock.now()
            ctx = None if s is None else self._ctx(s, now)
            if s is None or ctx is None or _resolved(s):
                continue
            gen, p, d = s.generation, ctx.profile, ctx.direction
            got = await self._fetch(Query(p.origin(d), p.destination(d), now), True, None)
            now = self._clock.now()
            self._auth(got if isinstance(got, str) else None, k, now)
            # re-read after the request: an occurrence skipped, archived or edited meanwhile is
            # never written back from the old copy (rule 21)
            s = self._st.occurrences.get(k)
            ctx = None if s is None else self._ctx(s, now)
            if s is None or ctx is None or _resolved(s):
                continue
            # an answer asked under another generation is stale; the report itself still counts
            answer = None if isinstance(got, str) or s.generation != gen else got
            t = on_location(s, ctx, event, answer)
            self._apply({k: (t.state, t.events)})
        await self._deliver()

    # --- reload (5.2, rule 10) ---

    def _refuse(self, errors: Sequence[ProfileError] | str, now: datetime) -> None:
        reason = (
            errors
            if isinstance(errors, str)
            else "; ".join(f"{e.path}: {e.message}" for e in errors)
        )
        self._app_warning(EventKind.PROFILE_REFUSED, f"profile change refused: {reason}.", now)

    async def reload(self) -> None:
        now = self._clock.now()
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            self._refuse(f"cannot read {self._path.name} ({e})", now)
        else:
            old, today = self._st.config, now.astimezone(TZ).date()
            if self.require_stations and self.known_stations is None:
                await self._fetch_stations()
            cfg, errors = load_config(text, self.known_stations, old, today)
            unchecked = self.require_stations and self.known_stations is None
            if unchecked and not errors and cfg is not old:
                # 5.3: an edit (or a first configuration) whose codes cannot be checked waits;
                # the last valid configuration keeps running meanwhile (5.2)
                if not self._unchecked:
                    self._refuse(
                        "the NS station list is unavailable, so station codes cannot be checked;"
                        " the last valid configuration keeps running and the change is tried"
                        " again",
                        now,
                    )
                self._unchecked = True
            else:
                self._unchecked = False
                if errors:
                    self._refuse(errors, now)
                if cfg is not None and cfg is not old:
                    self._apply_config(cfg, now)
        self._apply({})
        await self._deliver()

    def _stations_due(self) -> bool:
        last = self._stations_tried
        gap = timedelta(seconds=POLL_EVERY_S)
        return last is None or _utc(self._clock.now()) - _utc(last) >= gap

    async def _fetch_stations(self) -> None:
        """At most once per poll interval: the station list through the shared budget."""
        if not self._stations_due():
            return
        self._stations_tried = self._clock.now()
        got = await self.stations()
        if got:  # an empty list checks nothing
            self.known_stations = got
            if self.on_stations is not None:
                self.on_stations(got)

    def _apply_config(self, new: Config, now: datetime) -> None:
        old, today = self._st.config, now.astimezone(TZ).date()
        snaps = {**self._st.config_snapshots, new.revision: new}
        occs = {}
        for k, s in self._st.occurrences.items():
            s = self._frozen(s)  # its schedule, read before the history is re-seeded below
            route = _find(snaps.get(s.route_revision) or old, s.key.profile_id)
            p = _find(new, s.key.profile_id)
            if route is not None and p is not None and _same_route(route, p):
                rev = new.revision
            elif s.lock is not None or s.retained or _resolved(s):
                rev = s.route_revision  # locked or riding: runs on its route revision (rule 10)
            else:
                continue  # not locked yet: created again from the new route when its window opens
            moved = replace(s, route_revision=rev, settings_revision=new.revision)
            if _effective(snaps, new, moved) != self._profile_for(s):
                moved = replace(moved, generation=s.generation + 1)  # in-flight results are stale
            occs[k] = moved
        baselines, ledgers = dict(self._st.baselines), dict(self._st.lookahead)
        for p in new.profiles:
            q = _find(old, p.id)
            for d in Direction if q is not None else ():
                assert q is not None
                dk = _dkey(p.id, d)
                hist = baselines.get(dk)
                if hist is None:
                    continue
                if dk in ledgers:
                    ledgers[dk] = reset_on_edit(q, p, d, hist, ledgers[dk], today)
                baselines[dk] = rebase_on_edit(q, p, d, hist, today)
        self._st = replace(
            self._st, config=new, config_snapshots=snaps, occurrences=occs,
            baselines=baselines, lookahead=ledgers,
        )  # fmt: skip

    async def check_file(self) -> None:
        """The file watch: reload when the file's mtime changes (or it disappears)."""
        try:
            mtime: int | None = self._path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._mtime or (self._unchecked and self._stations_due()):  # tried again
            self._mtime = mtime
            await self.reload()

    # --- look-ahead (7.10, rule 21) ---

    async def lookahead_once(self) -> None:
        cfg = self._st.config
        if cfg is None:
            return
        works = await self._works()
        if works is None:  # refused by the shared cooldown: retried by `daily`
            return
        for pid, d in [(p.id, d) for p in cfg.profiles for d in Direction]:
            # the profile as configured now: one held across an earlier direction's requests
            # could be stamped with a newer revision than its own (rule 21)
            p = _find(self._st.config, pid)
            if p is not None:
                await self._lookahead(p, d, works)
        await self._deliver()

    def _lookahead_done(self, since: datetime) -> bool:
        """Every configured direction has a look-ahead run written since `since`."""
        cfg = self._st.config
        leds = [
            self._st.lookahead.get(_dkey(p.id, d))
            for p in (cfg.profiles if cfg else ())
            for d in Direction
        ]
        return all(
            x is not None and x.last_applied is not None and _utc(x.last_applied) >= _utc(since)
            for x in leds
        )

    async def _works(self) -> PlannedWorks | None:
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
            self._settle(before, e, None)
            if isinstance(e, AuthError):
                self._auth("auth", "lookahead", now)
            return PlannedWorks((), "unknown")
        self._settle(before, None, None)
        self._auth(None, "lookahead", now)  # an accepted key ends the outage (7.11)
        return works

    async def stations(self) -> frozenset[str] | None:
        """The NS station list through the shared budget (rule 14); None when refused."""
        if not self._budget.allow(self._clock.now(), live=False):
            return None
        before = self._budget
        self.requests += 1
        try:
            got = await asyncio.wait_for(self._provider.stations(), POLL_DEADLINE.total_seconds())
        except (ProviderError, TimeoutError) as e:
            self._settle(before, e, None)
            return None
        self._settle(before, None, None)
        return got

    async def _lookahead(self, p: Profile, d: Direction, works: PlannedWorks) -> None:
        dk = _dkey(p.id, d)
        start = self._clock.now()
        today = start.astimezone(TZ).date()
        cfg = self._st.config
        assert cfg is not None
        started_revision = cfg.revision
        hist = self._st.baselines.get(dk, BaselineHistory())
        started_baseline = hist.latest
        bases = {
            x: self._st.baselines.get(_dkey(p.id, x), BaselineHistory()).in_force(today)
            for x in Direction
        }
        results: list[ProbeResult] = []
        for day in probe_dates(p, today, bases):
            base = hist.in_force(service_date(p, d, day))
            got = await self._fetch_all(probe_queries(p, d, day, base), False, None)
            if isinstance(got, str):
                self._auth(got, "lookahead", start)
                return  # an incomplete run writes nothing
            self._auth(None, "lookahead", start)
            results += evaluate(p, d, day, got, works, base)
        # write time: the ledger as stored now with this run's start stamps (rule 21)
        cfg = self._st.config
        if cfg is None:
            return
        stored = self._st.lookahead.get(dk) or LookAheadLedger({}, (), None, 0, None)
        ledger = replace(
            stored, started_revision=started_revision, started_baseline=started_baseline
        )
        current = self._st.baselines.get(dk, BaselineHistory())
        others = [x for x in cfg.profiles if x.id != p.id]
        all_bases = {
            (x.id, y): self._st.baselines.get(_dkey(x.id, y), BaselineHistory())
            for x in cfg.profiles
            for y in Direction
            if (x.id, y) != (p.id, d)
        }
        out = run_lookahead(
            p, d, current, ledger, results, cfg.revision, today, start, others, all_bases
        )
        if out is None:
            log.info("look-ahead for %s discarded: configuration or baseline changed", dk)
            return
        new_hist, new_ledger, _ = out
        mine = {o.id for o in stored.pending}
        obligations = (
            *(o for o in self._st.obligations if o.id not in mine),
            *new_ledger.pending,
        )
        self._st = replace(
            self._st,
            baselines={**self._st.baselines, dk: new_hist},
            lookahead={**self._st.lookahead, dk: new_ledger},
            obligations=tuple(obligations),
        )
        self._save()

    # --- start, reconcile and the loops ---

    async def start(self) -> None:
        now = self._clock.now()
        self._since = now  # stored messages wait for the reconcile poll (rules 5, 11)
        try:
            await self._start(now)
        finally:
            self._since = None
        self._apply({})
        await self._deliver()

    async def _start(self, now: datetime) -> None:
        lost = None
        try:
            loaded = self._store.load()
        except StorageError as e:
            loaded, lost = None, str(e)
        if loaded is not None:
            self._st = self._migrated(loaded)
        self._st = replace(self._st, started_before=True)
        if lost is not None:  # rule 22: fresh state, loud, and a write clears "state lost"
            self._app_warning(EventKind.STORAGE_FAILED, f"saved state was lost ({lost}).", now)
        try:
            self._mtime = self._path.stat().st_mtime_ns
        except OSError:
            self._mtime = None
        await self.reload()
        self._create_occurrences(now)
        # deterministic reconcile: one fresh poll per unarchived occurrence (rule 11)
        for k in sorted(self._st.occurrences):
            await self._poll(k)

    def _migrated(self, st: PersistedState) -> PersistedState:
        """A stored state brought into the current model (rule 11): every delivered fact of an
        older store counts as last reported, so the reconcile poll ends the ones that ended (a
        recurrence is news again), and every occurrence gets its own fixed schedule."""
        self._st = st
        occs = {}
        for k, s in st.occurrences.items():
            keys = dict(s.alert_keys)
            for x in s.alert_keys:
                if x.startswith(_SENT):
                    keys.setdefault(_LIVE + x[len(_SENT) :], "1")
            occs[k] = self._frozen(replace(s, alert_keys=keys))
        return replace(st, occurrences=occs)

    async def run_forever(self, *, started: bool = False) -> None:
        """Starts (unless `started`) with the watchdog already running, so a slow reconcile is
        watched too (rule 12); every loop keeps fixed start times, a slow step eats into its own
        interval instead of adding to it (7.1)."""
        c = self._clock

        async def daily() -> None:
            """Today's run once 06:00 has passed, also after a start past it; retried each poll
            interval until every direction's run has written (7.10)."""
            while True:
                now = self._clock.now().astimezone(TZ)
                six = resolve_local(now.date(), LOOKAHEAD_AT, edge="start")
                if _utc(six) <= _utc(now) and self._lookahead_done(six):
                    six = resolve_local(now.date() + timedelta(days=1), LOOKAHEAD_AT, edge="start")
                if _utc(six) > _utc(now):
                    await self._clock.sleep((_utc(six) - _utc(now)).total_seconds())
                    continue
                try:
                    await self.lookahead_once()
                except Exception:
                    log.exception("look-ahead failed")
                if not self._lookahead_done(six):
                    await self._clock.sleep(POLL_EVERY_S)

        async def main() -> None:
            if not started:
                await self.start()
            await asyncio.gather(
                every(c, POLL_EVERY_S, self.poll_once),
                every(c, FILE_EVERY_S, self.check_file),
                daily(),
            )

        # main first: start() loads the store before its first await, so the watchdog never
        # judges the empty state
        await asyncio.gather(main(), every(c, WATCHDOG_EVERY_S, self.watchdog_once))
