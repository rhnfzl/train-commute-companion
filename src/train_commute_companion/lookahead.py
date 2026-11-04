"""Planned-works look-ahead and timetable baselines (7.10, 7.12 rules 18, 19, 21, Q2). Pure."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import NamedTuple

from train_commute_companion.calendar import TZ, is_commute_day, minute_of_day, resolve_local
from train_commute_companion.classify import classify_leg, current_connection, merge_arrivals
from train_commute_companion.match import matches_pattern, planned_departure, usual_connection
from train_commute_companion.messages import data_unclear_text
from train_commute_companion.model import (
    FOLLOW_LIMIT,
    AlertEvent,
    Connection,
    Direction,
    EventKind,
    Leg,
    LegKind,
    Obligation,
    Profile,
    Severity,
    Signature,
    SignatureLeg,
    TrainId,
    TransferState,
    display_name,
)
from train_commute_companion.profile import find_overlaps
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.transfer import check_transfer
from train_commute_companion.unclear import Unclear, disagreement, newest_copies, train_key

PROBE_DAYS = 3
HORIZON_DAYS = 10
SEARCH_WINDOW = timedelta(minutes=15)
# change tolerances of look-ahead 23; a unanimous run adopts any difference (7.12 rule 18)
DEP_TOL_MIN = 5
DUR_TOL_MIN = 10
REPLACEMENT_HINT = "Check replacement transport nearer the day."
_MISSING = "missing"
_PATTERN = "pattern:"


@dataclass(frozen=True, slots=True)
class BaselineVersion:
    version: int
    signature: Signature
    effective_from: date
    confirmed: bool


@dataclass(frozen=True, slots=True)
class BaselineHistory:
    versions: tuple[BaselineVersion, ...] = ()
    floor: int = 0  # highest number retired (re-seed or cancellation): numbers never repeat

    def in_force(self, day: date) -> BaselineVersion | None:
        live = [v for v in self.versions if v.effective_from <= day]
        return max(live, key=lambda v: (v.effective_from, v.version), default=None)

    @property
    def latest(self) -> int | None:
        return max((v.version for v in self.versions), default=None)

    def _next(self) -> int:
        return max(self.latest or 0, self.floor) + 1


@dataclass(frozen=True, slots=True)
class ProbeReason:
    """Why a probe counts as works. `token` is how it reads in a stored fingerprint."""

    tag: str  # missing, pattern, cancelled, cut_short, messages, maintenance or short
    leg: int | None = None
    stop: str | None = None  # where a leg is cut short, or the station of a short change
    margin: str | None = None  # a short change's minutes, "?" when unknown
    minimum: int | None = None  # the minutes that change needs

    @property
    def token(self) -> str:
        parts = (self.tag, self.leg, self.stop, self.margin, self.minimum)
        return ":".join(str(x) for x in parts if x is not None)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    direction: Direction
    service_date: date
    leg_index: int | None  # None: the whole journey
    connection: Connection | None
    clean: bool
    transfer_ok: bool
    head: str  # the signature text, "pattern:" and it for another pattern, or "missing"
    reasons: tuple[ProbeReason, ...] = ()
    works_known: bool = True  # False when planned_works() coverage was unknown (look-ahead 36)
    # (train key, where its copies in one answer disagree on the user's trip): rule 23
    unclear: tuple[tuple[str, Unclear], ...] = ()

    @property
    def works(self) -> bool:
        return bool(self.reasons)

    @property
    def fingerprint(self) -> str:
        """What the ledger marks compare, so its text must not change."""
        tail = ",".join(x.token for x in self.reasons)
        return self.head + ("|" + tail if tail else "")

    @property
    def other_pattern(self) -> bool:
        return self.head.startswith(_PATTERN)


class LookAheadFact(NamedTuple):
    """One statement of a look-ahead message, stored as the list [key, text, loud, warning]."""

    key: str
    text: str
    loud: bool
    warning: bool  # the message then closes with the replacement hint

    @property
    def warns(self) -> bool:
        return "|warn:" in self.key

    @property
    def adopts(self) -> bool:
        return self.key.startswith("adopt:")

    @property
    def cancels(self) -> bool:
        return self.key.startswith("cancel:")


@dataclass(frozen=True, slots=True)
class LookAheadLedger:
    # "<service date>|<leg index or j>" -> entries; a warn/loud/restored mark ends in
    # "#<id of the obligation that carries it>", read with entry_mark/_owner
    per_date: Mapping[str, tuple[str, ...]]
    pending: tuple[Obligation, ...]
    last_run: datetime | None  # last successful run (look-ahead health only)
    started_revision: int
    started_baseline: int | None
    last_applied: datetime | None = None  # start time of the last run whose writes were applied
    # pending obligation id -> its facts, so one fact can be withdrawn from a message that
    # bundles several (7.12 rule 18)
    facts: Mapping[str, tuple[LookAheadFact, ...]] = field(default_factory=dict)


# ---- the stored key grammars, each built and read here


WHOLE_JOURNEY = "j"  # a day key's leg part for the journey probe


def day_key(sd: date, leg_index: int | None) -> str:
    """The ledger key of one probe on one date."""
    return f"{sd.isoformat()}|{WHOLE_JOURNEY if leg_index is None else leg_index}"


def unclear_day_key(sd: date, train: str) -> str:
    """The ledger key of one train's disagreeing copies on one date."""
    return f"{sd.isoformat()}|u:{train}"


def split_day_key(key: str) -> tuple[date, str]:
    """A ledger key's date and its leg part, as `day_key` or `unclear_day_key` wrote them."""
    day, _, leg = key.partition("|")
    return date.fromisoformat(day), leg


def adopt_key(version: int) -> str:
    return f"adopt:{version}"


def cancel_key(version: int) -> str:
    return f"cancel:{version}"


def split_fact_key(key: str) -> tuple[str, str] | None:
    """A per-date fact key's ledger key and mark ("warn:<fp>[|loud]", "unclear:<fp>" or
    "restored"). None for any other fact: an adoption or its withdrawal."""
    parts = key.split("|", 2)
    if len(parts) < 3 or not (parts[2].startswith(("warn:", "unclear:")) or parts[2] == "restored"):
        return None
    return f"{parts[0]}|{parts[1]}", parts[2]


