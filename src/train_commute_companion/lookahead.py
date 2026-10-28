"""Planned-works look-ahead and timetable baselines (7.10, 7.12 rules 18, 19, 21, Q2). Pure."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta

from train_commute_companion.calendar import TZ, is_commute_day, resolve_local
from train_commute_companion.classify import classify_leg, current_connection
from train_commute_companion.delivery import Obligation
from train_commute_companion.lifecycle import FOLLOW_LIMIT
from train_commute_companion.match import matches_pattern, usual_connection
from train_commute_companion.model import (
    AlertEvent,
    Connection,
    Direction,
    EventKind,
    Leg,
    LegKind,
    Profile,
    Severity,
    Signature,
    SignatureLeg,
    TrainId,
    TransferState,
)
from train_commute_companion.profile import find_overlaps
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.transfer import check_transfer

PROBE_DAYS = 3
HORIZON_DAYS = 10
SEARCH = timedelta(minutes=15)
# change tolerances of look-ahead 23; a unanimous run adopts any difference (7.12 rule 18)
DEP_TOL_MIN = 5
DUR_TOL_MIN = 10
REPLACEMENT_HINT = "Check replacement transport nearer the day."


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
class ProbeResult:
    direction: Direction
    service_date: date
    leg_index: int | None  # None: the whole journey
    connection: Connection | None
    works: bool
    clean: bool
    transfer_ok: bool
    fingerprint: str
    works_known: bool = True  # False when planned_works() coverage was unknown (look-ahead 36)


@dataclass(frozen=True, slots=True)
class LookAheadLedger:
    # "<service date>|<leg index or j>" -> entries; a warn/loud/restored mark written since
    # owners exist ends in "#<id of the obligation that carries it>", read with _mark/_owner
    per_date: Mapping[str, tuple[str, ...]]
    pending: tuple[Obligation, ...]
    last_run: datetime | None  # last successful run (look-ahead health only)
    started_revision: int
    started_baseline: int | None
    last_applied: datetime | None = None  # start time of the last run whose writes were applied
    # pending obligation id -> its facts (key, text, loud, is warning), so one fact can be
    # withdrawn from a message that bundles several (7.12 rule 18)
    facts: Mapping[str, tuple[tuple[str, str, bool, bool], ...]] = field(default_factory=dict)


# ---- signatures


def _clock_min(t: time) -> int:
    return t.hour * 60 + t.minute


def signature_of(c: Connection, service_date: date) -> Signature:
    legs = []
    for leg in c.legs:
        dep = (leg.board.planned_departure or leg.train.planned_departure).astimezone(UTC)
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


def encode(s: Signature) -> str:
    return ";".join(
        f"{x.board}>{x.alight}@{x.departure:%H:%M}+{x.day_offset}/{x.duration_min}" for x in s.legs
    )


def _abs_min(x: SignatureLeg) -> int:
    return x.day_offset * 1440 + _clock_min(x.departure)


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
    return (t.astimezone(UTC) - SEARCH).astimezone(TZ)


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


def _dep(leg: Leg) -> datetime:
    return (leg.board.planned_departure or leg.train.planned_departure).astimezone(UTC)


def _nearest(conns: Sequence[Connection], ref: datetime) -> Connection | None:
    """Departing within 15 min of `ref`, nearest first, the earlier on a tie."""
    at = ref.astimezone(UTC)
    near = [c for c in conns if abs(_dep(c.legs[0]) - at) <= SEARCH]
    return min(near, key=lambda c: (abs(_dep(c.legs[0]) - at), _dep(c.legs[0])), default=None)


def _kind(leg: Leg, alight: str) -> LegKind:
    # 6.3: a train NS runs as other transport is no train, read as cancelled like `_observe`
    return LegKind.CANCELLED if leg.alternative_transport else classify_leg(leg, alight).kind


def _transfer_issue(inc: Leg, out: Leg, alight: str, p: Profile) -> str | None:
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
        return f"short:{station}:{planned}:{t.minimum}"
    if t.state is not TransferState.BROKEN:
        return None
    return f"short:{station}:{t.margin_min if t.margin_min is not None else '?'}:{t.minimum}"


def _leg_reasons(cur: Connection, c: Connection) -> list[str]:
    """`cur` is `c` as the current copies show it, judged against `c`'s own alighting stops."""
    out = []
    for i, leg in enumerate(cur.legs):
        k = _kind(leg, c.legs[i].alight.code)
        last = classify_leg(leg, c.legs[i].alight.code).last_served
        if k is LegKind.CUT_SHORT and last is not None:
            # the cut of the current copies, so a moved cut is a new warning (7.10, 7.12)
            out.append(f"{k.value}:{i}:{last.code}")
        elif k in (LegKind.CANCELLED, LegKind.CUT_SHORT):
            out.append(f"{k.value}:{i}")
        elif leg.messages:
            out.append(f"messages:{i}")
    if cur.status == "MAINTENANCE":
        out.append("maintenance")
    return out


