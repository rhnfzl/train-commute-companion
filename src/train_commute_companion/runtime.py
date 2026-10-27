"""The runtime around the pure core: polling, the independent watchdog, the shared request
budget, look-ahead, transactional reload and the restart reconcile (7.1, 7.11, 7.12 rules 5,
10, 11, 12, 14, 21, 22, 5.2, 7.13). The only module with a clock, I/O, the notifier and the
webhook sender."""

import asyncio
import copy
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from train_commute_companion.advice import (
    advice_query,
    advise,
    leave_now_text,
    needed,
    still_advised,
)
from train_commute_companion.calendar import TZ, resolve_local
from train_commute_companion.classify import find_leg
from train_commute_companion.delivery import (
    JOURNEY_WARNINGS,
    Obligation,
    WebhookCopy,
    deliver,
    pending_journey_warnings,
    post_copy,
    zone_report_stops_silent,
)
from train_commute_companion.health import PollRecord, WatchedLeg, board_health, cannot_see, offline
from train_commute_companion.lifecycle import (
    Board,
    Choose,
    Ctx,
    DirectionState,
    Leaving,
    LegProgress,
    LegState,
    NotTravelling,
    OccurrenceState,
    UserAction,
    _first_open,
    _left_note,
    _pool,
    apply_action,
    ends,
    location_queries,
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
    _mark,
    carried_ids,
    evaluate,
    forget_pending,
    probe_dates,
    probe_queries,
    rebase_on_edit,
    render_message,
    reset_on_edit,
    run_lookahead,
    service_date,
)
from train_commute_companion.match import usual_connection
from train_commute_companion.messages import CHECK_APP
from train_commute_companion.model import (
    AlertEvent,
    Config,
    Connection,
    Direction,
    EventKind,
    LocationSettings,
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
from train_commute_companion.notify import Notifier, WebhookPort
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
from train_commute_companion.view import (
    DirectionInfo,
    RuntimeView,
    moved,
    occurrence_view,
    works_ahead,
)
from train_commute_companion.webhook import (
    WebhookTestResult,
    body,
    event_id,
    headers,
    probe_body,
    probe_id,
    url_id,
)

log = logging.getLogger(__name__)

POLL_EVERY_S = 120
POLL_DEADLINE = timedelta(seconds=90)
WATCHDOG_EVERY_S = 30
FILE_EVERY_S = 10
LOOKAHEAD_AT = time(6, 0)
BACKOFF_MIN = (2, 4, 8)
# a rejected key: stays at 30 until a success, never stops (the key may be fixed on NS's side)
BACKOFF_AUTH_MIN = (2, 5, 15, 30)
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
# webhooks: one try is cut at TRY_DEADLINE_S in all (httpx.Timeout bounds each read only); a URL
# that failed rests REST_S; a copy is given up GIVE_UP after its first try (owner decision Q1);
# at most MAX_COPIES are owed (one try a minute cannot keep up with faster alerts)
TRY_DEADLINE_S = 5.0
TIMEOUT = httpx.Timeout(TRY_DEADLINE_S)
REST_S = 60
GIVE_UP = timedelta(hours=2)
MAX_COPIES = 200

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
    reason: PollError = "rate"  # which table the step counts in; a skip reports it (7.11)

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
        reason: PollError
        table: tuple[int, ...]
        if isinstance(error, AuthError):
            reason, table = "auth", BACKOFF_AUTH_MIN
        elif isinstance(error, (RateLimited, ServerError)):
            reason, table = "rate", BACKOFF_MIN
        else:
            return self
        step = min((self.backoff_step if self.reason == reason else 0) + 1, len(table))
        until = _utc(now) + timedelta(minutes=table[step - 1])
        if window_end is not None and _utc(now) < _utc(window_end):
            until = min(until, _utc(window_end))
        return RequestBudget(until, step, reason)


def _okey(k: OccurrenceKey) -> str:
    return f"{k.profile_id}|{k.direction.value}|{k.service_date.isoformat()}"


def _dkey(pid: str, d: Direction) -> str:
    return f"{pid}|{d.value}"


def _reasons(errors: Sequence[ProfileError]) -> str:
    return "; ".join(f"{e.path}: {e.message}" for e in errors)


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


_IN_TRY: ContextVar[bool] = ContextVar("tcc_webhook_try", default=False)
# httpx logs each request URL at INFO and httpcore the headers at DEBUG; a logger's filter does
# not cover its child loggers, so each one is named
_QUIETED = ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy")


class _InTryFilter(logging.Filter):
    """Drops every record emitted inside a webhook try: a URL path, a token or a signature
    must never reach a log. Records of other clients and other tasks pass."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _IN_TRY.get()


_FILTER = _InTryFilter()


def _install_log_filter() -> None:
    for name in _QUIETED:
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)


async def _try_once(
    client: httpx.AsyncClient, url: str, body: bytes, hdrs: dict[str, str]
) -> int | str:
    """One POST: the status, or the error's class name (never its text, which names the URL).
    The body of the answer is never read: leaving the block closes it, so a trickling or huge
    answer cannot hold the pass past the deadline."""
    token = _IN_TRY.set(True)
    try:
        async with (
            asyncio.timeout(TRY_DEADLINE_S),
            client.stream(
                "POST", url, content=body, headers=hdrs, timeout=TIMEOUT, follow_redirects=False
            ) as r,
        ):
            return r.status_code
    except (httpx.HTTPError, TimeoutError) as e:  # TimeoutError: the deadline
        return type(e).__name__
    finally:
        _IN_TRY.reset(token)


def _accepted(r: int | str) -> bool:
    return isinstance(r, int) and 200 <= r <= 299


class WebhookPoster:
    """The webhook port over httpx: one try per URL per call, every URL concurrently; a URL
    that failed rests REST_S. One warning per outage, never anything about the phone."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        urls: Sequence[str],
        secret: str,
        now: Callable[[], datetime],
        *,
        token: str = "",
        stopped: Callable[[], bool] = lambda: False,
    ) -> None:
        _install_log_filter()
        self._client, self.urls, self._secret, self._now = client, tuple(urls), secret, now
        self._token, self._stopped = token, stopped
        self.down: dict[str, datetime] = {}  # url -> end of its rest, in UTC (WebhookPort)

    @property
    def failing(self) -> bool:
        return bool(self.down)

    def due(self, urls: Collection[str]) -> frozenset[str]:
        if self._stopped():
            return frozenset()
        t = _utc(self._now())
        return frozenset(u for u in urls if u not in self.down or t >= self.down[u])

    async def post(
        self, event: AlertEvent, msg_id: str, urls: Collection[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        start = self._now()
        due = self.due(urls)
        todo = [u for u in urls if u in due]
        if not todo:
            return frozenset(), frozenset()
        b = body(event)
        hdrs = headers(self._secret, msg_id, int(start.timestamp()), b, self._token)
        results = await asyncio.gather(*(_try_once(self._client, u, b, hdrs) for u in todo))
        ok = set()
        for u, r in zip(todo, results, strict=True):
            host = httpx.URL(u).host
            if _accepted(r):
                ok.add(u)
                if self.down.pop(u, None) is not None:
                    log.info("webhook to %s works again", host)
                continue
            again = u in self.down
            self.down[u] = _utc(start) + timedelta(seconds=REST_S)
            if again:
                log.debug("webhook to %s failed again (%s)", host, r)
            else:
                log.warning("webhook to %s failed (%s); next try after %d s", host, r, REST_S)
        return frozenset(todo), frozenset(ok)


async def send_webhook_tests(
    client: httpx.AsyncClient,
    urls: Sequence[str],
    secret: str,
    now: Callable[[], datetime],
    *,
    token: str = "",
) -> tuple[WebhookTestResult, ...]:
    """Setup test sends (owner decision Q2(b)): a signed loud and a signed silent test to every
    URL, concurrently. Never stored, never retried, and apart from any `WebhookPoster`'s rest
    and `failing`."""
    _install_log_filter()

    async def one(url: str) -> WebhookTestResult:
        got: list[int | str] = []
        for sev in (Severity.LOUD, Severity.SILENT):
            t = now()
            b = probe_body(sev, t)
            hdrs = headers(secret, probe_id(sev, t), int(t.timestamp()), b, token)
            got.append(await _try_once(client, url, b, hdrs))
        res = WebhookTestResult(httpx.URL(url).host, got[0], got[1])
        if res.ok:
            log.info("webhook test to %s: accepted", res.host)
        else:
            log.info("webhook test to %s: not accepted (loud %s, silent %s)", res.host, *got)
        return res

    return tuple(await asyncio.gather(*(one(u) for u in urls)))


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

    def __init__(self, rt: "Runtime", held: bool = False) -> None:
        """`held`: for the alerts of unwritten changes, judged on what the last write tried,
        their own change included (rule 22)."""
        self._rt, self._held = rt, held

    @property
    def _s(self) -> PersistedState:
        """Read at every call: a skip, reload or poll written while a send awaited counts for
        the next message (7.8, rule 5)."""
        rt = self._rt
        return rt._shadow if self._held and rt._shadow is not None else rt._st

    def _occ(self, key: OccurrenceKey) -> OccurrenceState | None:
        return self._s.occurrences.get(_okey(key))

    def skipped(self, key: OccurrenceKey) -> bool:
        s = self._occ(key)
        return s is not None and s.not_travelling

    def fact_holds(self, o: Obligation) -> bool:
        rt = self._rt
        if o.look_ahead is not None:
            pid, d, day = o.look_ahead
            then = _find(self._s.config_snapshots.get(o.route_revision), pid)
            now = _find(self._s.config, pid)
            if now is None or (then is not None and not _same_route(then, now)):
                return False
            if then is None and self._s.config and o.route_revision != self._s.config.revision:
                return False
            hist = self._s.baselines.get(_dkey(pid, d), BaselineHistory())
            versions = {v.version for v in hist.versions}
            if o.baseline_version is not None and o.baseline_version not in versions:
                return False
            return self._look_ahead_holds(o, day)
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

    def _stored_facts(self, o: Obligation) -> tuple[tuple[str, str, bool, bool], ...]:
        assert o.look_ahead is not None
        led = self._s.lookahead.get(_dkey(o.look_ahead[0], o.look_ahead[1]))
        return () if led is None else tuple(led.facts.get(o.id, ()))

    def _live_facts(self, o: Obligation) -> tuple[tuple[str, str, bool, bool], ...] | None:
        """Rule 5(b): the stored facts of a look-ahead message that still hold: a per-date fact
        while it is the latest one the ledger records for its leg; an adoption or its
        withdrawal always (judged by `baseline_version`). None when no facts were stored for it
        (stored before facts were kept). Reads the stored facts, never o.fact_key: ";" also
        joins the legs inside a whole-journey fingerprint."""
        assert o.look_ahead is not None
        led = self._s.lookahead.get(_dkey(o.look_ahead[0], o.look_ahead[1]))
        fs = self._stored_facts(o)
        if led is None or not fs:
            return None
        live = []
        for f in fs:
            parts = f[0].split("|", 2)  # "<date>|<leg>|warn:<fp>[|loud]" or "...|restored"
            if len(parts) < 3 or not (parts[2].startswith("warn:") or parts[2] == "restored"):
                live.append(f)
                continue
            date_, leg, rest = parts
            entries = [_mark(x) for x in led.per_date.get(f"{date_}|{leg}", ())]
            last = next((x for x in reversed(entries) if x.startswith(("warn:", "restored:"))), "")
            if (
                last.startswith("restored:")
                if rest == "restored"
                else last == rest.removesuffix("|loud")
            ):
                live.append(f)
        return tuple(live)

    def _look_ahead_holds(self, o: Obligation, day: date) -> bool:
        """Rule 5(b): a held look-ahead message is dropped once its journey ended or none of its
        facts still holds. An adoption or its withdrawal keeps the whole message."""
        live = self._live_facts(o)
        if live is None or any(k.startswith(("adopt:", "cancel:")) for k, *_ in live):
            return True  # stored before facts were kept, or an adoption: judged as before
        # o.expires_at (the dated journey end) drops it in revalidate; without one (stored before
        # it was kept, or no baseline) the end is unknown: kept until two days after its date
        today = self._rt._clock.now().astimezone(TZ).date()
        if o.expires_at is None and day + timedelta(days=2) <= today:
            return False
        return bool(live)

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
        if e.kind is EventKind.RESTORED:  # back in its journey, and not broken again since
            t = e.train
            on = any(
                x.train == t and x.state not in (LegState.CANCELLED, LegState.RELOCKED)
                for x in legs
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
                ref = rt._usual_arrival(s, ctx, origin)
                return (
                    e.train is not None
                    and a.advised_train == e.train
                    and not a.withdrawn
                    and still_advised(s, ctx, origin, e.train, ref) is not None
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
            for x in self._s.obligations
        )

    def revision_live(self, key: OccurrenceKey, rev: int) -> bool:
        s = self._occ(key)
        if s is None:
            return False
        if s.route_revision == rev:
            return True
        snaps = self._s.config_snapshots
        a, b = (
            _find(snaps.get(rev), key.profile_id),
            _find(snaps.get(s.route_revision), key.profile_id),
        )
        return a is not None and b is not None and _same_route(a, b)

    def in_quiet_hours(self, profile_id: str, now: datetime) -> bool:
        p = _find(self._s.config, profile_id)
        return p is not None and _in_quiet(p.alerts.quiet_hours, now)

    def current(self, o: Obligation) -> AlertEvent:
        if o.look_ahead is not None:
            # rule 5(b): a bundle names only its facts that still hold, from the current profile
            live, p = self._live_facts(o), _find(self._s.config, o.look_ahead[0])
            if live and p is not None and len(live) < len(self._stored_facts(o)):
                kind, text = render_message(p, o.look_ahead[2], live)
                return replace(o.event, kind=kind, text=text, profile_name=p.name)
        e = self._regenerated(o, self._relocked(o, o.event))
        p = _find(self._s.config, e.profile_id)
        if p is None or p.name == e.profile_name:
            return e
        old, new = _title(e.profile_name), _title(p.name)
        text = new + e.text[len(old) :] if e.text.startswith((old + ":", old + ",")) else e.text
        return replace(e, profile_name=p.name, text=text)

    def _regenerated(self, o: Obligation, e: AlertEvent) -> AlertEvent:
        """Rule 5(b): an instruction states its connection as the latest answers show it."""
        rt = self._rt
        s = None if o.occurrence is None else self._s.occurrences.get(_okey(o.occurrence))
        if s is None or e.train is None:
            return e
        k = _okey(s.key)
        ctx, answers = rt._ctx(s, rt._clock.now()), rt._boards.get(k, ())
        if ctx is None or not answers:
            return e
        body = None
        if e.kind is EventKind.LEAVE_NOW:
            origin = _origin_answer(answers, ctx)
            c = (
                None
                if origin is None
                else still_advised(s, ctx, origin, e.train, rt._usual_arrival(s, ctx, origin))
            )
            body = None if c is None else leave_now_text(c, ctx.now.tzinfo)
        elif e.kind is EventKind.NEXT_CONNECTION:
            pool = _pool(answers)
            for j in (*((s.lock,) if s.lock else ()), *s.retained):
                i = next((i for i, x in enumerate(j.legs) if x.train == e.train), None)
                if i is not None:
                    body = _left_note(s, ctx, pool, j, i).text.partition(": ")[2]
                    break
        elif e.kind is EventKind.ALL_GOOD:  # with the platform the latest answers give (7.9)
            g = all_good(
                replace(s, all_good_evaluated=False), Board(answers, (), s.generation), ctx
            )
            body = None if g is None else _twin(g.text)[0].partition(": ")[2]
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
        s = None if o.occurrence is None else self._s.occurrences.get(_okey(o.occurrence))
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


def _held_id(o: Obligation) -> tuple[str | None, str, Severity]:
    """An alert's identity across recomputations of the same change (rule 22)."""
    return None if o.occurrence is None else _okey(o.occurrence), o.event.key, o.event.severity


def _settled(d: Mapping[str, Obligation], i: str, r: Obligation | None) -> dict[str, Obligation]:
    return {k: v for k, v in d.items() if k != i} if r is None else {**d, i: r}


_LEDGER = ("alert_keys", "poll_records")  # composed apart: the newest of each (rule 22)


class _Delta:
    """Rule 22: what a kept change (a user action, a look-ahead run) did to `old`, replayable
    onto a later state: the occurrence fields it changed (not the ledger), the look-ahead
    histories and ledgers, and the obligations it finished or updated. Its new obligations are
    held apart. A newer kept change supersedes what this one did to the same occurrence or
    look-ahead key, and a look-ahead result replays only while its route still runs."""

    def __init__(self, old: PersistedState, new: PersistedState) -> None:
        self.occs: dict[str, OccurrenceState | dict[str, Any] | None] = {}
        for k in old.occurrences.keys() | new.occurrences.keys():
            a, b = old.occurrences.get(k), new.occurrences.get(k)
            if b is None:
                self.occs[k] = None
            elif a is None:
                self.occs[k] = b
            elif diff := {
                f.name: getattr(b, f.name)
                for f in fields(b)
                if f.name not in _LEDGER and getattr(a, f.name) != getattr(b, f.name)
            }:
                self.occs[k] = diff
        self.bases = {k: v for k, v in new.baselines.items() if old.baselines.get(k) != v}
        self.leds = {k: v for k, v in new.lookahead.items() if old.lookahead.get(k) != v}
        self.cfg = new.config
        self.was = {o.id: o for o in old.obligations}
        now = {o.id: o for o in new.obligations}
        self.gone = self.was.keys() - now.keys()
        self.changed = {i: o for i, o in now.items() if i in self.was and self.was[i] != o}

    def supersede(self, newer: "_Delta") -> None:
        for k in newer.occs:
            self.occs.pop(k, None)
        for k in newer.bases.keys() | newer.leds.keys():
            self.bases.pop(k, None)
            self.leds.pop(k, None)

    def _runs(self, st: PersistedState, dk: str) -> bool:
        pid = dk.split("|")[0]
        then, now = _find(self.cfg, pid), _find(st.config, pid)
        return then is not None and now is not None and _same_route(then, now)

    def __call__(self, st: PersistedState) -> PersistedState:
        out = dict(st.occurrences)
        for k, c in self.occs.items():
            cur = out.get(k)
            if c is None:
                out.pop(k, None)
            elif isinstance(c, dict):
                if cur is not None:
                    out[k] = replace(cur, **c)
            else:
                out[k] = (
                    c
                    if cur is None
                    else replace(c, alert_keys=cur.alert_keys, poll_records=cur.poll_records)
                )
        obls = tuple(self.changed.get(o.id, o) for o in st.obligations if o.id not in self.gone)
        bases = {k: v for k, v in self.bases.items() if self._runs(st, k)}
        leds = {k: v for k, v in self.leds.items() if self._runs(st, k)}
        return replace(
            st,
            occurrences=out,
            baselines={**st.baselines, **bases},
            lookahead={**st.lookahead, **leds},
            obligations=obls,
        )


def _prune(st: PersistedState) -> PersistedState:
    """The configuration snapshots still referenced (rule 5(c) judges obligations on theirs),
    computed on the whole write, held alerts included (rule 22)."""
    live = {s.route_revision for s in st.occurrences.values()}
    live |= {st.config.revision} if st.config else set()
    live |= {o.route_revision for o in st.obligations}
    live |= {o.route_revision for v in st.lookahead.values() for o in v.pending}
    live |= {c.obligation.route_revision for c in st.webhook_copies}  # read by fact_holds
    snaps = {r: c for r, c in st.config_snapshots.items() if r in live}
    return st if snaps == st.config_snapshots else replace(st, config_snapshots=snaps)


def _advice_told(st: PersistedState, told: Sequence[tuple[Obligation, bool]]) -> PersistedState:
    """7.6: the advice episode a finished LEAVE_NOW belongs to is delivered once sent, and
    ended silently once dropped unsent, so the same train can be advised again."""
    occs = dict(st.occurrences)
    for o, sent in told:
        k = None if o.occurrence is None else _okey(o.occurrence)
        s = None if k is None else occs.get(k)
        a = None if s is None else s.advice
        if k is None or s is None or a is None or a.sent_at is None:
            continue
        if a.advised_train != o.event.train or _utc(a.sent_at) != _utc(o.event.created_at):
            continue  # an older episode's message
        a = replace(a, delivered=True) if sent else replace(a, withdrawn=True)
        occs[k] = replace(s, advice=a)
    return replace(st, occurrences=occs)


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
    """Every answer asked from the origin as one, so an earlier train searched for advice and
    the usual or locked train are judged together (7.6)."""
    origin = ctx.profile.origin(ctx.direction)
    mine = [a for a in answers if a.query.origin == origin]
    if not mine:
        return None
    # newest first: a connection with a train a newer answer also shows is superseded, so an
    # older healthy copy never hides a newer cancellation from advice (7.6)
    conns: list[Connection] = []
    for a in sorted(mine, key=lambda a: _utc(a.fetched_at), reverse=True):
        newer = {g.train for c in conns for g in c.legs}
        conns += [c for c in a.connections if not any(g.train in newer for g in c.legs)]
    newest = max(mine, key=lambda a: _utc(a.fetched_at))
    return ProviderAnswer(mine[0].query, tuple(conns), newest.fetched_at)


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
        lookahead_at: time = LOOKAHEAD_AT,
        webhooks: WebhookPort | None = None,
    ) -> None:
        self._path = profile_path
        self._webhooks = webhooks
        # rule 22: the one live record of owed webhook copies, written with every save and never
        # rebuilt from `_st` (a failed write resets `_st`). `_held_copies`: copies of alerts
        # held in memory, judged with the held view. `_copied`: obligations whose copy was made
        # since the last delivery write landed (a failed write resets their `webhook_copied`)
        self._copies: dict[str, WebhookCopy] = {}
        self._held_copies: set[str] = set()
        self._copied: set[str] = set()
        self._lookahead_at = lookahead_at
        self._refusal: str | None = None
        self._started = False
        self._store = store
        self._provider = provider
        self.events_log: list[AlertEvent] = []
        self._notifier = _LoggingNotifier(notifier, self.events_log)
        self._clock = clock
        self.known_stations = known_stations  # None: codes unchecked (replays, tests)
        self._st = PersistedState(None, {}, {}, {}, {}, (), False)
        self._budget = RequestBudget()
        self._store_ok = True
        # rule 22: only a written state is applied. `_st` is the last written (or loaded) state
        # and everything runs from it; while writes fail it only gains what unwritten changes
        # observed and told (poll records, the delivery ledger in `_marks`), never their
        # decisions. `_durable` is that written state as written. Pending until one write lands:
        # `_base` the newest change, retried by the watchdog (a poll's is recomputed anyway);
        # `_ops` the changes nothing recomputes (a user action, a look-ahead run, delivery
        # outcomes), replayed onto whatever is written next; `_held` their new alerts, sent from
        # memory and judged on `_shadow` (the last state a write tried); `_gone` obligations
        # finished meanwhile; `_told` alerts sent from memory. `_notes`: `storage_failed`, which
        # never uses the store
        self._durable = self._st
        self._base: PersistedState | None = None
        self._ops: list[Callable[[PersistedState], PersistedState]] = []
        self._marks: dict[str, Mapping[str, str]] = {}
        self._held: dict[str, Obligation] = {}
        self._gone: set[str] = set()
        self._told: set[tuple[str | None, str, Severity]] = set()
        self._shadow: PersistedState | None = None
        self._notes: dict[str, Obligation] = {}
        self._refused_told: str | None = None
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
        self._post_task: asyncio.Task[None] | None = None  # the running webhook pass
        self._post_again = False
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
        # profile id -> location source epoch, bumped on every accepted location change (8):
        # an event read under an older epoch is dropped, even after a switch back (A, B, A)
        self._loc_epoch: dict[str, int] = {}

    # --- configuration and context ---

    @property
    def config(self) -> Config | None:
        return self._st.config

    @property
    def state(self) -> PersistedState:
        return self._st

    @property
    def refusal(self) -> str | None:
        """Why the last reload refused the file (or waits for the station list); None if not."""
        return self._refusal

    @property
    def store_ok(self) -> bool:
        return self._store_ok

    @property
    def key_rejected(self) -> bool:
        return self._auth_outage

    @property
    def started(self) -> bool:
        return self._started

    def now(self) -> datetime:
        return self._clock.now()

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

    def view(self) -> RuntimeView:
        """A frozen snapshot for shells (5.4): nothing in it changes as the runtime goes on. It
        shows the written state (rule 22); an unsent alert counts whether written or not."""
        live = [o for o in self._st.obligations if o.id not in self._gone]
        mem = (*self._held.values(), *self._notes.values())
        return self._view(sum(o.attempts > 0 for o in (*live, *mem)))

    def _view(self, undelivered: int) -> RuntimeView:
        now = self._clock.now()
        st, cfg = self._st, self._st.config
        occurrences = []
        for k, s in sorted(st.occurrences.items()):
            ctx = self._ctx(s, now)
            if ctx is not None:
                occurrences.append(occurrence_view(s, ctx, self._boards.get(k, ())))
        # rule 10: a removed profile still runs on its occurrences, so its entities stay
        # deep copies: a profile's mappings (`transfer_min`, zones) stay the runtime's own
        profiles = list(cfg.profiles if cfg else ())
        running = [s for s in st.occurrences.values() if _find(cfg, s.key.profile_id) is None]
        for pid in sorted({s.key.profile_id for s in running}):
            newest = max(
                (s for s in running if s.key.profile_id == pid), key=lambda s: s.key.service_date
            )
            p = self._profile_for(newest)
            if p is not None:
                profiles.append(p)
        directions = []
        for p in profiles:
            for d in Direction:
                routes = [
                    _find(st.config_snapshots.get(s.route_revision), p.id)
                    for s in st.occurrences.values()
                    if s.key.profile_id == p.id and s.key.direction is d
                ]
                n = max(len(r.journey(d).transfers) for r in (p, *routes) if r is not None)
                directions.append(DirectionInfo(p.id, p.name, d, n))
        today = now.astimezone(TZ).date()
        return RuntimeView(
            now,
            self._started,
            copy.deepcopy(tuple(profiles)),
            tuple(directions),
            tuple(occurrences),
            {k: works_ahead(v, today) for k, v in st.lookahead.items()},
            moved(cfg.profiles if cfg else (), st.baselines),
            self._refusal,
            self._store_ok,
            self._auth_outage,
            undelivered,
            self.requests,
            dict(self._loc_epoch),
            {
                k: led.last_run if led else None
                for p in (cfg.profiles if cfg else ())
                for d in Direction
                for k in (f"{p.id}|{d.value}",)
                for led in (st.lookahead.get(k),)
            },
        )

    # --- the serialised writer: state, ledger and obligations in one save (rule 5, 22) ---

    def _obligation(self, e: AlertEvent, s: OccurrenceState | None, now: datetime) -> Obligation:
        # a LEAVE_NOW has no fixed expiry: still_advised checks its current departure minus
        # buffer at send time (is_good), so a delayed escape train still catchable goes out
        if s is not None:
            revision = s.route_revision
        else:
            revision = self._st.config.revision if self._st.config else 0
        # one poll shares `now` and the key names neither profile nor direction (7.12 rule 6)
        d = e.direction.value if e.direction else ""
        return Obligation(
            f"{e.profile_id}|{d}|{e.key}|{e.severity.value}|{now.isoformat()}",
            e,
            None if s is None else s.key,
            None,
            revision,
            None if s is None else s.baseline_version,
            e.key,
            None,
            0,
        )

    def _app_obligation(self, kind: EventKind, body: str, now: datetime, tag: str) -> Obligation:
        e = AlertEvent(
            "", _APP, None, None, None, kind, Severity.LOUD, None,
            f"{_title(_APP)}: {body}", f"{kind.value}:{tag}:{now.isoformat()}", now,
        )  # fmt: skip
        return self._obligation(e, None, now)

    def _app_warning(self, kind: EventKind, body: str, now: datetime, tag: str = "") -> None:
        o = self._app_obligation(kind, body, now, tag)
        self._st = replace(self._st, obligations=(*self._st.obligations, o))

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
        keep: bool = False,
        stage: bool = True,
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
        self._save(keep, stage)

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
        self._st = replace(self._st, occurrences=occs)  # snapshots are pruned on write

    def _save(
        self,
        keep: bool = False,
        stage: bool = True,
        cand: PersistedState | None = None,
        op: Callable[[PersistedState], PersistedState] | None = None,
    ) -> bool:
        """Rule 22: writes the changed state (`cand`, else `_st`) as one, with every change
        still waiting; only once written is it applied. A failed write leaves the last written
        state running and keeps the change: as the newest to retry (`stage`), and when nothing
        recomputes it (`keep`, or its own `op`) to replay onto whatever is written next. Its new
        alerts go from memory. A first configuration runs anyway: nothing else could."""
        cand = self._st if cand is None else cand
        # only what this change did: restaging carries other waiting changes it must not replace
        mine = _Delta(self._durable, cand) if op is None and keep else None
        if self._base is not None and cand is not self._base:
            cand = self._restage(self._durable, cand)  # what waits on other occurrences too
        if mine is not None:
            op = mine
            for x in self._ops:  # a newer action supersedes an older one (rule 22)
                if isinstance(x, _Delta):
                    x.supersede(mine)
        try:
            composed = self._compose(cand)
            port = self._webhooks
            down = {} if port is None else {url_id(u): t for u, t in port.down.items()}
            write = _prune(
                replace(composed, webhook_copies=tuple(self._copies.values()), webhook_down=down)
            )
            self._store.save(write)
        except StorageError as e:
            self._pend(cand, stage, op)
            if self._store_ok:  # one loud storage_failed per outage, held in memory only
                self._store_ok = False
                log.error("store write failed: %s", e)
                o = self._app_obligation(
                    EventKind.STORAGE_FAILED,
                    "could not save state, alerts may repeat or be lost.",
                    self._clock.now(),
                    "",
                )
                self._notes[o.id] = o
            return False
        self._st = self._durable = write
        self._base, self._ops, self._marks, self._shadow = None, [], {}, None
        self._held, self._gone, self._told = {}, set(), set()
        self._store_ok = True
        return True

    def _compose(self, cand: PersistedState) -> PersistedState:
        """`cand` with every change still waiting (rule 22): the kept changes replayed, the
        newest poll records and delivery ledger, held alerts in and finished ones out."""
        if self._shadow is None and not self._held and not self._gone:
            return cand
        st = cand
        for op in self._ops:
            st = op(st)
        occs = {}
        for k, s in st.occurrences.items():
            # the running state holds the newest of both; an occurrence it lacks, the marks
            cur = self._st.occurrences.get(k)
            occs[k] = (
                replace(s, alert_keys=self._marks.get(k, s.alert_keys))
                if cur is None
                else replace(s, alert_keys=cur.alert_keys, poll_records=cur.poll_records)
            )
        known = {o.id for o in self._durable.obligations}
        taken = {_held_id(o) for o in self._held.values()} | self._told
        obls = [
            self._held.get(o.id, o)
            for o in st.obligations
            if o.id not in self._gone
            and (o.id in known or o.id in self._held or _held_id(o) not in taken)
        ]
        ids = {o.id for o in obls}
        obls += [o for o in self._held.values() if o.id not in ids]
        ledgers = {
            k: replace(
                v,
                pending=tuple(self._held.get(o.id, o) for o in v.pending if o.id not in self._gone),
            )
            for k, v in st.lookahead.items()
        }
        return replace(st, occurrences=occs, obligations=tuple(obls), lookahead=ledgers)

    def _pend(
        self,
        cand: PersistedState,
        stage: bool,
        op: Callable[[PersistedState], PersistedState] | None,
    ) -> None:
        """A failed write (rule 22): the change waits, the last written state runs on with
        what the change observed and told (poll records, delivery ledger), its new alerts are
        held to go from memory."""
        base = self._durable
        if op is not None:
            self._ops.append(op)
        if stage or self._base is None:
            self._base = self._restage(base, cand)
        known = {o.id for o in base.obligations}
        taken = {_held_id(o) for o in self._held.values()} | self._told
        for o in cand.obligations:
            if o.id in known or o.id in self._held or o.id in self._gone or _held_id(o) in taken:
                continue  # a recomputed alert already held or sent from memory is not again
            self._held[o.id] = o
            taken.add(_held_id(o))
        for k, x in self._st.occurrences.items():  # the newest ledger, computed on the marks
            was = base.occurrences.get(k)
            if k in self._marks or was is None or was.alert_keys != x.alert_keys:
                self._marks[k] = x.alert_keys
        if base.config is None and cand.config is not None:
            base = replace(base, config=cand.config, config_snapshots=cand.config_snapshots)
            self._durable = base
        occs = {}
        for k, x in base.occurrences.items():
            cur = self._st.occurrences.get(k)
            occs[k] = replace(
                x,
                alert_keys=self._marks.get(k, x.alert_keys),
                poll_records=x.poll_records if cur is None else cur.poll_records,
            )
        self._st = replace(base, occurrences=occs)
        # a journey fact told from memory that a healthy poll ended leaves the ledger: its
        # recurrence is news again (7.5), as it is with a working store
        self._told = {
            t
            for t in self._told
            if t[0] is None
            or (s := self._st.occurrences.get(t[0])) is not None
            and _SENT + t[1] in s.alert_keys
        }
        self._shadow = self._compose(self._base)

    def _restage(self, base: PersistedState, cand: PersistedState) -> PersistedState:
        """The newest change to retry: `cand`, keeping every occurrence an earlier waiting
        change made that `cand` left as written (a poll of another occurrence, a no-op)."""
        old = self._base
        if old is None:
            return cand

        def bare(x: OccurrenceState) -> OccurrenceState:
            return replace(x, alert_keys={}, poll_records=())

        def revs(x: OccurrenceState | None) -> tuple[int, int] | None:
            return None if x is None else (x.route_revision, x.settings_revision)

        # a waiting configuration `cand` left as written is not carried (the file watch tries
        # it again, 5.2), and neither is what it did to an occurrence: never revisions alone
        stale = old.config != base.config and cand.config == base.config
        occs = {}
        for k in old.occurrences.keys() | cand.occurrences.keys():
            c, o, w = cand.occurrences.get(k), old.occurrences.get(k), base.occurrences.get(k)
            if c is None and w is not None:
                continue  # ended by `cand`
            if stale and w is not None and revs(o) != revs(w):
                if c is not None:
                    occs[k] = c  # computed on the written configuration
            elif c is not None and (
                w is None and o is None or w is not None and bare(c) != bare(w)
            ):
                occs[k] = c
            elif o is not None:
                occs[k] = o
        return replace(cand, occurrences=occs)

    async def _deliver(self) -> None:
        """Every pending obligation, revalidated right before sending (rule 5). Merges by id,
        so obligations added by another task while a send awaited are kept. Serialised: the
        poll, watchdog and file watch all deliver, and two passes would send one alert twice.
        The webhook copies go after, in a task of their own: a slow receiver never holds up a
        phone send or the loop that delivers (an alert's advice can expire)."""
        async with self._sending:
            await self._deliver_pass()
        self._post_soon()

    def _post_soon(self) -> None:
        """A webhook pass now, or one more round of the running one (copies made meanwhile)."""
        if self._webhooks is None:
            return
        if self._post_task is not None and not self._post_task.done():
            self._post_again = True
            return
        self._post_task = asyncio.get_running_loop().create_task(self._post_passes())

    async def _post_passes(self) -> None:
        port = self._webhooks
        assert port is not None
        again = True
        while again:
            self._post_again = False
            before = dict(self._copies)
            try:
                await self._post_copies(port, _View(self), _View(self, held=True))
            except Exception:  # a broken pass must not stop the next one
                log.exception("webhook pass failed")
            finally:  # a copy that ended or was tried is recorded, also when shutdown cancels
                if self._copies != before:
                    self._delivered(set(), {}, (), True)
            again = self._post_again

    async def webhooks_idle(self) -> None:
        """Waits until no webhook pass runs (a clean shutdown, tests)."""
        while (t := self._post_task) is not None and not t.done():
            await asyncio.wait({t})

    async def _stop_posting(self) -> None:
        if (t := self._post_task) is not None and not t.done():
            t.cancel()
            await asyncio.wait({t})

    def _waits(self, o: Obligation) -> bool:
        """Not judged yet: written before this start (judged once the reconcile ran, rule 11),
        or judged on observations while its occurrence has no healthy answer (rule 5b)."""
        if self._since is not None and _utc(o.event.created_at) < _utc(self._since):
            return True
        k = None if o.occurrence is None else _okey(o.occurrence)
        return o.event.kind in _OBSERVED and k in self._st.occurrences and k not in self._boards

    def _drop_copy(self, cid: str) -> None:
        self._copies.pop(cid, None)
        self._held_copies.discard(cid)

    def _copy(self, o: Obligation) -> bool:
        """The webhook copy of an obligation handed to the phone; True when one was made."""
        self._copied.add(o.id)
        cid = event_id(o.id)
        if cid in self._copies:
            return False
        if len(self._copies) >= MAX_COPIES:  # overload: a never-tried copy goes first (Q1)
            pool = [c for c in self._copies.values() if c.first_try is None]
            old = min(
                pool or self._copies.values(), key=lambda c: _utc(c.obligation.event.created_at)
            )
            self._drop_copy(old.id)
            log.warning("dropped a webhook copy: more than %d owed", MAX_COPIES)
        self._copies[cid] = WebhookCopy(cid, o)
        if o.id in self._held:
            self._held_copies.add(cid)
        return True

    def _merge_copy(self, port: WebhookPort, c: WebhookCopy, r: WebhookCopy | None) -> None:
        """A copy's try result, merged into what is there now: `act`, a poll or the look-ahead
        run can change the copies while the POST awaits."""
        cur = self._copies.get(c.id)
        if cur is None:
            return  # skipped or given up meanwhile: never restored
        if cur is c:
            if r is None:
                self._drop_copy(c.id)
            elif r is not c:
                self._copies[c.id] = r
            return
        if r is None:  # every URL `c` still owed took the text sent at the try
            self._drop_copy(c.id)
            return
        tries = [t for t in (cur.last_try, r.last_try) if t is not None]
        merged = replace(
            cur,
            done=cur.done + tuple(u for u in r.done if u not in cur.done),
            first_try=cur.first_try or r.first_try,
            last_try=max(tries, key=_utc, default=None),
        )
        if all(url_id(u) in merged.done for u in port.urls):
            self._drop_copy(c.id)
        else:
            self._copies[c.id] = merged

    async def _deliver_pass(self) -> None:
        view = _View(self)  # reads the state as it is when each message is judged (rule 5)
        held_view = _View(self, held=True)
        finished: set[str] = set()
        kept: dict[str, Obligation] = {}
        told: list[tuple[Obligation, bool]] = []  # finished LEAVE_NOW, sent (else dropped)
        port = self._webhooks
        copies = False  # the webhook copies changed
        try:
            for o in (*self._st.obligations, *self._held.values(), *self._notes.values()):
                if o.id in self._gone:
                    continue  # finished while the store was down (rule 22)
                mem = o.id in self._held or o.id in self._notes
                if not mem and all(x.id != o.id for x in self._st.obligations):
                    continue  # removed by a change written while an earlier send awaited
                if self._waits(o):
                    continue
                if port is not None and not o.webhook_copied and o.id not in self._copied:
                    copies |= self._copy(o)
                # a fresh clock per message: a slow send must not leave the next one judged early
                n = len(self.events_log)  # the logging notifier appends what it sent
                v = held_view if o.id in self._held else view
                r = await deliver(o, v, self._notifier, self._clock.now())
                sent = len(self.events_log) > n
                if r is not None and port is not None and not r.webhook_copied:
                    r = replace(r, webhook_copied=True)
                if r is None and o.event.kind is EventKind.LEAVE_NOW:
                    told.append((o, sent))
                # where it is now: a write landing during the send may have written a held one
                if o.id in self._notes:
                    self._notes = _settled(self._notes, o.id, r)
                elif o.id in self._held:
                    self._held = _settled(self._held, o.id, r)
                    if r is None:
                        self._gone.add(o.id)
                        if sent:
                            self._told.add(_held_id(o))
                elif r is None:
                    finished.add(o.id)
                elif r != o:
                    kept[o.id] = r
        finally:  # each acknowledged send is recorded, also when shutdown cancels the pass
            self._delivered(finished, kept, told, copies)

    async def _post_copies(self, port: WebhookPort, view: _View, held_view: _View) -> None:
        """After the phone sends: each owed copy once. Never tried first, oldest first, then
        least recently tried first, so a failing URL's one try per rest goes to every copy in
        turn. The give-up check comes before every wait (owner decision Q1)."""
        for c in sorted(
            self._copies.values(),
            key=lambda c: (
                c.first_try is not None,
                _utc(c.last_try or c.obligation.event.created_at),
                _utc(c.obligation.event.created_at),
            ),
        ):
            if self._copies.get(c.id) is not c:
                continue  # removed or replaced since
            now = self._clock.now()
            if c.first_try is not None and _utc(now) - _utc(c.first_try) >= GIVE_UP:
                self._drop_copy(c.id)
                log.warning("gave up a webhook copy after 2 h")
                continue
            if self._waits(c.obligation):
                continue
            v = held_view if c.id in self._held_copies else view
            cur = c

            def started(x: WebhookCopy) -> None:  # the first try is written before it goes out
                nonlocal cur
                cur = self._copies[x.id] = x
                self._delivered(set(), {}, (), True)

            r = await post_copy(c, v, port, now, started)
            self._merge_copy(port, cur, r)

    def _delivered(
        self,
        finished: set[str],
        kept: Mapping[str, Obligation],
        told: Sequence[tuple[Obligation, bool]],
        copies: bool = False,
    ) -> None:
        if not finished and not kept and not told and not copies:
            return

        def outcome(st: PersistedState) -> PersistedState:
            def survive(xs: Iterable[Obligation]) -> tuple[Obligation, ...]:
                return tuple(kept.get(o.id, o) for o in xs if o.id not in finished)

            # look-ahead messages live in their ledger too, so a delivered one counts as told
            ledgers = {k: replace(v, pending=survive(v.pending)) for k, v in st.lookahead.items()}
            st = _advice_told(st, told)
            return replace(st, obligations=survive(st.obligations), lookahead=ledgers)

        self._st = outcome(self._st)
        self._archive(self._clock.now())
        self._gone |= finished
        if self._save(stage=False, op=outcome):
            self._copied.clear()  # their `webhook_copied` is written

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
            # skipped by the shared backoff: counts toward board health (7.1), and a rejected
            # key still reads as one through its cooldown (7.11)
            return self._budget.reason
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
        self,
        queries: Sequence[Query],
        live: bool,
        window_end: datetime | None,
        deadline: datetime | None = None,
    ) -> tuple[ProviderAnswer, ...] | PollError:
        """All or nothing: a poll missing one answer cannot judge the legs it was for (rule 2).
        One hard deadline for the whole poll, not one per request (7.1)."""
        answers = []
        deadline = deadline or _utc(self._clock.now()) + POLL_DEADLINE
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
                occs[k] = replace(
                    fresh,
                    baseline_signature=own,
                    baseline_frozen=True,
                    alert_keys=self._marks.get(k, {}),  # told while the store was down (r22)
                )
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
        gen, end = s.generation, ctx.schedule.window_end
        deadline = _utc(now) + POLL_DEADLINE  # one hard deadline for every request (7.1)
        got = await self._fetch_all(queries_needed(s, ctx), True, end, deadline)
        extra: ProviderAnswer | PollError | None = None
        if not isinstance(got, str) and (usual := _origin_answer(got, ctx)) is not None:
            # 7.6: an earlier catchable train is before the time the poll asked from; a failed
            # search leaves advice to the poll's own answers and never fails the poll
            q = advice_query(s, ctx, usual, self._usual_arrival(s, ctx, usual))
            extra = None if q is None else await self._fetch(q, True, end, deadline)
            got = got if extra is None or isinstance(extra, str) else (*got, extra)
        now = self._clock.now()
        s = self._st.occurrences.get(k)
        ctx = None if s is None else self._ctx(s, now)
        if s is None or ctx is None:
            return
        error = got if isinstance(got, str) else None
        if error is None and extra == "auth":
            # 7.11: a rejected key on the advice search is reported like any other, loudly;
            # the main answers still count as observations
            error = "auth"
        elif isinstance(extra, str):
            log.warning("advice search for %s failed (%s)", k, extra)
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
            if (merged := _origin_answer(answers, ctx)) is not None:
                # no reference: an absent or broken usual still makes waiting bad (7.6)
                arrival = self._usual_arrival(st, ctx, merged)
                record, more = advise(st, ctx, merged, arrival)
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
        if self._shadow is not None:  # the waiting changes, written again (rule 22)
            self._save(stage=False, cand=self._base)
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
            self._apply(changes, stage=False)
        elif self._st.occurrences != before:
            self._save(stage=False)
        await self._deliver()

    # --- user actions and location ---

    async def act(
        self, profile_id: str, action: UserAction, direction: Direction | None = None
    ) -> bool:
        """`choose`, `leaving` and `not travelling` through the serialised writer (7.13).
        `direction`: only that direction's occurrences are targeted (both share a date).
        False when nothing was targeted: an unknown profile, or no occurrence to act on. A
        removed profile still running on an occurrence is known (rule 10)."""
        now = self._clock.now()
        self._create_occurrences(now)
        mine = [
            s for s in self._st.occurrences.values()
            if s.key.profile_id == profile_id and direction in (None, s.key.direction)
        ]  # fmt: skip
        target = target_occurrence(mine, action)
        p = None if target is None else self._profile_for(target)
        if target is None or p is None:
            return False
        keys = [target.key]
        if isinstance(action, NotTravelling) and action.whole_day:
            keys = list(whole_day_keys(p, target.key))  # its own route revision's day_offset
        if isinstance(action, NotTravelling):  # written with the skip: an archived occurrence
            for c in list(self._copies.values()):  # would no longer read as skipped
                if c.obligation.event.kind in JOURNEY_WARNINGS and c.obligation.occurrence in keys:
                    self._drop_copy(c.id)
        cfg = self._st.config
        revision = cfg.revision if cfg else target.route_revision
        changes: dict[str, tuple[OccurrenceState, Sequence[AlertEvent]]] = {}
        for key in keys:
            k = _okey(key)
            s = self._st.occurrences.get(k) or self._frozen(_fresh_state(key, revision, None))
            ctx = self._ctx(s, now)
            if ctx is not None:
                t = apply_action(s, ctx, action)
                changes[k] = (t.state, t.events)
        self._apply(changes, keep=True)  # nothing recomputes a user action (rule 22)
        await self._deliver()
        return True

    async def choose(self, profile_id: str, choice: Choose) -> bool:
        return await self.act(profile_id, choice)

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
                s for s in self._st.occurrences.values()
                if s.key.profile_id == profile_id and not _resolved(s)
                and service_date in (None, s.key.service_date)
                and direction in (None, s.key.direction)
            ),
            key=lambda s: s.key.service_date,
        )  # fmt: skip
        named = _find(self._st.config, profile_id) or next(
            (p for s in cands if (p := self._profile_for(s)) is not None), None
        )
        gone = f"no journey of {_title(named.name if named else profile_id)} is being followed now"
        hhmm = f"{departure:%H:%M}"

        def named_leg(x: LegProgress) -> bool:
            t = x.train.planned_departure.astimezone(TZ)
            return (
                x.state is not LegState.DONE
                and (t.hour, t.minute) == (departure.hour, departure.minute)
                and train_number in (None, x.train.train_number)
            )

        # a train of the lock or of a retained remainder: the one the user may ride (N3)
        hit = next(
            (
                (s, x)
                for s in cands
                for j in (*((s.lock,) if s.lock else ()), *s.retained)
                for x in j.legs
                if named_leg(x)
            ),
            None,
        )
        occ: OccurrenceState | None
        ctx: Ctx | None
        if hit is not None:
            occ, x = hit
            ctx = self._ctx(occ, now)
            station, when = x.board_station, x.train.planned_departure
        else:
            # at a transfer station: target_occurrence would skip it as riding
            occ = next(
                (s for s in cands if s.lock and 0 < _first_open(s.lock) < len(s.lock.legs)),
                None,
            ) or target_occurrence(cands, Leaving(service_date, now))
            ctx = None if occ is None else self._ctx(occ, now)
            if occ is None or ctx is None:
                return gone
            w = ctx.schedule
            n = 0 if occ.lock is None else _first_open(occ.lock)
            lock = occ.lock
            if lock and n:
                # an onward train continues the kept incoming leg: dated from it, not the window
                station = lock.legs[n - 1].alight_stop
                lo = _utc(lock.legs[n - 1].train.planned_departure)
                hi = lo + timedelta(days=1)
            else:
                station = ctx.profile.origin(ctx.direction)
                lo, hi = _utc(w.window_start), _utc(w.window_end)
            first = lo.astimezone(TZ).date()
            inside = sorted(
                (
                    t for d in (first, first + timedelta(days=1)) for edge in ("start", "end")
                    if lo <= _utc(t := resolve_local(d, departure, edge=edge)) < hi
                ),
                key=_utc,
            )  # fmt: skip
            if not inside:
                return f"{hhmm} is outside the {ctx.direction.value} window"
            when = inside[0]
        if occ is None or ctx is None:
            return gone
        k, end = _okey(occ.key), ctx.schedule.window_end
        # rule 21: what the choice is judged on must still hold when it is written
        mark = (occ.generation, occ.route_revision, occ.baseline_version)
        # one deadline for every request of the choice: the shell waits 120 s in all
        deadline = _utc(self._clock.now()) + POLL_DEADLINE
        boards: tuple[ProviderAnswer, ...] = ()
        if station != ctx.profile.origin(ctx.direction):
            # the change from the kept incoming leg is judged on a fresh observation of it,
            # never on an older poll's answer (rule 1)
            more = await self._fetch_all(queries_needed(occ, ctx), True, end, deadline)
            self._auth(more if isinstance(more, str) else None, k, self._clock.now())
            if isinstance(more, str):
                await self._deliver()
                return f"NS did not answer ({more}); try again"
            boards = more
        got = await self._fetch(
            Query(station, ctx.profile.destination(ctx.direction), when), True, end, deadline
        )
        self._auth(got if isinstance(got, str) else None, k, self._clock.now())
        if isinstance(got, str):
            await self._deliver()
            return f"NS did not answer ({got}); try again"
        minute = _utc(when).replace(second=0, microsecond=0)
        same = [
            c for c in got.connections
            if c.legs and c.legs[0].board.code == station
            and _utc(c.legs[0].train.planned_departure).replace(second=0, microsecond=0) == minute
        ]  # fmt: skip
        # 6.1: two trains in one boarding minute are uncertain even by number, so is_good refuses
        if len({(c.legs[0].train.operating_date, c.legs[0].train.train_number) for c in same}) > 1:
            return f"two trains leave {station} at {hhmm}; NS data cannot tell them apart"
        found = [c for c in same if train_number in (None, c.legs[0].train.train_number)]
        if not found:
            return f"no train at {hhmm} from {station} in NS data"
        far = datetime.max.replace(tzinfo=UTC)
        ranked = sorted(found, key=lambda c: _utc(c.legs[-1].alight.planned_arrival or far))
        # rule 21: a reload during the request may have removed or archived the occurrence
        now_occ = self._st.occurrences.get(k)
        if now_occ is None or _resolved(now_occ):
            return gone
        if (now_occ.generation, now_occ.route_revision, now_occ.baseline_version) != mark:
            return "your journey changed while NS answered; try again"
        d = occ.key.direction
        mine = [
            s for s in self._st.occurrences.values()
            if s.key.profile_id == profile_id and s.key.direction is d
        ]  # fmt: skip

        def refusal(choice: Choose) -> str | None:
            """The check `act` applies, ridden prefix included, as a dry run (rule 1)."""
            t = target_occurrence(mine, choice)
            tctx = None if t is None else self._ctx(t, self._clock.now())
            if t is None or tctx is None:
                return gone
            events = apply_action(t, tctx, choice).events
            return next((e.text for e in events if e.kind is EventKind.CHOOSE_REFUSED), None)

        choices = [Choose(occ.key.service_date, c, (got, *boards)) for c in ranked]
        why = [refusal(c) for c in choices]
        # the earliest arrival that passes; when none does, the earliest is refused loudly
        i = next((n for n, r in enumerate(why) if r is None), 0)
        ok = await self.act(profile_id, choices[i], d)
        return why[i] if ok else gone

    async def location(
        self,
        profile_id: str,
        event: LocationEvent,
        source: LocationSettings | None = None,
        epoch: int | None = None,
    ) -> None:
        """Every unresolved occurrence of the profile gets the event with a fresh origin answer.
        With `source` (the settings the event was read under), a profile whose location settings
        differ by the time it would be written drops the event: a switched, disabled or replaced
        source supplies no evidence (8). With `epoch` (`RuntimeView.location_epochs` when the fix
        arrived), any location change since then drops it too, a switch back included."""

        def stale(ctx: Ctx) -> bool:
            if epoch is not None and epoch != self._loc_epoch.get(profile_id, 0):
                return True
            return source is not None and ctx.profile.location != source

        mine = [k for k, s in self._st.occurrences.items() if s.key.profile_id == profile_id]
        for k in mine:
            s = self._st.occurrences.get(k)
            now = self._clock.now()
            ctx = None if s is None else self._ctx(s, now)
            if s is None or ctx is None or _resolved(s) or stale(ctx):
                continue
            gen, p, d = s.generation, ctx.profile, ctx.direction
            got = await self._fetch(Query(p.origin(d), p.destination(d), now), True, None)
            qs = location_queries(s, ctx, event)  # the locked trains, from where they leave
            more = await self._fetch_all(qs, True, None) if qs else ()
            now = self._clock.now()
            self._auth(got if isinstance(got, str) else None, k, now)
            # re-read after the request: an occurrence skipped, archived or edited meanwhile is
            # never written back from the old copy (rule 21)
            s = self._st.occurrences.get(k)
            ctx = None if s is None else self._ctx(s, now)
            if s is None or ctx is None or _resolved(s) or stale(ctx):
                continue
            # an answer asked under another generation is stale; the report itself still counts
            answer = None if isinstance(got, str) or s.generation != gen else got
            watched = () if isinstance(more, str) or s.generation != gen else more
            t = on_location(s, ctx, event, answer, watched)
            self._apply({k: (t.state, t.events)})
        await self._deliver()

    # --- reload (5.2, rule 10) ---

    def _refuse(self, errors: Sequence[ProfileError] | str, now: datetime) -> None:
        reason = errors if isinstance(errors, str) else _reasons(errors)
        self._refusal = reason
        if not self._store_ok and reason == self._refused_told:
            return  # the same file retried while its write fails (rule 22): told once
        self._refused_told = reason
        self._app_warning(EventKind.PROFILE_REFUSED, f"profile change refused: {reason}.", now)

    async def reload(self) -> None:
        now = self._clock.now()
        self._refusal = None
        applied: Config | None = None
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
        if applied is not None and self._shadow is not None:
            self._mtime = None  # not written, so not active: the file watch tries again (r22)
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
        was = {p.id: p.location for p in (old.profiles if old else ())}
        now_loc = {p.id: p.location for p in new.profiles}
        for pid in was.keys() | now_loc.keys():
            if was.get(pid) != now_loc.get(pid):
                self._loc_epoch[pid] = self._loc_epoch.get(pid, 0) + 1
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
            # removed and added again: its old history goes, so it starts from the file like a
            # new profile (a first configuration has no earlier profiles to forget)
            for d in Direction if q is None and old is not None else ():
                baselines.pop(_dkey(p.id, d), None)
                ledgers.pop(_dkey(p.id, d), None)
            for d in Direction if q is not None else ():
                assert q is not None
                dk = _dkey(p.id, d)
                hist = baselines.get(dk)
                if hist is None:
                    continue
                if dk in ledgers:
                    ledgers[dk] = reset_on_edit(q, p, d, hist, ledgers[dk], today)
                    if not _same_route(q, p):  # rule 5(c) drops its pending messages
                        ledgers[dk] = forget_pending(ledgers[dk])
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
        elif self.require_stations and self.known_stations is None and self._stations_due():
            # nothing waits, but station zones need the list (8): a failed fetch is retried (7.11)
            await self._fetch_stations()
            self._apply({})
            await self._deliver()

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
        now = self._clock.now()
        if not self._budget.allow(now, live=False):
            return None
        before = self._budget
        self.requests += 1
        try:
            got = await asyncio.wait_for(self._provider.stations(), POLL_DEADLINE.total_seconds())
        except (ProviderError, TimeoutError) as e:
            self._settle(before, e, None)
            if isinstance(e, AuthError):
                self._auth("auth", "stations", now)
            return None
        self._settle(before, None, None)
        self._auth(None, "stations", now)  # an accepted key ends the outage (7.11)
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
        new_ledger = self._copies_through(p, d, stored, current, new_hist, new_ledger)
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
        self._save(keep=True, stage=False)

    def _copies_through(
        self,
        p: Profile,
        d: Direction,
        stored: LookAheadLedger,
        current: BaselineHistory,
        new_hist: BaselineHistory,
        new_ledger: LookAheadLedger,
    ) -> LookAheadLedger:
        """A look-ahead run as owed webhook copies see it (rules 5, 18): a copy carried by
        `retire` follows its message, a copy whose bundled adoption was withdrawn after its
        phone send keeps its other facts, and every owed copy keeps the facts it is judged on.
        A copy changed here is judged on what the write tried (the held view) until it lands:
        a failed write must not drop it against the state before this run (rule 22)."""
        was = {o.id: o for o in stored.pending}
        pending = {o.id: o for o in new_ledger.pending}
        for old, new in carried_ids(stored, new_ledger).items():
            if old not in was or new not in pending:
                continue
            n = replace(pending[new], webhook_copied=True)
            c = next((c for c in self._copies.values() if c.obligation.id == old), None)
            if c is not None:  # the same copy: its webhook-id, receipts and tries stay
                self._copies[c.id] = replace(c, obligation=n)
                self._held_copies.add(c.id)
                pending[new] = n
            elif was[old].webhook_copied or old in self._copied:  # it ended: no second one
                pending[new] = n
        rewritten: dict[str, tuple[tuple[str, str, bool, bool], ...]] = {}
        gone = {v.version for v in current.versions} - {v.version for v in new_hist.versions}
        for c in list(self._copies.values()):
            o = c.obligation
            if o.look_ahead is None or o.look_ahead[:2] != (p.id, d) or o.id in was:
                continue
            if o.baseline_version not in gone:
                continue
            fs = stored.facts.get(o.id, ())
            rest = tuple(f for f in fs if f[0] != f"adopt:{o.baseline_version}")
            if not rest or len(rest) == len(fs):
                continue  # only the adoption (dropped at its next try), or none bundled
            day = o.look_ahead[2]
            kind, text = render_message(p, day, rest)
            b = new_hist.in_force(day)
            o = replace(
                o,
                baseline_version=b.version if b else None,
                fact_key=";".join(k for k, *_ in rest),
                event=replace(o.event, kind=kind, text=text),
            )
            self._copies[c.id] = replace(c, obligation=o)
            self._held_copies.add(c.id)
            rewritten[o.id] = rest
        owed = {c.obligation.id for c in self._copies.values()}
        carry = {i: f for i, f in stored.facts.items() if i in owed}
        return replace(
            new_ledger,
            pending=tuple(pending.values()),
            facts={**carry, **rewritten, **new_ledger.facts},
        )

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
        self._started = True

    async def _start(self, now: datetime) -> None:
        lost = None
        try:
            loaded = self._store.load()
        except StorageError as e:
            loaded, lost = None, str(e)
        if loaded is not None:
            self._st = self._migrated(loaded)
            if (port := self._webhooks) is not None:
                self._copies = {c.id: c for c in loaded.webhook_copies}
                # a restart keeps each URL's rest and outage: no extra try, no second warning
                saved = loaded.webhook_down
                port.down.update({u: saved[url_id(u)] for u in port.urls if url_id(u) in saved})
            elif n := len(loaded.webhook_copies):
                # nothing could post them, and a later poster would get stale alerts
                log.info("dropped %d webhook copies: webhooks are off", n)
        self._st = self._durable = replace(self._st, started_before=True)
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
            a = s.advice
            # a record saved before `delivered` loads as told; its LEAVE_NOW still owed was not
            if (
                a is not None
                and not a.withdrawn
                and a.sent_at is not None
                and any(
                    o.event.kind is EventKind.LEAVE_NOW
                    and o.occurrence is not None
                    and _okey(o.occurrence) == k
                    and o.event.train == a.advised_train
                    and _utc(o.event.created_at) == _utc(a.sent_at)
                    for o in st.obligations
                )
            ):
                s = replace(s, advice=replace(a, delivered=False))
            occs[k] = self._frozen(replace(s, alert_keys=keys))
        obligations = tuple(  # stored with the planned departure frozen as expiry
            replace(o, expires_at=None) if o.event.kind is EventKind.LEAVE_NOW else o
            for o in st.obligations
        )
        # an id stored before it named profile and direction is shared by two profiles' alerts
        ids = Counter(o.id for o in obligations)
        obligations = tuple(
            o
            if ids[o.id] == 1
            else replace(
                o,
                id=f"{o.event.profile_id}|"
                f"{o.event.direction.value if o.event.direction else ''}|{o.id}",
            )
            for o in obligations
        )
        return replace(st, occurrences=occs, obligations=obligations)

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
                if _utc(six) <= _utc(now) and self._lookahead_done(six):
                    six = resolve_local(
                        now.date() + timedelta(days=1), self._lookahead_at, edge="start"
                    )
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
        try:
            await asyncio.gather(main(), every(c, WATCHDOG_EVERY_S, self.watchdog_once))
        finally:
            await self._stop_posting()