def bundle_key(facts: Iterable[LookAheadFact]) -> str:
    """An obligation's fact key: the keys of the facts its message bundles."""
    return ";".join(f.key for f in facts)


def bundle_names(fact_key: str, key: str) -> bool:
    return key in fact_key.split(";")


# ---- signatures


def signature_of(c: Connection, service_date: date) -> Signature:
    legs = []
    for leg in c.legs:
        dep = planned_departure(leg)
        arr = leg.alight.planned_arrival
        local = dep.astimezone(TZ)
        dur = 0 if arr is None else int((arr.astimezone(UTC) - dep).total_seconds() // 60)
        legs.append(
            SignatureLeg(
                leg.board.code,
                leg.alight.code,
                local.time().replace(second=0, microsecond=0, fold=0),
                (local.date() - service_date).days,
                dur,
            )
        )
    return Signature(tuple(legs))


def signature_text(s: Signature) -> str:
    return ";".join(
        f"{x.board}>{x.alight}@{x.departure:%H:%M}+{x.day_offset}/{x.duration_min}" for x in s.legs
    )


def _abs_min(x: SignatureLeg) -> int:
    return x.day_offset * 1440 + minute_of_day(x.departure)


def _duration(s: Signature) -> int:
    return _abs_min(s.legs[-1]) + s.legs[-1].duration_min - _abs_min(s.legs[0])


def _differs(a: Signature, b: Signature, dep_tol: int, dur_tol: int) -> bool:
    if len(a.legs) != len(b.legs) or abs(_duration(a) - _duration(b)) >= dur_tol:
        return True
    return any(
        (x.board, x.alight) != (y.board, y.alight)
        or abs(_abs_min(x) - _abs_min(y)) >= dep_tol
        or abs(x.duration_min - y.duration_min) >= dur_tol
        for x, y in zip(a.legs, b.legs, strict=True)
    )


# ---- probing


def service_date(p: Profile, d: Direction, day: date) -> date:
    return day + timedelta(days=p.journey(d).day_offset)


def _stations(p: Profile, d: Direction) -> list[str]:
    return [p.origin(d), *p.journey(d).transfers, p.destination(d)]


def _usable(p: Profile, d: Direction, base: BaselineVersion | None) -> Signature | None:
    n = len(p.journey(d).transfers) + 1
    return base.signature if base is not None and len(base.signature.legs) == n else None


def _base_departures(
    p: Profile, d: Direction, day: date, base: BaselineVersion | None
) -> list[datetime | None]:
    sd = service_date(p, d, day)
    sig = _usable(p, d, base)
    if sig is None:  # cold start: only leg 1 is known, at the profile's first train
        first = resolve_local(sd, p.journey(d).first_train, edge="start")
        return [first] + [None] * len(p.journey(d).transfers)
    return [
        resolve_local(sd + timedelta(days=x.day_offset), x.departure, edge="start")
        for x in sig.legs
    ]


def dated_end(p: Profile, d: Direction, sd: date, base: BaselineVersion | None) -> datetime | None:
    """When a journey on service date `sd` can last be followed: its last planned arrival plus
    the 150-min limit (7.12 rule 6), in UTC. None without a usable baseline: unknown."""
    sig = _usable(p, d, base)
    if sig is None:
        return None
    x = sig.legs[-1]
    dep = resolve_local(sd + timedelta(days=x.day_offset), x.departure, edge="start")
    return dep.astimezone(UTC) + timedelta(minutes=x.duration_min) + FOLLOW_LIMIT


def _probed_end(r: ProbeResult) -> datetime | None:
    """The affected journey's own last planned arrival plus the 150-min limit (7.12 rule 6):
    works can move it past the baseline's. None when the probe shows no arrival: unknown."""
    arr = None if r.connection is None else r.connection.legs[-1].alight.planned_arrival
    return None if arr is None else arr.astimezone(UTC) + FOLLOW_LIMIT


def _minus_search(t: datetime) -> datetime:
    return (t.astimezone(UTC) - SEARCH_WINDOW).astimezone(TZ)


def probe_queries(p: Profile, d: Direction, day: date, base: BaselineVersion | None) -> list[Query]:
    """One query per leg plus one for the whole journey; a direct journey is its one leg.
    The whole-journey query has no via station, so a changed transfer pattern is visible."""
    st = _stations(p, d)
    deps = _base_departures(p, d, day, base)
    first = deps[0]
    assert first is not None
    whole = Query(st[0], st[-1], _minus_search(first))
    if len(st) == 2:
        return [whole]
    legs = [Query(st[i], st[i + 1], _minus_search(deps[i] or first)) for i in range(len(st) - 1)]
    return [*legs, whole]


def probe_dates(
    p: Profile, today: date, bases: Mapping[Direction, BaselineVersion | None] | None = None
) -> list[date]:
    """The next 3 commute days whose every query date is within 10 days of today. `bases`
    gives leg day offsets (a leg after midnight is queried with its own date)."""
    out: list[date] = []
    for i in range(1, HORIZON_DAYS + 1):
        day = today + timedelta(days=i)
        if not is_commute_day(day, p.days, p.extra_dates, p.skip_public_holidays):
            continue
        latest = max(
            service_date(p, d, day)
            + timedelta(
                days=max((x.day_offset for x in sig.legs), default=0)
                if (sig := _usable(p, d, (bases or {}).get(d))) is not None
                else 0
            )
            for d in Direction
        )
        if (latest - today).days <= HORIZON_DAYS:
            out.append(day)
        if len(out) == PROBE_DAYS:
            break
    return out


def _nearest(conns: Sequence[Connection], ref: datetime) -> Connection | None:
    """Departing within 15 min of `ref`, nearest first, the earlier on a tie."""
    at = ref.astimezone(UTC)
    near = [c for c in conns if abs(planned_departure(c.legs[0]) - at) <= SEARCH_WINDOW]
    return min(
        near,
        key=lambda c: (abs(planned_departure(c.legs[0]) - at), planned_departure(c.legs[0])),
        default=None,
    )


def _kind(leg: Leg, alight: str) -> LegKind:
    # 6.3: a train NS runs as other transport is no train, read as cancelled like `observe`
    return LegKind.CANCELLED if leg.alternative_transport else classify_leg(leg, alight).kind


def _transfer_issue(inc: Leg, out: Leg, alight: str, p: Profile) -> ProbeReason | None:
    """A planned transfer below the 7.12 rule 9 minimum or refused by provider flags. `inc`
    and `out` are current copies (6.2), `alight` the raw outgoing leg's alighting stop."""
    station = out.board.code
    if {_kind(inc, station), _kind(out, alight)} & {LegKind.CANCELLED, LegKind.CUT_SHORT}:
        return None  # the leg itself is the works fact
    t = check_transfer(
        classify_leg(inc, station), classify_leg(out, alight), station, p.transfer_min
    )
    # 7.10 checks the planned transfer: a reported delay must not hide a short change
    arr = next((s.planned_arrival for s in inc.stops[1:] if s.code == station), None)
    dep = out.board.planned_departure
    planned = (
        None
        if arr is None or dep is None
        else int((dep.astimezone(UTC) - arr.astimezone(UTC)).total_seconds() // 60)
    )
    if planned is not None and planned < t.minimum:
        return ProbeReason("short", stop=station, margin=str(planned), minimum=t.minimum)
    if t.state is not TransferState.BROKEN:
        return None
    margin = "?" if t.margin_min is None else str(t.margin_min)
    return ProbeReason("short", stop=station, margin=margin, minimum=t.minimum)


def _leg_reasons(cur: Connection, c: Connection) -> list[ProbeReason]:
    """`cur` is `c` as the current copies show it, judged against `c`'s own alighting stops."""
    out = []
    for i, leg in enumerate(cur.legs):
        k = _kind(leg, c.legs[i].alight.code)
        last = classify_leg(leg, c.legs[i].alight.code).last_served
        if k is LegKind.CUT_SHORT and last is not None:
            # the cut of the current copies, so a moved cut is a new warning (7.10, 7.12)
            out.append(ProbeReason(k.value, leg=i, stop=last.code))
        elif k in (LegKind.CANCELLED, LegKind.CUT_SHORT):
            out.append(ProbeReason(k.value, leg=i))
        elif leg.messages:
            out.append(ProbeReason("messages", leg=i))
    if cur.status == "MAINTENANCE":
        out.append(ProbeReason("maintenance"))
    return out


def _covered(c: Connection, works: PlannedWorks) -> bool:
    start = planned_departure(c.legs[0])
    last = c.legs[-1].alight.planned_arrival
    end = start if last is None else last.astimezone(UTC)
    stops = [s for leg in c.legs for s in leg.stops]
    return any(
        w.start.astimezone(UTC) <= end
        and w.end.astimezone(UTC) > start
        and any(w.lists(s) for s in stops)
        for w in works.items
    )


def _trains(c: Connection) -> tuple[tuple[TrainId, str, str], ...]:
    return tuple((g.train, g.board.code, g.alight.code) for g in c.legs)


def _copies(answers: Sequence[ProviderAnswer], c: Connection) -> list[Connection]:
    """`c` and every connection of the same trains at the newest fetch showing them: in any
    order, a status or a stop one copy shows counts (7.10, 7.12)."""
    same = [
        (a.fetched_at.astimezone(UTC), x)
        for a in answers
        for x in a.connections
        if _trains(x) == _trains(c)
    ]
    top = max((t for t, _ in same), default=None)
    return [c, *(x for t, x in same if t == top)]


def _current(answers: Sequence[ProviderAnswer], c: Connection) -> Connection:
    # every judgment reads the current copy of each train over all probe answers (6.2)
    now = current_connection(answers, c)
    if any(x.status == "MAINTENANCE" for x in _copies(answers, c)):
        return replace(now, status="MAINTENANCE")
    return now


def _snapshot(answers: Sequence[ProviderAnswer], c: Connection | None) -> Connection | None:
    """One snapshot of `c`'s copies for every judgment, fingerprint, baseline and expiry:
    each stop at its worst, in any order (7.10, 7.12)."""
    if c is None:
        return None
    xs = _copies(answers, c)
    merged = (merge_arrivals(g, (x.legs[i] for x in xs)) for i, g in enumerate(c.legs))
    return replace(c, legs=tuple(merged))


def _unclear_of(
    answers: Sequence[ProviderAnswer], c: Connection
) -> tuple[tuple[str, Unclear], ...]:
    # rule 23, on top of the merge: the snapped `c` keeps its trains and boarding stops
    found = (
        (
            train_key(g.train),
            disagreement(newest_copies(answers, g.train, g.board.code), g.alight.code),
        )
        for g in c.legs
    )
    return tuple((tk, u) for tk, u in found if u is not None)


def _select_whole(
    answer: ProviderAnswer,
    origin: str,
    destination: str,
    transfers: tuple[str, ...],
    first: datetime,
    sig: Signature | None,
) -> tuple[Connection | None, Connection | None]:
    """The usual whole journey and, without one, the nearest on another transfer pattern:
    nearest matching-pattern leg 1, then the 6.3 tie-break among those."""
    ends = [
        c
        for c in answer.connections
        if c.is_train_only and c.origin == origin and c.destination == destination
    ]
    near = _nearest([c for c in ends if matches_pattern(c, transfers)], first)
    if near is None:
        return None, _nearest([c for c in ends if not matches_pattern(c, transfers)], first)
    only = replace(answer, connections=tuple(ends))  # the tie-break sees `ends` only
    usual = usual_connection(only, origin, planned_departure(near.legs[0]), transfers, sig)
    return usual or near, None


def _select_leg(answer: ProviderAnswer | None, a: str, b: str, ref: datetime) -> Connection | None:
    """The direct train nearest the leg's baseline departure."""
    direct = (
        []
        if answer is None
        else [
            c
            for c in answer.connections
            if len(c.legs) == 1 and c.is_train_only and (c.origin, c.destination) == (a, b)
        ]
    )
    return _nearest(direct, ref)


def _chain_issues(
    answers: Sequence[ProviderAnswer], legs: Sequence[Connection | None], p: Profile
) -> dict[int, ProbeReason]:
    """The planned transfer from each probed leg to the next one."""
    return {
        i: issue
        for i, (left, right) in enumerate(pairwise(legs))
        if left is not None
        and right is not None
        and (
            issue := _transfer_issue(
                _current(answers, left).legs[0],
                _current(answers, right).legs[0],
                right.legs[0].alight.code,
                p,
            )
        )
        is not None
    }


def _whole_issues(
    answers: Sequence[ProviderAnswer], whole: Connection | None, p: Profile
) -> list[ProbeReason]:
    if whole is None:
        return []
    now = _current(answers, whole)
    found = (
        _transfer_issue(now.legs[i], now.legs[i + 1], whole.legs[i + 1].alight.code, p)
        for i in range(len(whole.legs) - 1)
    )
    return [x for x in found if x is not None]


@dataclass(frozen=True, slots=True)
class _Judging:
    """What every probe result of one commute day and direction is judged on."""

    direction: Direction
    service_date: date
    first_train: time
    answers: Sequence[ProviderAnswer]
    works: PlannedWorks
    transfer_ok: bool

    def _clean(self, c: Connection, reasons: Sequence[ProbeReason]) -> bool:
        return (
            not reasons
            and self.transfer_ok
            and self.works.coverage == "complete"
            and all(
                _kind(x, r.alight.code) is LegKind.OK
                for x, r in zip(_current(self.answers, c).legs, c.legs, strict=True)
            )
            and not any(_covered(x, self.works) for x in _copies(self.answers, c))
        )

    def _on_first_train(self, c: Connection) -> bool:
        """A seed is anchored on the profile's first train."""
        local = planned_departure(c.legs[0]).astimezone(TZ)
        return local.date() == self.service_date and local.time().replace(
            second=0, microsecond=0, fold=0
        ) == self.first_train.replace(tzinfo=None)

    def missing(self, leg_index: int | None) -> ProbeResult:
        return self._result(leg_index, None, _MISSING, [ProbeReason(_MISSING)], False)

    def other_pattern(self, c: Connection) -> ProbeResult:
        head = _PATTERN + signature_text(signature_of(c, self.service_date))
        return self._result(None, c, head, [ProbeReason("pattern")], False)

    def usual(
        self, leg_index: int | None, c: Connection, extra: Sequence[ProbeReason]
    ) -> ProbeResult:
        reasons = [*_leg_reasons(_current(self.answers, c), c), *extra]
        clean = self._clean(c, reasons) and (leg_index is not None or self._on_first_train(c))
        head = signature_text(signature_of(c, self.service_date))
        return self._result(leg_index, c, head, reasons, clean, _unclear_of(self.answers, c))

    def _result(
        self,
        leg_index: int | None,
        c: Connection | None,
        head: str,
        reasons: Sequence[ProbeReason],
        clean: bool,
        unclear: tuple[tuple[str, Unclear], ...] = (),
    ) -> ProbeResult:
        return ProbeResult(
            direction=self.direction,
            service_date=self.service_date,
            leg_index=leg_index,
            connection=c,
            clean=clean,
            transfer_ok=self.transfer_ok,
            head=head,
            reasons=tuple(reasons),
            works_known=self.works.coverage == "complete",
            unclear=unclear,
        )


def evaluate(
    p: Profile,
    d: Direction,
    day: date,
    answers: Sequence[ProviderAnswer],
    works: PlannedWorks,
    base: BaselineVersion | None,
) -> list[ProbeResult]:
    """Probe results for one commute day and direction. A probe without its answer gives no
    result (an empty probe is not an observation, look-ahead 16)."""
    j = p.journey(d)
    sd = service_date(p, d, day)
    st = _stations(p, d)
    n = len(st) - 1
    deps = _base_departures(p, d, day, base)
    by_route = {(a.query.origin, a.query.destination): a for a in answers}
    first = deps[0]
    assert first is not None

    whole_ans = by_route.get((st[0], st[-1]))
    whole, pattern = (
        (None, None)
        if whole_ans is None
        else _select_whole(whole_ans, st[0], st[-1], j.transfers, first, _usable(p, d, base))
    )
    # a leg's departure: the baseline's, else the one the whole journey shows
    refs = [
        deps[i] or (planned_departure(whole.legs[i]) if whole is not None else None)
        for i in range(n if n > 1 else 0)
    ]
    answered = [(st[i], st[i + 1]) in by_route and ref is not None for i, ref in enumerate(refs)]
    legs = [
        None
        if ref is None
        else _snapshot(
            answers, _select_leg(by_route.get((st[i], st[i + 1])), st[i], st[i + 1], ref)
        )
        for i, ref in enumerate(refs)
    ]
    whole, pattern = _snapshot(answers, whole), _snapshot(answers, pattern)

    chain_issues = _chain_issues(answers, legs, p)
    whole_issues = _whole_issues(answers, whole, p)
    judging = _Judging(d, sd, j.first_train, answers, works, not chain_issues and not whole_issues)

    out: list[ProbeResult] = []
    for i, c in enumerate(legs):
        if answered[i]:
            issue = chain_issues.get(i)
            extra = () if issue is None else (issue,)
            out.append(judging.missing(i) if c is None else judging.usual(i, c, extra))
    if whole_ans is not None:
        if whole is not None:
            out.append(judging.usual(None, whole, whole_issues))
        elif pattern is not None:
            out.append(judging.other_pattern(pattern))
        else:
            out.append(judging.missing(None))
    return out


# ---- the run


def _clock_text(t: time) -> str:
    return f"{t:%H:%M}"


def _leg_name(r: ProbeResult, p: Profile) -> str:
    if r.leg_index is None:
        return "journey"
    st = _stations(p, r.direction)
    a, b = st[r.leg_index], st[r.leg_index + 1]
    if r.connection is not None:
        a, b = r.connection.legs[0].board.name, r.connection.legs[0].alight.name
    return f"{a} to {b} leg"


def _station_name(r: ProbeResult, code: str) -> str:
    if r.connection is not None:
        for leg in r.connection.legs:
            for s in leg.stops:
                if s.code == code:
                    return s.name
    return code


def _warn_text(r: ProbeResult, p: Profile, base: Signature | None) -> str:
    what = _leg_name(r, p)
    parts: list[str] = []
    for x in r.reasons:
        tag = x.tag
        if tag == _MISSING:
            parts.append(f"your {what} has no usual train (works)")
        elif tag == "cancelled":
            parts.append(f"your {what} has no trains (works)")
        elif tag == "cut_short":
            stop = _station_name(r, x.stop) if x.stop else "an earlier stop"
            parts.append(f"your {what} runs only to {stop} (works)")
        elif tag == "pattern" and r.connection is not None:
            via = " then ".join(_station_name(r, s) for s in r.connection.transfer_stations)
            parts.append(f"your journey goes {'via ' + via if via else 'direct'} (works)")
        elif tag == "short":
            parts.append(
                f"your change at {_station_name(r, x.stop or '')} is too short"
                f" ({x.margin} min, you need {x.minimum})"
            )
        elif tag in ("messages", "maintenance"):
            parts.append(f"your {what} is affected by planned works")
    if not parts and r.connection is not None and base is not None:
        now = signature_of(r.connection, r.service_date)
        parts.append(
            f"your {what} changes to {_clock_text(now.legs[0].departure)}, {_duration(now)} min"
            f" (was {_clock_text(base.legs[0].departure)}, {_duration(base)} min)"
        )
    return "; ".join(dict.fromkeys(parts))


def _expected(base: BaselineVersion, li: int | None) -> Signature:
    return base.signature if li is None else Signature((base.signature.legs[li],))


def _overlaps(
    p: Profile,
    d: Direction,
    agreed: Signature,
    start: date,
    current: BaselineHistory,
    others: Sequence[Profile],
    bases: Mapping[tuple[str, Direction], BaselineHistory],
) -> bool:
    """Rule 19: every occurrence from `start` on gets the baseline in force on its own service
    date, with `agreed` adopted for `p` and `d` from `start`, so a window opened under an
    older version that runs past an adoption boundary is compared too."""
    own = tuple(v for v in current.versions if v.effective_from < start)
    hs = {**bases, (p.id, d): BaselineHistory((*own, BaselineVersion(0, agreed, start, True)))}

    def first(x: Profile, dx: Direction, commute_day: date) -> time | None:
        h = hs.get((x.id, dx))
        v = None if h is None else h.in_force(commute_day + timedelta(x.journey(dx).day_offset))
        return None if v is None else v.signature.legs[0].departure

    return bool(find_overlaps([p, *others], start, first))


def entry_mark(entry: str) -> str:
    return entry.partition("#")[0]  # no fingerprint holds "#"


def _seed_day(per_date: Mapping[str, tuple[str, ...]], version: int) -> date | None:
    tag = f"seed:{version}"
    return next((split_day_key(k)[0] for k, v in per_date.items() if tag in v), None)


def _owner(entry: str) -> str:
    return entry.partition("#")[2]  # "" for an unowned mark: never a pending fact


def render_message(p: Profile, sd: date, fs: Sequence[LookAheadFact]) -> tuple[EventKind, str]:
    """One look-ahead message for direction and date `sd` from its facts (`_fact_messages`): its kind and text."""
    kind = (
        EventKind.WORKS_AHEAD
        if any(f.warns for f in fs)
        else EventKind.TIMETABLE_ADOPTED
        if any(f.adopts for f in fs)
        else EventKind.RESTORED
    )
    text = (
        f"{display_name(p.name)}, {sd:%A} {sd.day} {sd:%b}: "
        + ". ".join(dict.fromkeys(f.text for f in fs))
        + "."
    )
    if any(f.warning for f in fs):
        text += " " + REPLACEMENT_HINT
    return kind, text


def carried_ids(old: LookAheadLedger, new: LookAheadLedger) -> dict[str, str]:
    """The message ids whose marks a run moved to another message (`_fact_messages` moves the
    marks of a message `retire` withdrew to the message that carries its other facts): old -> new."""
    out = {}
    for k, v in old.per_date.items():
        for a, b in zip(v, new.per_date.get(k, ()), strict=False):
            if _owner(a) and _owner(b) and _owner(a) != _owner(b):
                out[_owner(a)] = _owner(b)
    return out


@dataclass(frozen=True, slots=True)
class _Bundle:
    """The facts of one message about to be written."""

    service_date: date
    facts: Sequence[LookAheadFact]
    was: str | None = None  # what a retire left: the withdrawn obligation that owned its marks
    expires: datetime | None = None  # and its expiry


@dataclass(slots=True)
class _Draft:
    """What a run has written so far: the phases of `run_lookahead` add to it in turn."""

    versions: list[BaselineVersion]
    floor: int  # raised by every removal so a number is never handed out twice
    per_date: dict[str, tuple[str, ...]]
    pending: list[Obligation]
    bundled: dict[str, tuple[LookAheadFact, ...]]
    carried: list[_Bundle] = field(default_factory=list)
    facts: dict[date, list[LookAheadFact]] = field(default_factory=dict)
    owned: dict[date, list[tuple[str, int]]] = field(default_factory=dict)  # marks of its message

    def history(self) -> BaselineHistory:
        return BaselineHistory(tuple(self.versions), self.floor)

    def add_entry(self, key: str, entry: str) -> None:
        self.per_date[key] = (*self.per_date.get(key, ()), entry)

    def own(self, sd: date, key: str, entry: str) -> None:
        self.add_entry(key, entry)
        self.owned.setdefault(sd, []).append((key, len(self.per_date[key]) - 1))

    def tell(self, sd: date, fact: LookAheadFact) -> None:
        self.facts.setdefault(sd, []).append(fact)

    def retire(self, w: BaselineVersion, text: str) -> None:
        """Removes a waiting version: an untold adoption of it is just dropped, a told one gets
        a silent correction on its date (7.12 rule 18). The adoption fact is found by key, also
        inside a message it shares with warnings; those other facts are sent again without it."""
        self.versions.remove(w)
        self.floor = max(self.floor, w.version)
        tag = adopt_key(w.version)
        untold = [o for o in self.pending if bundle_names(o.fact_key, tag)]
        for o in untold:  # never delivered
            self.pending.remove(o)
            rest = [f for f in self.bundled.pop(o.id, ()) if f.key != tag]
            if rest and o.look_ahead is not None:
                self.carried.append(_Bundle(o.look_ahead[2], rest, o.id, o.expires_at))
        if not untold:
            self.tell(
                w.effective_from,
                LookAheadFact(cancel_key(w.version), text, loud=False, warning=False),
            )

    def stamp_owned(self, sd: date, owner: str) -> None:
        for k, i in self.owned.get(sd, ()):
            v = self.per_date[k]
            self.per_date[k] = (*v[:i], f"{v[i]}#{owner}", *v[i + 1 :])

    def move_marks(self, was: str, owner: str) -> None:
        self.per_date = {
            k: tuple(f"{entry_mark(x)}#{owner}" if _owner(x) == was else x for x in v)
            for k, v in self.per_date.items()
        }


def _seed_or_confirm(
    draft: _Draft, results: Sequence[ProbeResult], wholes: Mapping[date, ProbeResult], today: date
) -> None:
    """Cold start: seed, shorter provisional, confirmation (7.10)."""
    if any(v.confirmed for v in draft.versions):
        return
    for sd, r in sorted(wholes.items()):
        # works seen by any leg probe of that date also keep it out (7.10)
        same_day = [x for x in results if x.service_date == sd]
        if not r.clean or r.connection is None or not all(x.clean for x in same_day):
            continue
        sig = signature_of(r.connection, sd)
        prov = max(draft.versions, key=lambda v: v.version, default=None)
        if prov is None or (sig != prov.signature and _duration(sig) < _duration(prov.signature)):
            v = BaselineVersion(draft.history()._next(), sig, today, False)
            draft.versions.append(v)
            draft.add_entry(day_key(sd, None), f"seed:{v.version}")
        elif sig == prov.signature and _seed_day(draft.per_date, prov.version) != sd:
            draft.versions[draft.versions.index(prov)] = replace(prov, confirmed=True)
            break


def _agreed_signature(
    results: Sequence[ProbeResult],
    wholes: Mapping[date, ProbeResult],
    expected: Sequence[date],
    probes_per_day: int,
) -> tuple[Signature | None, str | None]:
    """Unanimity over every requested date (look-ahead 19, 20): the signature they agree on, and
    the refusal when they agree only on another transfer pattern."""
    per_day = {sd: [r for r in results if r.service_date == sd] for sd in expected}
    complete = bool(expected) and all(
        sd in wholes and len(per_day[sd]) == probes_per_day for sd in expected
    )
    if not complete:
        return None, None
    rs = [r for sd in expected for r in per_day[sd]]
    sigs = {
        signature_of(c, wholes[sd].service_date)
        for sd in expected
        if (c := wholes[sd].connection) is not None
    }
    agreed = None
    if all(not r.works and r.transfer_ok for r in rs) and len(sigs) == 1:
        (agreed,) = sigs
    if len({wholes[sd].head for sd in expected}) == 1 and wholes[expected[0]].other_pattern:
        return (
            agreed,
            "NS now offers your usual journey only with other changes; update your profile",
        )
    return agreed, None


def _cancel_waiting(
    draft: _Draft, agreed: Signature | None, in_force_today: BaselineVersion | None, today: date
) -> None:
    """A waiting version is cancelled when the run agrees with the version in force."""
    if agreed is None or in_force_today is None or agreed != in_force_today.signature:
        return
    for w in [v for v in draft.versions if v.effective_from > today]:
        draft.retire(w, "your usual timetable stays as it was; ignore the earlier change")


def _adopt(
    draft: _Draft,
    p: Profile,
    d: Direction,
    agreed: Signature | None,
    expected: Sequence[date],
    today: date,
    others: Sequence[Profile],
    bases: Mapping[tuple[str, Direction], BaselineHistory],
) -> tuple[BaselineVersion | None, str | None]:
    """Unanimous adoption of the whole signature, after confirmation only (Q2): the version
    adopted, or the refusal that blocked it."""
    if agreed is None or not any(v.confirmed for v in draft.versions):
        return None, None
    current = draft.history()
    if all((b := current.in_force(sd)) is not None and b.signature == agreed for sd in expected):
        return None, None
    old = current.in_force(expected[0]) or current.in_force(today)
    old_first = old.signature.legs[0].departure if old is not None else p.journey(d).first_train
    new_first = agreed.legs[0].departure
    if agreed.legs[0].day_offset != 0:
        return None, (
            f"NS moved your {_clock_text(old_first)} to {_clock_text(new_first)} on another day;"
            " update your profile"
        )
    if _overlaps(p, d, agreed, expected[0], current, others, bases):
        return None, (
            f"NS moved your {_clock_text(old_first)} to {_clock_text(new_first)}, which overlaps"
            " another window; update your profile"
        )
    adopted = BaselineVersion(current._next(), agreed, expected[0], True)
    for w in [v for v in draft.versions if v.effective_from > today]:
        if w.effective_from >= adopted.effective_from:  # it would override this one
            draft.retire(w, "ignore the earlier change; I follow the newer NS time")
    draft.versions.append(adopted)
    tag = adopt_key(adopted.version)
    draft.add_entry(day_key(expected[0], None), tag)
    when = f"{expected[0]:%A} {expected[0].day} {expected[0]:%b}"
    draft.tell(
        expected[0],
        LookAheadFact(
            tag,
            f"NS moved your {_clock_text(old_first)} to {_clock_text(new_first)} from {when};"
            " I follow the new time. Update your profile?",
            loud=False,
            warning=False,
        ),
    )
    return adopted, None


def _probe_facts(
    draft: _Draft,
    p: Profile,
    d: Direction,
    results: Sequence[ProbeResult],
    final: BaselineHistory,
    adopted: BaselineVersion | None,
    today: date,
) -> set[date]:
    """Per probe: warnings, already told, restored (look-ahead 23-27). Returns the dates with a
    fact whose affected journey has no known end."""
    unsure: set[date] = set()
    for r in results:
        sd = r.service_date
        key = day_key(sd, r.leg_index)
        base = final.in_force(sd)
        conf = base if base is not None and base.confirmed and _usable(p, d, base) else None
        exp = _expected(conf, r.leg_index) if conf is not None else None
        changed = (
            exp is not None
            and r.connection is not None
            and not r.other_pattern
            and _differs(signature_of(r.connection, sd), exp, DEP_TOL_MIN, DUR_TOL_MIN)
        )
        entries = tuple(entry_mark(x) for x in draft.per_date.get(key, ()))
        at = next(
            (
                i
                for i in reversed(range(len(entries)))
                if entries[i].startswith(("warn:", "restored:"))
            ),
            None,
        )
        last = None if at is None else entries[at]
        since = () if at is None else entries[at + 1 :]
        if r.works or changed:
            loud = sd == today + timedelta(days=1)
            fact = f"{key}|warn:{r.fingerprint}"
            if last == f"warn:{r.fingerprint}":  # only the latest entry counts
                if not loud or f"loud:{r.fingerprint}" in since:
                    continue  # already told at this severity
                fact += "|loud"  # rule 4: told silent, now tomorrow: once more, loud
            else:
                draft.own(sd, key, f"warn:{r.fingerprint}")
            if loud:
                draft.own(sd, key, f"loud:{r.fingerprint}")
            draft.tell(sd, LookAheadFact(fact, _warn_text(r, p, exp), loud, r.works))
            if _probed_end(r) is None:
                unsure.add(sd)
        elif last is not None and last.startswith("warn:"):
            draft.own(sd, key, f"restored:{r.fingerprint}")
            if adopted is not None and last == f"warn:{r.fingerprint}":
                continue  # the warned change is what this run adopted: told by the adoption
            what = _leg_name(r, p)
            draft.tell(
                sd,
                LookAheadFact(
                    f"{key}|restored", f"your {what} is back to normal", loud=False, warning=False
                ),
            )
            if _probed_end(r) is None:
                unsure.add(sd)
    return unsure


@dataclass(frozen=True, slots=True)
class _Writer:
    """Builds the stored messages of one finished judgment."""

    p: Profile
    d: Direction
    revision: int
    now: datetime
    final: BaselineHistory
    results: Sequence[ProbeResult]
    unsure: set[date]

    def event(
        self, sd: date | None, kind: EventKind, severity: Severity, text: str, tail: str
    ) -> AlertEvent:
        return AlertEvent(
            profile_id=self.p.id,
            profile_name=self.p.name,
            direction=self.d,
            service_date=sd,
            leg_index=None,
            kind=kind,
            severity=severity,
            train=None,
            text=text,
            key=f"lookahead|{self.p.id}|{self.d.value}|{tail}",
            created_at=self.now,
        )

    def dated_event(
        self, sd: date, kind: EventKind, severity: Severity, text: str, fact_key: str
    ) -> AlertEvent:
        return self.event(sd, kind, severity, text, f"{sd.isoformat()}|{fact_key}")

    def obligation(
        self,
        event: AlertEvent,
        day: date,
        version: int | None,
        fact_key: str,
        expires: datetime | None,
    ) -> Obligation:
        return Obligation(
            id=f"{event.key}|{self.now.isoformat()}",
            event=event,
            occurrence=None,
            look_ahead=(self.p.id, self.d, day),
            route_revision=self.revision,
            baseline_version=version,
            fact_key=fact_key,
            expires_at=expires,
            attempts=0,
        )

    def in_force_version(self, sd: date) -> int | None:
        b = self.final.in_force(sd)
        return b.version if b else None

    def journey_end(self, sd: date) -> datetime | None:
        # 7.12 rule 5(b): the latest of the baseline's end and every journey probed for `sd`
        base_end = dated_end(self.p, self.d, sd, self.final.in_force(sd))
        if base_end is None or sd in self.unsure:
            return None
        ends = [_probed_end(r) for r in self.results if r.service_date == sd]
        return max([base_end, *(e for e in ends if e is not None)])


def _fact_messages(draft: _Draft, w: _Writer, adopted: BaselineVersion | None) -> list[AlertEvent]:
    """One message per direction and date, no cap, with what a retire left after them."""
    events: list[AlertEvent] = []
    for b in [*(_Bundle(sd, fs) for sd, fs in sorted(draft.facts.items())), *draft.carried]:
        sd = b.service_date
        is_adopt = any(f.adopts for f in b.facts)
        kind, text = render_message(w.p, sd, b.facts)
        fact_key = bundle_key(b.facts)
        severity = Severity.LOUD if any(f.loud for f in b.facts) else Severity.SILENT
        event = w.dated_event(sd, kind, severity, text, fact_key)
        version = adopted.version if is_adopt and adopted is not None else w.in_force_version(sd)
        # 7.12 rule 5(b): expires when the journey on that date ended, which can be after
        # midnight (a 23:40 journey with a 00:05 transfer), never at the end of its date: the
        # latest of the baseline's end and every affected journey probed for that date, as
        # works can move the last arrival later. An unknown end, an adoption or its withdrawal
        # gets no expiry: it is kept after its date. Carried facts keep their message's bound
        expires = b.expires
        if b.was is None and not is_adopt and not any(f.cancels for f in b.facts):
            expires = w.journey_end(sd)
        ob = w.obligation(event, sd, version, fact_key, expires)
        events.append(event)
        draft.pending.append(ob)
        draft.bundled[ob.id] = tuple(b.facts)
        if b.was is None:
            draft.stamp_owned(sd, ob.id)
        else:  # the carried facts' marks move to the message that now carries them
            draft.move_marks(b.was, ob.id)
    return events


def _unclear_messages(draft: _Draft, w: _Writer) -> list[AlertEvent]:
    """Rule 23: copies of one train in one answer that disagree, once per train, date and
    values; its own loud message, gated only by its ledger mark (owner decision 7)."""
    events: list[AlertEvent] = []
    told: set[tuple[date, str]] = set()
    for r in w.results:
        sd = r.service_date
        for tk, u in r.unclear:
            if (sd, tk) in told:
                continue
            told.add((sd, tk))
            key = unclear_day_key(sd, tk)
            mark = f"unclear:{u.fingerprint}"
            marks = [entry_mark(x) for x in draft.per_date.get(key, ()) if x.startswith("unclear:")]
            if marks and marks[-1] == mark:
                continue
            fact = f"{key}|{mark}"
            text = f"{display_name(w.p.name)}, {sd:%A} {sd.day} {sd:%b}: " + data_unclear_text(
                u.label, u.station, u.copies
            )
            event = w.dated_event(sd, EventKind.DATA_UNCLEAR, Severity.LOUD, text, fact)
            expires = w.journey_end(sd) if _probed_end(r) is not None else None
            ob = w.obligation(event, sd, w.in_force_version(sd), fact, expires)
            draft.pending.append(ob)
            events.append(event)
            draft.bundled[ob.id] = (LookAheadFact(fact, text, loud=True, warning=False),)
            draft.add_entry(key, f"{mark}#{ob.id}")
    return events


def _refusal_messages(
    draft: _Draft, w: _Writer, refusals: Sequence[str], today: date
) -> list[AlertEvent]:
    """A blocked adoption is told while it lasts."""
    events: list[AlertEvent] = []
    for reason in refusals:
        text = f"{display_name(w.p.name)}: profile change refused: {reason}."
        event = w.event(None, EventKind.PROFILE_REFUSED, Severity.LOUD, text, f"refused|{reason}")
        events.append(event)
        draft.pending.append(w.obligation(event, today, w.final.latest, event.key, None))
    return events


def run_lookahead(
    p: Profile,
    d: Direction,
    history: BaselineHistory,
    ledger: LookAheadLedger,
    results: Sequence[ProbeResult],
    *,
    revision: int,
    today: date,
    now: datetime,
    others: Sequence[Profile] = (),
    bases: Mapping[tuple[str, Direction], BaselineHistory] | None = None,
) -> tuple[BaselineHistory, LookAheadLedger, list[AlertEvent]] | None:
    """The direction's serialised look-ahead writer. Every fact is written to the ledger and
    stored as a pending message before any delivery. `others` are the other profiles and
    `bases` the current baseline histories of every other (profile id, direction), this
    profile's other direction included, for the dated overlap check of an adoption (7.12
    rule 19); `others` is only compared through `bases`, so it needs them.

    Caller contract (rule 21): `ledger` is the ledger as stored at write time with
    `started_revision` / `started_baseline` set to the revision and `history.latest` seen when
    this run started; `history` and `revision` are the current ones; `now` is the run's start
    time. A newer run that finished first stamped a later `last_run`, so this one is dropped.
    `ledger.pending` holds only undelivered obligations: delivery removes each one it finishes."""
    assert bases is not None or not others
    if ledger.started_revision != revision or ledger.started_baseline != history.latest:
        return None  # the route or baseline changed since the run started (rule 21)
    stamps = [t.astimezone(UTC) for t in (ledger.last_applied, ledger.last_run) if t is not None]
    if stamps and max(stamps) > now.astimezone(UTC):
        return None  # an older run overlapping a newer one, also a newer failed-health one
    results = sorted(
        (r for r in results if r.direction is d),
        key=lambda r: (r.service_date, r.leg_index is None, r.leg_index or 0),
    )
    wholes = {r.service_date: r for r in results if r.leg_index is None}
    draft = _Draft(
        list(history.versions),
        history.floor,
        dict(ledger.per_date),
        list(ledger.pending),
        dict(ledger.facts),
    )

    _seed_or_confirm(draft, results, wholes, today)
    in_force_today = draft.history().in_force(today)
    expected = [service_date(p, d, day) for day in probe_dates(p, today, {d: in_force_today})]
    n = len(p.journey(d).transfers) + 1
    agreed, pattern_refusal = _agreed_signature(results, wholes, expected, n + 1 if n > 1 else 1)
    _cancel_waiting(draft, agreed, in_force_today, today)
    adopted, adopt_refusal = _adopt(draft, p, d, agreed, expected, today, others, bases or {})
    final = draft.history()
    unsure = _probe_facts(draft, p, d, results, final, adopted, today)

    w = _Writer(p, d, revision, now, final, results, unsure)
    events = [
        *_fact_messages(draft, w, adopted),
        *_unclear_messages(draft, w),
        *_refusal_messages(draft, w, [x for x in (pattern_refusal, adopt_refusal) if x], today),
    ]
    success = bool(results) and all(r.works_known for r in results)
    new_ledger = LookAheadLedger(
        per_date=draft.per_date,
        pending=tuple(draft.pending),
        last_run=now if success else ledger.last_run,
        started_revision=revision,
        started_baseline=final.latest,
        last_applied=now,
        facts={o.id: draft.bundled[o.id] for o in draft.pending if o.id in draft.bundled},
    )
    return final, new_ledger, events


# ---- edits


def rebase_on_edit(
    old: Profile, new: Profile, d: Direction, history: BaselineHistory, today: date
) -> BaselineHistory:
    """Rule 10: a change of stations, transfers, first train or day offset re-seeds the
    direction's baseline, except setting `first_train` to the baseline in force (Q2)."""
    a, b = old.journey(d), new.journey(d)
    same_route = (old.origin(d), old.destination(d), a.transfers, a.day_offset) == (
        new.origin(d),
        new.destination(d),
        b.transfers,
        b.day_offset,
    )
    if same_route and a.first_train == b.first_train:
        return history
    base = history.in_force(today)
    if same_route and base is not None and base.signature.legs[0].departure == b.first_train:
        return history
    return BaselineHistory((), max(history.latest or 0, history.floor))


def reset_on_edit(
    old: Profile,
    new: Profile,
    d: Direction,
    history: BaselineHistory,
    ledger: LookAheadLedger,
    today: date,
) -> LookAheadLedger:
    """Rule 10: a route edit that re-seeds the direction also forgets its per-date facts, so a
    warning about the new route is told again instead of matching one about the old route
    (whose pending messages rule 5 drops). Call it with the same arguments as rebase_on_edit."""
    if rebase_on_edit(old, new, d, history, today) is history:
        return ledger
    return replace(ledger, per_date={})


def forget_pending(ledger: LookAheadLedger) -> LookAheadLedger:
    """Rule 5(c): a route edit drops the pending messages written before it. Their per-date
    marks are taken back so the next run tells those facts again (7.10), instead of matching a
    fact that was never delivered. Only marks owned by a dropped message go; a mark of a
    delivered one stays."""
    drop = {o.id for o in ledger.pending}
    per_date = {k: tuple(x for x in v if _owner(x) not in drop) for k, v in ledger.per_date.items()}
    return replace(ledger, per_date=per_date)