def _covered(c: Connection, works: PlannedWorks) -> bool:
    start = _dep(c.legs[0])
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


def _fingerprint(head: str, reasons: Sequence[str]) -> str:
    return head + ("|" + ",".join(reasons) if reasons else "")


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
    sig = _usable(p, d, base)
    known = works.coverage == "complete"
    by_route = {(a.query.origin, a.query.destination): a for a in answers}

    # whole journey: nearest matching-pattern leg 1, then the 6.3 tie-break among those
    whole: Connection | None = None
    pattern: Connection | None = None
    whole_ans = by_route.get((st[0], st[-1]))
    first = deps[0]
    assert first is not None
    if whole_ans is not None:
        ends = [
            c
            for c in whole_ans.connections
            if c.is_train_only and c.origin == st[0] and c.destination == st[-1]
        ]
        near = _nearest([c for c in ends if matches_pattern(c, j.transfers)], first)
        if near is not None:
            only = replace(whole_ans, connections=tuple(ends))  # the tie-break sees `ends` only
            whole = usual_connection(only, st[0], _dep(near.legs[0]), j.transfers, sig) or near
        else:
            pattern = _nearest([c for c in ends if not matches_pattern(c, j.transfers)], first)

    # legs: the direct train nearest the leg's baseline departure
    legs: list[Connection | None] = []
    answered: list[bool] = []
    if n > 1:
        for i in range(n):
            a = by_route.get((st[i], st[i + 1]))
            ref = deps[i] or (_dep(whole.legs[i]) if whole is not None else None)
            answered.append(a is not None and ref is not None)
            direct = (
                []
                if a is None
                else [
                    c
                    for c in a.connections
                    if len(c.legs) == 1
                    and c.is_train_only
                    and (c.origin, c.destination) == (st[i], st[i + 1])
                ]
            )
            legs.append(None if ref is None else _nearest(direct, ref))

    def copies(c: Connection) -> list[Connection]:
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

    def cur(c: Connection) -> Connection:
        # every judgment reads the current copy of each train over all probe answers (6.2)
        now = current_connection(answers, c)
        if any(x.status == "MAINTENANCE" for x in copies(c)):
            return replace(now, status="MAINTENANCE")
        return now

    chain_issues = {
        i: issue
        for i in range(n - 1)
        if (left := legs[i]) is not None
        and (right := legs[i + 1]) is not None
        and (
            issue := _transfer_issue(
                cur(left).legs[0], cur(right).legs[0], right.legs[0].alight.code, p
            )
        )
        is not None
    }
    whole_issues: list[str] = []
    if whole is not None:
        now = cur(whole)
        for i in range(len(whole.legs) - 1):
            x = _transfer_issue(now.legs[i], now.legs[i + 1], whole.legs[i + 1].alight.code, p)
            if x is not None:
                whole_issues.append(x)
    transfer_ok = not chain_issues and not whole_issues

    def result(li: int | None, c: Connection | None, reasons: list[str], head: str) -> ProbeResult:
        is_works = bool(reasons)
        clean = (
            c is not None
            and not is_works
            and transfer_ok
            and known
            and all(
                _kind(x, r.alight.code) is LegKind.OK
                for x, r in zip(cur(c).legs, c.legs, strict=True)
            )
            and not any(_covered(x, works) for x in copies(c))
        )
        if li is None and c is not None:  # a seed is anchored on the profile's first train
            local = _dep(c.legs[0]).astimezone(TZ)
            clean = (
                clean
                and local.date() == sd
                and local.time().replace(second=0, microsecond=0, fold=0)
                == j.first_train.replace(tzinfo=None)
            )
        return ProbeResult(
            d, sd, li, c, is_works, clean, transfer_ok, _fingerprint(head, reasons), known
        )

    out: list[ProbeResult] = []
    for i in range(n if n > 1 else 0):
        if not answered[i]:
            continue
        c = legs[i]
        if c is None:
            out.append(result(i, None, ["missing"], "missing"))
            continue
        reasons = _leg_reasons(cur(c), c)
        if i in chain_issues:
            reasons.append(chain_issues[i])
        out.append(result(i, c, reasons, encode(signature_of(c, sd))))
    if whole_ans is not None:
        if whole is not None:
            reasons = _leg_reasons(cur(whole), whole) + whole_issues
            out.append(result(None, whole, reasons, encode(signature_of(whole, sd))))
        elif pattern is not None:
            head = "pattern:" + encode(signature_of(pattern, sd))
            out.append(result(None, pattern, ["pattern"], head))
        else:
            out.append(result(None, None, ["missing"], "missing"))
    return out


