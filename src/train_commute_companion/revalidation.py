"""What a held message is judged on right before it goes (7.12 rule 5): the stored state and the
latest healthy answers, read through an explicit view. Pure apart from reading them."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from train_commute_companion.advice import (
    leave_now_text,
    still_advised,
    usual_arrival,
    waiting_is_bad,
)
from train_commute_companion.calendar import TZ, hhmm, to_utc
from train_commute_companion.classify import current_run, find_leg
from train_commute_companion.delivery import (
    APP_WARNINGS,
    JOURNEY_WARNINGS,
    zone_report_stops_silent,
)
from train_commute_companion.health import WatchedLeg
from train_commute_companion.lifecycle import (
    DELAY_NEWS,
    LegProgress,
    OccurrenceState,
    PollAnswers,
    left_note,
    legacy_facts,
    merged_pool,
    origin_answer,
)
from train_commute_companion.lookahead import entry_mark, render_message
from train_commute_companion.lookahead_probe import BaselineHistory, LookAheadFact, split_fact_key
from train_commute_companion.model import (
    LIVE_PREFIX,
    SENT_PREFIX,
    AlertEvent,
    AlertFacts,
    Ctx,
    Direction,
    EventKind,
    LegState,
    Obligation,
    OccurrenceKey,
    Severity,
    TrainId,
    direction_key,
    display_name,
    okey,
    present,
    train_ident,
)
from train_commute_companion.news import all_good
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.revisions import find_profile, occurrence_ctx, profile_for, same_route
from train_commute_companion.store import PersistedState
from train_commute_companion.unclear import train_key

# ongoing facts a poll restates while they last: a held one holds while its key is live (rule 5)
_ONGOING = frozenset(
    {
        EventKind.DELAYED,
        EventKind.TRANSFER_AT_RISK,
        EventKind.PLATFORM_CHANGE,
        EventKind.NO_CONNECTION,
    }
)
# judged on the latest healthy answers (rule 5b): held while an occurrence has none yet
OBSERVED = frozenset(
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
_FINISHED_LEG_STATES = (LegState.DONE, LegState.CANCELLED, LegState.RELOCKED)


@dataclass(slots=True)
class PollMemory:
    """Per occurrence, what the runtime remembers of its polls and never stores."""

    # the latest healthy answers, what rule 5(b) regenerates messages from
    boards: dict[str, tuple[ProviderAnswer, ...]] = field(default_factory=dict)
    blind: dict[str, datetime] = field(default_factory=dict)  # start of its blindness outage
    offline: set[str] = field(default_factory=set)
    # before a lock: the usual train as the last healthy answer showed it
    before: dict[str, tuple[WatchedLeg, ...]] = field(default_factory=dict)
    # per train: what the last healthy poll showed of it (rule 13)
    seen: dict[str, dict[TrainId, WatchedLeg]] = field(default_factory=dict)

    def forget(self, k: str) -> None:
        for held in (self.boards, self.blind, self.before, self.seen):
            held.pop(k, None)
        self.offline.discard(k)


def _in_quiet(window: tuple[time, time] | None, now: datetime) -> bool:
    if window is None:
        return False
    t, (a, b) = now.astimezone(TZ).time(), window
    return a <= t < b if a <= b else (t >= a or t < b)


def alert_facts(e: AlertEvent) -> AlertFacts:
    return legacy_facts(e) if e.facts is None else e.facts


def states_current(e: AlertEvent, keys: Mapping[str, str]) -> bool:
    """A delay message whose stated values are still the ones `lifecycle.keep_stated` remembers."""
    return all(keys.get(t.slot) == t.said for t in alert_facts(e).told)


def covers(x: Obligation, o: Obligation) -> bool:
    """Rule 5(b): `x` is a newer message about what `o` reported (same occurrence and train,
    the same kind or both delay news). Rule 5(a): a warning is never covered."""

    def family(k: EventKind) -> frozenset[EventKind]:
        return DELAY_NEWS if k in DELAY_NEWS else frozenset({k})

    return (
        o.occurrence is not None
        and o.event.kind not in APP_WARNINGS | JOURNEY_WARNINGS
        and x.id != o.id
        and x.occurrence == o.occurrence
        and x.event.kind in family(o.event.kind)
        and x.event.train == o.event.train
        and to_utc(x.event.created_at) > to_utc(o.event.created_at)
    )


def leg_now(answers: Sequence[ProviderAnswer], x: LegProgress) -> tuple[str, str]:
    """A locked leg as the latest answers show it: departure (expected, else planned) and
    its platform, for a message naming it."""
    leg = find_leg(merged_pool(answers), x.train, x.board_station)[0] if answers else None
    run = None
    if leg is not None:
        run = current_run(answers, leg.train, x.board_station, x.intended_alight)
    g = None if run is None else run[1].leg.board
    dep = None if g is None else g.departure
    t = dep.value if dep is not None and dep.value is not None else x.train.planned_departure
    track = None if g is None else g.actual_track or g.planned_track
    return hhmm(t), f" (platform {track})" if track else ""


def _broken(s: OccurrenceState, t: TrainId | None) -> bool:
    """A live break (cancelled, cut short, missing) is held for train `t`."""
    ident = "" if t is None else f":{train_ident(t)}:"
    return any(
        key.startswith(LIVE_PREFIX + b) and ident in key for key in s.alert_keys for b in _BREAKS
    )


def _current_body(
    e: AlertEvent,
    train: TrainId,
    s: OccurrenceState,
    ctx: Ctx,
    answers: tuple[ProviderAnswer, ...],
) -> str | None:
    if e.kind is EventKind.LEAVE_NOW:
        origin = origin_answer(answers, ctx)
        c = (
            None
            if origin is None
            else still_advised(s, ctx, origin, train, usual_arrival(s, ctx, origin))
        )
        return None if c is None else leave_now_text(c, ctx.now.tzinfo)
    if e.kind is EventKind.NEXT_CONNECTION:
        pool = merged_pool(answers)
        for j in (*((s.lock,) if s.lock else ()), *s.retained):
            i = next((i for i, x in enumerate(j.legs) if x.train == train), None)
            if i is not None:
                return left_note(s, ctx, pool, j, i).body
        return None
    if e.kind is EventKind.ALL_GOOD:  # with the platform the latest answers give (7.9)
        g = all_good(
            replace(s, all_good_evaluated=False), PollAnswers(answers, (), s.generation), ctx
        )
        return None if g is None else g.body
    return None


class LiveLedger:
    """The delivery ledger view (7.12 rule 5): judges a message on the state and on what the
    runtime remembers of the latest polls. `written` reads the running state and `judged` the
    state a message is judged on, both at every call: a skip, reload or poll written while a send
    awaited counts for the next message (7.8, rule 5). For the alerts of unwritten changes
    `judged` is what the last write tried, their own change included (rule 22)."""

    def __init__(
        self,
        written: Callable[[], PersistedState],
        judged: Callable[[], PersistedState],
        memory: PollMemory,
        now: Callable[[], datetime],
    ) -> None:
        self._written, self._judged, self._memory, self._now = written, judged, memory, now

    @property
    def _state(self) -> PersistedState:
        return self._judged()

    def _ctx(self, s: OccurrenceState) -> Ctx | None:
        return occurrence_ctx(self._written(), s, self._now())

    def _occurrence(self, key: OccurrenceKey) -> OccurrenceState | None:
        return self._state.occurrences.get(okey(key))

    def skipped(self, key: OccurrenceKey) -> bool:
        s = self._occurrence(key)
        return s is not None and s.not_travelling

    def fact_holds(self, o: Obligation) -> bool:
        if o.look_ahead is not None:
            return self._look_ahead_route_holds(o, o.look_ahead) and self._look_ahead_holds(
                o, o.look_ahead[2]
            )
        if o.occurrence is None:
            return True
        s = self._occurrence(o.occurrence)
        if s is None or s.not_travelling or not self._fact_still_true(o, s):
            return False
        p = profile_for(self._written(), s)
        if o.event.severity is Severity.SILENT and p is not None:
            d = s.key.direction
            role = "home" if p.destination(d) == p.home_station else "work"
            zone = (p.location.zones if p.location else {}).get(role, role)
            return not zone_report_stops_silent(s, {p.destination(d), zone})
        return True

    def _look_ahead_route_holds(self, o: Obligation, key: tuple[str, Direction, date]) -> bool:
        pid, d, _ = key
        then = find_profile(self._state.config_snapshots.get(o.route_revision), pid)
        now = find_profile(self._state.config, pid)
        if now is None or (then is not None and not same_route(then, now)):
            return False
        if then is None and self._state.config and o.route_revision != self._state.config.revision:
            return False
        hist = self._state.baselines.get(direction_key(pid, d), BaselineHistory())
        versions = {v.version for v in hist.versions}
        return o.baseline_version is None or o.baseline_version in versions

    def _stored_facts(self, o: Obligation) -> tuple[LookAheadFact, ...]:
        pid, d, _ = present(o.look_ahead)
        led = self._state.lookahead.get(direction_key(pid, d))
        return () if led is None else tuple(led.facts.get(o.id, ()))

    def _live_facts(self, o: Obligation) -> tuple[LookAheadFact, ...] | None:
        """Rule 5(b): the stored facts of a look-ahead message that still hold: a per-date fact
        while it is the latest one the ledger records for its leg (a `...|unclear:<fp>` fact
        holds while `unclear:<fp>` is the latest unclear mark of its key); an adoption or its
        withdrawal always (judged by `baseline_version`). None when its ledger holds no facts for
        it, as for a message stored before facts were kept: they cannot be rebuilt on load, since
        ";" also joins the legs inside a whole-journey fingerprint, so never reads o.fact_key."""
        pid, d, _ = present(o.look_ahead)
        led = self._state.lookahead.get(direction_key(pid, d))
        if led is None or not (fs := led.facts.get(o.id)):
            return None
        live = []
        for f in fs:
            dated = split_fact_key(f.key)
            if dated is None:
                live.append(f)
                continue
            key, rest = dated
            entries = [entry_mark(x) for x in led.per_date.get(key, ())]
            last = next(
                (x for x in reversed(entries) if x.startswith(("warn:", "unclear:", "restored:"))),
                "",
            )
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
        if live is None or any(f.adopts or f.cancels for f in live):
            return True  # no facts to judge, or an adoption: the whole message holds
        # o.expires_at (the dated journey end) drops it in revalidate; without one (no baseline,
        # or a message stored before it was kept) the end is unknown: kept until two days after
        today = self._now().astimezone(TZ).date()
        if o.expires_at is None and day + timedelta(days=2) <= today:
            return False
        return bool(live)

    def _fact_still_true(self, o: Obligation, s: OccurrenceState) -> bool:
        """Rule 5(b): the message's fact, regenerated from the current state and the latest
        healthy answers."""
        e, k, mem = o.event, okey(s.key), self._memory
        legs = [x for j in (*((s.lock,) if s.lock else ()), *s.retained) for x in j.legs]
        unfinished = any(x.train == e.train and x.state not in _FINISHED_LEG_STATES for x in legs)
        # the delay memory still says what the message stated
        if e.kind in DELAY_NEWS and not states_current(e, s.alert_keys):
            return False
        if e.kind in (EventKind.NEARLY_ON_TIME, EventKind.BACK_TO_NORMAL):
            return unfinished and not _broken(s, e.train)  # no all-clear for a broken train
        if e.kind in _ONGOING:  # a healthy poll that stopped reporting it ended it
            return SENT_PREFIX + o.fact_key in s.alert_keys
        if e.kind is EventKind.CANNOT_SEE_TRAIN:
            return k in mem.blind
        if e.kind is EventKind.CAN_SEE_TRAIN_AGAIN:
            return k not in mem.blind
        if e.kind is EventKind.OFFLINE:
            return k in mem.offline
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
        if e.kind is EventKind.ALL_GOOD:
            return self._all_good_holds(e, s)
        if e.kind in (EventKind.LEAVE_NOW, EventKind.EARLIER_TRAIN_WITHDRAWN):
            return self._advice_holds(e, s)
        if e.kind is EventKind.DATA_UNCLEAR:  # while its values are the latest told for its train
            told = None if e.train is None else s.alert_keys.get(f"unclear#{train_key(e.train)}")
            return told == alert_facts(e).unclear
        # the answer to a command the user gave is true as said; a new kind must say what holds it
        return e.kind is EventKind.CHOOSE_REFUSED

    def _all_good_holds(self, e: AlertEvent, s: OccurrenceState) -> bool:
        """Still proven now, on the same terms as the first time."""
        k = okey(s.key)
        ctx, answers = self._ctx(s), self._memory.boards.get(k, ())
        if ctx is None or not answers or k in self._memory.blind or s.lock is None:
            return False
        if s.lock.legs[0].train != e.train:  # it named the journey a relock replaced
            return False
        poll = PollAnswers(answers, (), s.generation)
        return all_good(replace(s, all_good_evaluated=False), poll, ctx) is not None

    def _advice_holds(self, e: AlertEvent, s: OccurrenceState) -> bool:
        ctx, answers = self._ctx(s), self._memory.boards.get(okey(s.key), ())
        a, origin = s.advice, None if ctx is None else origin_answer(answers, ctx)
        if a is None or ctx is None or origin is None:
            return False
        if e.kind is EventKind.LEAVE_NOW:
            ref = usual_arrival(s, ctx, origin)
            return (
                e.train is not None
                and a.advised_train == e.train
                and not a.withdrawn
                and still_advised(s, ctx, origin, e.train, ref) is not None
            )
        return a.withdrawn and not waiting_is_bad(s, ctx, origin, usual_arrival(s, ctx, origin))

    def newer_covers(self, o: Obligation) -> bool:
        return any(covers(x, o) for x in self._state.obligations)

    def revision_live(self, key: OccurrenceKey, rev: int) -> bool:
        s = self._occurrence(key)
        if s is None:
            return False
        if s.route_revision == rev:
            return True
        snaps = self._state.config_snapshots
        a, b = (
            find_profile(snaps.get(rev), key.profile_id),
            find_profile(snaps.get(s.route_revision), key.profile_id),
        )
        return a is not None and b is not None and same_route(a, b)

    def in_quiet_hours(self, profile_id: str, now: datetime) -> bool:
        p = find_profile(self._state.config, profile_id)
        return p is not None and _in_quiet(p.alerts.quiet_hours, now)

    def current(self, o: Obligation) -> AlertEvent:
        if o.look_ahead is not None:
            # rule 5(b): a bundle names only its facts that still hold, from the current profile
            live, p = self._live_facts(o), find_profile(self._state.config, o.look_ahead[0])
            if live and p is not None and len(live) < len(self._stored_facts(o)):
                kind, text = render_message(p, o.look_ahead[2], live)
                return replace(o.event, kind=kind, text=text, profile_name=p.name)
        e = self._regenerated(o, self._relocked(o, o.event))
        p = find_profile(self._state.config, e.profile_id)
        if p is None or p.name == e.profile_name:
            return e
        old, new = display_name(e.profile_name), display_name(p.name)
        text = new + e.text[len(old) :] if e.text.startswith((old + ":", old + ",")) else e.text
        return replace(e, profile_name=p.name, text=text)

    def _regenerated(self, o: Obligation, e: AlertEvent) -> AlertEvent:
        """Rule 5(b): an instruction states its connection as the latest answers show it."""
        s = None if o.occurrence is None else self._state.occurrences.get(okey(o.occurrence))
        if s is None or e.train is None:
            return e
        ctx, answers = self._ctx(s), self._memory.boards.get(okey(s.key), ())
        if ctx is None or not answers:
            return e
        body = _current_body(e, e.train, s, ctx, answers)
        if body is None:
            return e
        return replace(e, text=f"{display_name(e.profile_name)}: {body}{alert_facts(e).twin_note}")

    def _relocked(self, o: Obligation, e: AlertEvent) -> AlertEvent:
        """Rule 5(a): a journey warning names the current lock, not the replacement frozen at
        write time. Kept as written while the lock still holds the warned train (nothing better
        to name) or still ends in the whole replacement the text names. Otherwise the stale
        clause is replaced: the lock's first train as the next good connection while nothing
        of it has left, else the train now followed and every onward leg still ahead."""
        s = None if o.occurrence is None else self._state.occurrences.get(okey(o.occurrence))
        if e.kind not in _NAMES_LOCK or e.train is None or s is None or s.lock is None:
            return e
        f = alert_facts(e)
        live = [x for x in s.lock.legs if x.state not in (LegState.CANCELLED, LegState.RELOCKED)]
        if not live or e.train in {x.train for x in live} or f.on:
            return e  # a retained warning already names its own next connection (rule 7)
        if f.named and tuple(train_ident(x.train) for x in live)[-len(f.named) :] == f.named:
            return e
        head = e.text.removesuffix(f.twin_note).removesuffix(f.next_clause)
        answers = self._memory.boards.get(okey(s.key), ())
        ahead = [x for x in live if x.state in (LegState.PENDING, LegState.BOARDING)]
        left = [x for x in live if x not in ahead]
        if not left:
            nxt = "Next good connection: {}{}.".format(*leg_now(answers, ahead[0]))
        else:
            now = hhmm(left[-1].train.planned_departure)
            onward = "".join(
                f", then the {t} from {x.board_station}{pl}"
                for x in ahead
                for t, pl in (leg_now(answers, x),)
            )
            nxt = f"Now following your {now} train{onward}."
        return replace(e, text=f"{head.rstrip()} {nxt}{f.twin_note}")
