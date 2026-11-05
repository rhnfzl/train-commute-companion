"""Look-ahead probing: the stored keys, signatures, what to ask NS and how one day's answers read
(7.10, 7.12 rules 18, 19, 21). Pure."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Final, Literal

from train_commute_companion.calendar import (
    MINUTES_PER_DAY,
    TZ,
    is_commute_day,
    minute_of_day,
    resolve_local,
)
from train_commute_companion.classify import classify_leg, current_connection, merge_arrivals
from train_commute_companion.match import matches_pattern, planned_departure, usual_connection
from train_commute_companion.model import (
    FOLLOW_LIMIT,
    Connection,
    Direction,
    Leg,
    LegKind,
    Profile,
    Signature,
    SignatureLeg,
    TrainId,
    TransferState,
    present,
)
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.state import BaselineVersion, LookAheadFact
from train_commute_companion.transfer import check_transfer
from train_commute_companion.unclear import Unclear, disagreement, newest_copies, train_key

PROBE_DAYS = 3
HORIZON_DAYS = 10
SEARCH_WINDOW = timedelta(minutes=15)
# change tolerances of look-ahead 23; a unanimous run adopts any difference (7.12 rule 18)
DEP_TOL_MIN = 5
DUR_TOL_MIN = 10
REPLACEMENT_HINT = "Check replacement transport nearer the day."
MISSING_REASON: Final = "missing"
_PATTERN = "pattern:"


ProbeTag = Literal["missing", "pattern", "cancelled", "cut_short", "messages", "maintenance", "short"]


@dataclass(frozen=True, slots=True)
class ProbeReason:
    """Why a probe counts as works. `token` is how it reads in a stored fingerprint."""

    tag: ProbeTag
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


# ---- the stored key grammars, each built and read here


WHOLE_JOURNEY = "j"  # a day key's leg part for the journey probe
WORKS_MARKS = ("warn:", "restored:")  # the marks that say whether a date has works
UNCLEAR_MARK = "unclear:"


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


def seed_mark(version: int) -> str:
    """The entry on the date that seeded a baseline version."""
    return f"seed:{version}"


def owned_mark(mark: str, owner: str) -> str:
    """A per-date mark with the id of the obligation that carries it."""
    return f"{mark}#{owner}"


def entry_mark(entry: str) -> str:
    return entry.partition("#")[0]  # no fingerprint holds "#"


def entry_owner(entry: str) -> str:
    return entry.partition("#")[2]  # "" for an unowned mark: never a pending fact


def latest_mark(
    entries: Iterable[str], prefixes: tuple[str, ...]
) -> tuple[str | None, tuple[str, ...]]:
    """The latest of a date's marks with one of `prefixes`, and the marks after it."""
    marks = [entry_mark(x) for x in entries]
    at = next((i for i in reversed(range(len(marks))) if marks[i].startswith(prefixes)), None)
    return (None, ()) if at is None else (marks[at], tuple(marks[at + 1 :]))


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
    return x.day_offset * MINUTES_PER_DAY + minute_of_day(x.departure)


def signature_minutes(s: Signature) -> int:
    return _abs_min(s.legs[-1]) + s.legs[-1].duration_min - _abs_min(s.legs[0])


def signature_differs(a: Signature, b: Signature, dep_tol: int, dur_tol: int) -> bool:
    if len(a.legs) != len(b.legs) or abs(signature_minutes(a) - signature_minutes(b)) >= dur_tol:
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


def journey_stations(p: Profile, d: Direction) -> list[str]:
    return [p.origin(d), *p.journey(d).transfers, p.destination(d)]


def baseline_usable(p: Profile, d: Direction, base: BaselineVersion | None) -> Signature | None:
    n = len(p.journey(d).transfers) + 1
    return base.signature if base is not None and len(base.signature.legs) == n else None


def _base_departures(
    p: Profile, d: Direction, day: date, base: BaselineVersion | None
) -> list[datetime | None]:
    sd = service_date(p, d, day)
    sig = baseline_usable(p, d, base)
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
    sig = baseline_usable(p, d, base)
    if sig is None:
        return None
    x = sig.legs[-1]
    dep = resolve_local(sd + timedelta(days=x.day_offset), x.departure, edge="start")
    return dep.astimezone(UTC) + timedelta(minutes=x.duration_min) + FOLLOW_LIMIT


def probed_end(r: ProbeResult) -> datetime | None:
    """The affected journey's own last planned arrival plus the 150-min limit (7.12 rule 6):
    works can move it past the baseline's. None when the probe shows no arrival: unknown."""
    arr = None if r.connection is None else r.connection.legs[-1].alight.planned_arrival
    return None if arr is None else arr.astimezone(UTC) + FOLLOW_LIMIT


def _minus_search(t: datetime) -> datetime:
    return (t.astimezone(UTC) - SEARCH_WINDOW).astimezone(TZ)


def probe_queries(p: Profile, d: Direction, day: date, base: BaselineVersion | None) -> list[Query]:
    """One query per leg plus one for the whole journey; a direct journey is its one leg.
    The whole-journey query has no via station, so a changed transfer pattern is visible."""
    st = journey_stations(p, d)
    deps = _base_departures(p, d, day, base)
    first = present(deps[0])  # a journey's first departure is always known
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
                if (sig := baseline_usable(p, d, (bases or {}).get(d))) is not None
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
            out.append(ProbeReason("cut_short", leg=i, stop=last.code))
        elif k is LegKind.CUT_SHORT:
            out.append(ProbeReason("cut_short", leg=i))
        elif k is LegKind.CANCELLED:
            out.append(ProbeReason("cancelled", leg=i))
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
        return self._result(leg_index, None, MISSING_REASON, [ProbeReason(MISSING_REASON)], False)

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
    st = journey_stations(p, d)
    n = len(st) - 1
    deps = _base_departures(p, d, day, base)
    by_route = {(a.query.origin, a.query.destination): a for a in answers}
    first = present(deps[0])  # a journey's first departure is always known

    whole_ans = by_route.get((st[0], st[-1]))
    whole, pattern = (
        (None, None)
        if whole_ans is None
        else _select_whole(
            whole_ans, st[0], st[-1], j.transfers, first, baseline_usable(p, d, base)
        )
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
    if whole_ans is None:
        return out
    if whole is not None:
        return [*out, judging.usual(None, whole, whole_issues)]
    return [*out, judging.other_pattern(pattern) if pattern is not None else judging.missing(None)]