# ---- the run


def _title(p: Profile) -> str:
    return p.name.replace("_", " ").title()


def _hhmm(t: time) -> str:
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
    reasons = r.fingerprint.split("|", 1)[1].split(",") if "|" in r.fingerprint else []
    parts: list[str] = []
    for x in reasons:
        tag, _, rest = x.partition(":")
        if tag == "missing":
            parts.append(f"your {what} has no usual train (works)")
        elif tag == "cancelled":
            parts.append(f"your {what} has no trains (works)")
        elif tag == "cut_short":
            code = rest.partition(":")[2]
            stop = _station_name(r, code) if code else "an earlier stop"
            parts.append(f"your {what} runs only to {stop} (works)")
        elif tag == "pattern" and r.connection is not None:
            via = " then ".join(_station_name(r, s) for s in r.connection.transfer_stations)
            parts.append(f"your journey goes {'via ' + via if via else 'direct'} (works)")
        elif tag == "short":
            station, margin, minimum = rest.split(":")
            parts.append(
                f"your change at {_station_name(r, station)} is too short"
                f" ({margin} min, you need {minimum})"
            )
        elif tag in ("messages", "maintenance"):
            parts.append(f"your {what} is affected by planned works")
    if not parts and r.connection is not None and base is not None:
        now = signature_of(r.connection, r.service_date)
        parts.append(
            f"your {what} changes to {_hhmm(now.legs[0].departure)}, {_duration(now)} min"
            f" (was {_hhmm(base.legs[0].departure)}, {_duration(base)} min)"
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


def _mark(entry: str) -> str:
    return entry.partition("#")[0]  # no fingerprint holds "#"


def _seed_day(per_date: Mapping[str, tuple[str, ...]], version: int) -> str | None:
    tag = f"seed:{version}"
    return next((k.split("|")[0] for k, v in per_date.items() if tag in v), None)


def _owner(entry: str) -> str:
    return entry.partition("#")[2]  # "" for an entry stored before owners: counts as delivered


def render_message(
    p: Profile, sd: date, fs: Sequence[tuple[str, str, bool, bool]]
) -> tuple[EventKind, str]:
    """One look-ahead message for direction and date `sd` from its facts (step 6 of
    `run_lookahead`): its kind and text."""
    warns = any("|warn:" in k for k, *_ in fs)
    is_adopt = any(k.startswith("adopt:") for k, *_ in fs)
    kind = (
        EventKind.WORKS_AHEAD
        if warns
        else EventKind.TIMETABLE_ADOPTED
        if is_adopt
        else EventKind.RESTORED
    )
    text = (
        f"{_title(p)}, {sd:%A} {sd.day} {sd:%b}: "
        + ". ".join(dict.fromkeys(t for _, t, _, _ in fs))
        + "."
    )
    if any(w for *_, w in fs):
        text += " " + REPLACEMENT_HINT
    return kind, text


def carried_ids(old: LookAheadLedger, new: LookAheadLedger) -> dict[str, str]:
    """The message ids whose marks a run moved to another message (step 6 moves the marks of
    a message `retire` withdrew to the message that carries its other facts): old -> new."""
    out = {}
    for k, v in old.per_date.items():
        for a, b in zip(v, new.per_date.get(k, ()), strict=False):
            if _owner(a) and _owner(b) and _owner(a) != _owner(b):
                out[_owner(a)] = _owner(b)
    return out


def run_lookahead(
    p: Profile,
    d: Direction,
    history: BaselineHistory,
    ledger: LookAheadLedger,
    results: Sequence[ProbeResult],
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
    rule 19).

    Caller contract (rule 21): `ledger` is the ledger as stored at write time with
    `started_revision` / `started_baseline` set to the revision and `history.latest` seen when
    this run started; `history` and `revision` are the current ones; `now` is the run's start
    time. A newer run that finished first stamped a later `last_run`, so this one is dropped.
    `ledger.pending` holds only undelivered obligations: delivery removes each one it finishes."""
    if ledger.started_revision != revision or ledger.started_baseline != history.latest:
        return None  # the route or baseline changed since the run started (rule 21)
    stamps = [t.astimezone(UTC) for t in (ledger.last_applied, ledger.last_run) if t is not None]
    if stamps and max(stamps) > now.astimezone(UTC):
        return None  # an older run overlapping a newer one, also a newer failed-health one
    j = p.journey(d)
    n = len(j.transfers) + 1
    results = sorted(
        (r for r in results if r.direction is d),
        key=lambda r: (r.service_date, r.leg_index is None, r.leg_index or 0),
    )
    wholes = {r.service_date: r for r in results if r.leg_index is None}
    per_date = dict(ledger.per_date)
    pending = list(ledger.pending)
    bundled = dict(ledger.facts)
    # what a retire left, with the withdrawn obligation that owned its marks and its expiry
    carried: list[tuple[date, list[tuple[str, str, bool, bool]], str | None, datetime | None]] = []
    versions = list(history.versions)
    floor = history.floor  # raised by every removal so a number is never handed out twice
    facts: dict[date, list[tuple[str, str, bool, bool]]] = {}  # key, text, loud, is warning
    refusals: list[str] = []
    adopted: BaselineVersion | None = None
    owned: dict[date, list[tuple[str, int]]] = {}  # this run's marks, owned by its message

    def own(sd: date, key: str, entry: str) -> None:
        add_entry(key, entry)
        owned.setdefault(sd, []).append((key, len(per_date[key]) - 1))

    def hist() -> BaselineHistory:
        return BaselineHistory(tuple(versions), floor)

    def add_entry(key: str, entry: str) -> None:
        per_date[key] = (*per_date.get(key, ()), entry)

    def retire(w: BaselineVersion, text: str) -> None:
        """Removes a waiting version: an untold adoption of it is just dropped, a told one gets
        a silent correction on its date (7.12 rule 18). The adoption fact is found by key, also
        inside a message it shares with warnings; those other facts are sent again without it."""
        nonlocal floor, pending
        versions.remove(w)
        floor = max(floor, w.version)
        tag = f"adopt:{w.version}"
        untold = [o for o in pending if tag in o.fact_key.split(";")]
        for o in untold:  # never delivered
            pending.remove(o)
            rest = [f for f in bundled.pop(o.id, ()) if f[0] != tag]
            if rest and o.look_ahead is not None:
                carried.append((o.look_ahead[2], rest, o.id, o.expires_at))
        if not untold:
            facts.setdefault(w.effective_from, []).append(
                (f"cancel:{w.version}", text, False, False)
            )

    # 1. cold start: seed, shorter provisional, confirmation (7.10)
    if not any(v.confirmed for v in versions):
        for sd, r in sorted(wholes.items()):
            # works seen by any leg probe of that date also keep it out (7.10)
            same_day = [x for x in results if x.service_date == sd]
            if not r.clean or r.connection is None or not all(x.clean for x in same_day):
                continue
            sig = signature_of(r.connection, sd)
            prov = max(versions, key=lambda v: v.version, default=None)
            if prov is None or (
                sig != prov.signature and _duration(sig) < _duration(prov.signature)
            ):
                v = BaselineVersion(hist()._next(), sig, today, False)
                versions.append(v)
                add_entry(f"{sd.isoformat()}|j", f"seed:{v.version}")
            elif sig == prov.signature and _seed_day(per_date, prov.version) != sd.isoformat():
                versions[versions.index(prov)] = replace(prov, confirmed=True)
                break

    # 2. unanimity over every requested date (look-ahead 19, 20)
    in_force_today = hist().in_force(today)
    expected = [service_date(p, d, day) for day in probe_dates(p, today, {d: in_force_today})]
    per_day = {sd: [r for r in results if r.service_date == sd] for sd in expected}
    complete = bool(expected) and all(
        sd in wholes and len(per_day[sd]) == (n + 1 if n > 1 else 1) for sd in expected
    )
    agreed: Signature | None = None
    if complete:
        rs = [r for sd in expected for r in per_day[sd]]
        sigs = {
            signature_of(c, wholes[sd].service_date)
            for sd in expected
            if (c := wholes[sd].connection) is not None
        }
        if all(not r.works and r.transfer_ok for r in rs) and len(sigs) == 1:
            (agreed,) = sigs
        pats = {wholes[sd].fingerprint.split("|")[0] for sd in expected}
        if len(pats) == 1 and next(iter(pats)).startswith("pattern:"):
            refusals.append(
                "NS now offers your usual journey only with other changes; update your profile"
            )

    # 3. a waiting version is cancelled when the run agrees with the version in force
    if agreed is not None and in_force_today is not None and agreed == in_force_today.signature:
        for w in [v for v in versions if v.effective_from > today]:
            retire(w, "your usual timetable stays as it was; ignore the earlier change")

    # 4. unanimous adoption of the whole signature, after confirmation only (Q2)
    if agreed is not None and any(v.confirmed for v in versions):
        current = hist()
        if any((b := current.in_force(sd)) is None or b.signature != agreed for sd in expected):
            old = current.in_force(expected[0]) or current.in_force(today)
            old_first = old.signature.legs[0].departure if old is not None else j.first_train
            new_first = agreed.legs[0].departure
            if agreed.legs[0].day_offset != 0:
                refusals.append(
                    f"NS moved your {_hhmm(old_first)} to {_hhmm(new_first)} on another day;"
                    " update your profile"
                )
            elif _overlaps(p, d, agreed, expected[0], current, others, bases or {}):
                refusals.append(
                    f"NS moved your {_hhmm(old_first)} to {_hhmm(new_first)}, which overlaps"
                    " another window; update your profile"
                )
            else:
                adopted = BaselineVersion(current._next(), agreed, expected[0], True)
                for w in [v for v in versions if v.effective_from > today]:
                    if w.effective_from >= adopted.effective_from:  # it would override this one
                        retire(w, "ignore the earlier change; I follow the newer NS time")
                versions.append(adopted)
                add_entry(f"{expected[0].isoformat()}|j", f"adopt:{adopted.version}")
                when = f"{expected[0]:%A} {expected[0].day} {expected[0]:%b}"
                facts.setdefault(expected[0], []).append(
                    (
                        f"adopt:{adopted.version}",
                        (
                            f"NS moved your {_hhmm(old_first)} to {_hhmm(new_first)} from {when};"
                            " I follow the new time. Update your profile?"
                        ),
                        False,
                        False,
                    )
                )

    # 5. per probe: warnings, already told, restored (look-ahead 23-27)
    final = hist()
    unsure: set[date] = set()  # a date with a fact whose affected journey has no known end
    for r in results:
        sd = r.service_date
        key = f"{sd.isoformat()}|{'j' if r.leg_index is None else r.leg_index}"
        base = final.in_force(sd)
        conf = base if base is not None and base.confirmed and _usable(p, d, base) else None
        exp = _expected(conf, r.leg_index) if conf is not None else None
        changed = (
            exp is not None
            and r.connection is not None
            and not r.fingerprint.startswith("pattern:")
            and _differs(signature_of(r.connection, sd), exp, DEP_TOL_MIN, DUR_TOL_MIN)
        )
        entries = tuple(_mark(x) for x in per_date.get(key, ()))
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
                own(sd, key, f"warn:{r.fingerprint}")
            if loud:
                own(sd, key, f"loud:{r.fingerprint}")
            facts.setdefault(sd, []).append((fact, _warn_text(r, p, exp), loud, r.works))
            if _probed_end(r) is None:
                unsure.add(sd)
        elif last is not None and last.startswith("warn:"):
            own(sd, key, f"restored:{r.fingerprint}")
            if adopted is not None and last == f"warn:{r.fingerprint}":
                continue  # the warned change is what this run adopted: told by the adoption
            what = _leg_name(r, p)
            facts.setdefault(sd, []).append(
                (f"{key}|restored", f"your {what} is back to normal", False, False)
            )
            if _probed_end(r) is None:
                unsure.add(sd)

    # 6. one message per direction and date, no cap
    events: list[AlertEvent] = []
    for sd, fs, was, kept in [
        *((sd, fs, None, None) for sd, fs in sorted(facts.items())),
        *carried,
    ]:
        is_adopt = any(k.startswith("adopt:") for k, *_ in fs)
        kind, text = render_message(p, sd, fs)
        fact_key = ";".join(k for k, *_ in fs)
        ev = AlertEvent(
            p.id,
            p.name,
            d,
            sd,
            None,
            kind,
            Severity.LOUD if any(loud for _, _, loud, _ in fs) else Severity.SILENT,
            None,
            text,
            f"lookahead|{p.id}|{d.value}|{sd.isoformat()}|{fact_key}",
            now,
        )
        b = final.in_force(sd)
        version = (
            adopted.version if is_adopt and adopted is not None else (b.version if b else None)
        )
        events.append(ev)
        # 7.12 rule 5(b): expires when the journey on that date ended, which can be after
        # midnight (a 23:40 journey with a 00:05 transfer), never at the end of its date: the
        # latest of the baseline's end and every affected journey probed for that date, as
        # works can move the last arrival later. An unknown end, an adoption or its withdrawal
        # gets no expiry: it is kept after its date. Carried facts keep their message's bound
        expires = kept
        if was is None and not is_adopt and not any(k.startswith("cancel:") for k, *_ in fs):
            ends = [_probed_end(r) for r in results if r.service_date == sd]
            base_end = dated_end(p, d, sd, b)
            if base_end is not None and sd not in unsure:
                expires = max([base_end, *(e for e in ends if e is not None)])
        ob = Obligation(
            f"{ev.key}|{now.isoformat()}", ev, None, (p.id, d, sd), revision, version, fact_key,
            expires, 0,
        )  # fmt: skip
        pending.append(ob)
        bundled[ob.id] = tuple(fs)
        if was is None:
            for k, i in owned.get(sd, ()):
                per_date[k] = (*per_date[k][:i], f"{per_date[k][i]}#{ob.id}", *per_date[k][i + 1 :])
        else:  # the carried facts' marks move to the message that now carries them
            per_date = {
                k: tuple(f"{_mark(x)}#{ob.id}" if _owner(x) == was else x for x in v)
                for k, v in per_date.items()
            }
    for reason in refusals:  # a blocked adoption is told while it lasts
        ev = AlertEvent(
            p.id,
            p.name,
            d,
            None,
            None,
            EventKind.PROFILE_REFUSED,
            Severity.LOUD,
            None,
            f"{_title(p)}: profile change refused: {reason}.",
            f"lookahead|{p.id}|{d.value}|refused|{reason}",
            now,
        )
        events.append(ev)
        pending.append(
            Obligation(
                f"{ev.key}|{now.isoformat()}",
                ev,
                None,
                (p.id, d, today),
                revision,
                final.latest,
                ev.key,
                None,
                0,
            )
        )

    success = bool(results) and all(r.works_known for r in results)
    new_ledger = LookAheadLedger(
        per_date,
        tuple(pending),
        now if success else ledger.last_run,
        revision,
        final.latest,
        now,
        {o.id: bundled[o.id] for o in pending if o.id in bundled},
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
    delivered one stays. A message stored before owners owns no mark, so its facts take back
    their own latest unowned mark instead: a missing owner does not prove delivery."""
    drop = {o.id for o in ledger.pending}
    owners = {_owner(x) for v in ledger.per_date.values() for x in v}
    per_date = {k: tuple(x for x in v if _owner(x) not in drop) for k, v in ledger.per_date.items()}
    # shortcut: equal unowned marks are told apart by position only; delete once no store
    # written before owners can be loaded
    for o in ledger.pending:
        if o.id in owners:
            continue
        for fact, *_ in ledger.facts.get(o.id, ()):
            # "<date>|<leg>|warn:<fp>[|loud]" or "<date>|<leg>|restored"; a fingerprint may hold "|"
            parts = fact.split("|", 2)
            if len(parts) < 3 or not parts[2].startswith(("warn:", "restored")):
                continue  # an adoption or a correction: kept by the baseline, not per date
            key, what = "|".join(parts[:2]), parts[2]
            entries = per_date.get(key, ())
            # only the loud repeat was pending, or the warning; a restored fact has no fingerprint
            want = f"loud:{what[5:-5]}" if what.endswith("|loud") else what
            at = [
                i
                for i, x in enumerate(entries)
                if not _owner(x) and (x == want or want == "restored" and x.startswith("restored:"))
            ]
            if at:
                i = at[-1]
                twin = want.startswith("warn:") and entries[i + 1 : i + 2] == (f"loud:{want[5:]}",)
                per_date[key] = entries[:i] + entries[i + 2 if twin else i + 1 :]
    return replace(ledger, per_date=per_date)
