"""Planned-works look-ahead and timetable baselines (7.10, 7.12 rules 18, 19, 21). Pure."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta

from train_commute_companion.lookahead_probe import (
    DEP_TOL_MIN,
    DUR_TOL_MIN,
    LOUD_FACT,
    MISSING_REASON,
    REPLACEMENT_HINT,
    RESTORED_FACT,
    UNCLEAR_MARK,
    WORKS_MARKS,
    ProbeReason,
    ProbeResult,
    adopt_key,
    baseline_usable,
    bundle_key,
    bundle_names,
    cancel_key,
    dated_end,
    day_key,
    entry_mark,
    entry_owner,
    fact_warns,
    is_warning,
    journey_stations,
    latest_mark,
    loud_mark,
    owned_mark,
    probe_dates,
    probed_end,
    restored_mark,
    seed_mark,
    service_date,
    signature_differs,
    signature_minutes,
    signature_of,
    split_day_key,
    unclear_day_key,
    warn_mark,
)
from train_commute_companion.messages import data_unclear_text
from train_commute_companion.model import (
    AlertEvent,
    Direction,
    EventKind,
    Obligation,
    Profile,
    Severity,
    Signature,
    display_name,
)
from train_commute_companion.overlap import find_overlaps
from train_commute_companion.state import (
    BaselineHistory,
    BaselineVersion,
    LookAheadFact,
    LookAheadLedger,
)

# ---- the run


def _clock_text(t: time) -> str:
    return f"{t:%H:%M}"


def _leg_name(r: ProbeResult, p: Profile) -> str:
    if r.leg_index is None:
        return "journey"
    st = journey_stations(p, r.direction)
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


def _reason_text(r: ProbeResult, x: ProbeReason, what: str) -> str | None:
    tag = x.tag
    if tag == MISSING_REASON:
        return f"your {what} has no usual train (works)"
    if tag == "cancelled":
        return f"your {what} has no trains (works)"
    if tag == "cut_short":
        stop = _station_name(r, x.stop) if x.stop else "an earlier stop"
        return f"your {what} runs only to {stop} (works)"
    if tag == "pattern" and r.connection is not None:
        via = " then ".join(_station_name(r, s) for s in r.connection.transfer_stations)
        return f"your journey goes {'via ' + via if via else 'direct'} (works)"
    if tag == "short":
        return (
            f"your change at {_station_name(r, x.stop or '')} is too short"
            f" ({x.margin} min, you need {x.minimum})"
        )
    if tag in ("messages", "maintenance"):
        return f"your {what} is affected by planned works"
    return None


def _warn_text(r: ProbeResult, p: Profile, base: Signature | None) -> str:
    what = _leg_name(r, p)
    parts = [t for x in r.reasons if (t := _reason_text(r, x, what))]
    if not parts and r.connection is not None and base is not None:
        now = signature_of(r.connection, r.service_date)
        parts.append(
            f"your {what} changes to {_clock_text(now.legs[0].departure)}, {signature_minutes(now)} min"
            f" (was {_clock_text(base.legs[0].departure)}, {signature_minutes(base)} min)"
        )
    return "; ".join(dict.fromkeys(parts))


def _expected(base: BaselineVersion, li: int | None) -> Signature:
    return base.signature if li is None else Signature((base.signature.legs[li],))


def overlaps_next(
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


def _seed_day(per_date: Mapping[str, tuple[str, ...]], version: int) -> date | None:
    tag = seed_mark(version)
    return next((split_day_key(k)[0] for k, v in per_date.items() if tag in v), None)


def render_message(p: Profile, sd: date, fs: Sequence[LookAheadFact]) -> tuple[EventKind, str]:
    """One look-ahead message for direction and date `sd` from its facts (`_fact_messages`): its kind and text."""
    kind = (
        EventKind.WORKS_AHEAD
        if any(fact_warns(f) for f in fs)
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
            if entry_owner(a) and entry_owner(b) and entry_owner(a) != entry_owner(b):
                out[entry_owner(a)] = entry_owner(b)
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

    def add_owned_entry(self, sd: date, key: str, entry: str) -> None:
        self.add_entry(key, entry)
        self.owned.setdefault(sd, []).append((key, len(self.per_date[key]) - 1))

    def add_fact(self, sd: date, fact: LookAheadFact) -> None:
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
            self.add_fact(
                w.effective_from,
                LookAheadFact(cancel_key(w.version), text, loud=False, warning=False),
            )

    def stamp_owned(self, sd: date, owner: str) -> None:
        for k, i in self.owned.get(sd, ()):
            v = self.per_date[k]
            self.per_date[k] = (*v[:i], owned_mark(v[i], owner), *v[i + 1 :])

    def move_marks(self, was: str, owner: str) -> None:
        self.per_date = {
            k: tuple(owned_mark(entry_mark(x), owner) if entry_owner(x) == was else x for x in v)
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
        if prov is None or (
            sig != prov.signature and signature_minutes(sig) < signature_minutes(prov.signature)
        ):
            v = BaselineVersion(draft.history()._next(), sig, today, False)
            draft.versions.append(v)
            draft.add_entry(day_key(sd, None), seed_mark(v.version))
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
    """Unanimous adoption of the whole signature, after confirmation only: the version
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
    if overlaps_next(p, d, agreed, expected[0], current, others, bases):
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
    draft.add_fact(
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


def _changed(r: ProbeResult, exp: Signature | None) -> bool:
    return (
        exp is not None
        and r.connection is not None
        and not r.other_pattern
        and signature_differs(
            signature_of(r.connection, r.service_date), exp, DEP_TOL_MIN, DUR_TOL_MIN
        )
    )


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
        conf = base if base is not None and base.confirmed and baseline_usable(p, d, base) else None
        exp = _expected(conf, r.leg_index) if conf is not None else None
        last, since = latest_mark(draft.per_date.get(key, ()), WORKS_MARKS)
        if r.works or _changed(r, exp):
            loud = sd == today + timedelta(days=1)
            warned = warn_mark(r.fingerprint)
            fact = f"{key}|{warned}"
            if last == warned:  # only the latest entry counts
                if not loud or loud_mark(r.fingerprint) in since:
                    continue  # already told at this severity
                fact += LOUD_FACT  # rule 4: told silent, now tomorrow: once more, loud
            else:
                draft.add_owned_entry(sd, key, warned)
            if loud:
                draft.add_owned_entry(sd, key, loud_mark(r.fingerprint))
            draft.add_fact(sd, LookAheadFact(fact, _warn_text(r, p, exp), loud, r.works))
            if probed_end(r) is None:
                unsure.add(sd)
        elif is_warning(last):
            draft.add_owned_entry(sd, key, restored_mark(r.fingerprint))
            if adopted is not None and last == warn_mark(r.fingerprint):
                continue  # the warned change is what this run adopted: told by the adoption
            what = _leg_name(r, p)
            draft.add_fact(
                sd,
                LookAheadFact(
                    f"{key}|{RESTORED_FACT}",
                    f"your {what} is back to normal",
                    loud=False,
                    warning=False,
                ),
            )
            if probed_end(r) is None:
                unsure.add(sd)
    return unsure


@dataclass(frozen=True, slots=True)
class _MessageBuilder:
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
        ends = [probed_end(r) for r in self.results if r.service_date == sd]
        return max([base_end, *(e for e in ends if e is not None)])


def _fact_messages(
    draft: _Draft, w: _MessageBuilder, adopted: BaselineVersion | None
) -> list[AlertEvent]:
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


def _unclear_messages(draft: _Draft, w: _MessageBuilder) -> list[AlertEvent]:
    """Rule 23: copies of one train in one answer that disagree, once per train, date and
    values; its own loud message, gated only by its ledger mark."""
    events: list[AlertEvent] = []
    told: set[tuple[date, str]] = set()
    for r in w.results:
        sd = r.service_date
        for tk, u in r.unclear:
            if (sd, tk) in told:
                continue
            told.add((sd, tk))
            key = unclear_day_key(sd, tk)
            mark = f"{UNCLEAR_MARK}{u.fingerprint}"
            if latest_mark(draft.per_date.get(key, ()), (UNCLEAR_MARK,))[0] == mark:
                continue
            fact = f"{key}|{mark}"
            text = f"{display_name(w.p.name)}, {sd:%A} {sd.day} {sd:%b}: " + data_unclear_text(
                u.label, u.station, u.copies
            )
            event = w.dated_event(sd, EventKind.DATA_UNCLEAR, Severity.LOUD, text, fact)
            expires = w.journey_end(sd) if probed_end(r) is not None else None
            ob = w.obligation(event, sd, w.in_force_version(sd), fact, expires)
            draft.pending.append(ob)
            events.append(event)
            draft.bundled[ob.id] = (LookAheadFact(fact, text, loud=True, warning=False),)
            draft.add_entry(key, owned_mark(mark, ob.id))
    return events


def _refusal_messages(
    draft: _Draft, w: _MessageBuilder, refusals: Sequence[str], today: date
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
    others: Sequence[Profile],
    bases: Mapping[tuple[str, Direction], BaselineHistory],
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
    adopted, adopt_refusal = _adopt(draft, p, d, agreed, expected, today, others, bases)
    final = draft.history()
    unsure = _probe_facts(draft, p, d, results, final, adopted, today)

    w = _MessageBuilder(p, d, revision, now, final, results, unsure)
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
    direction's baseline, except setting `first_train` to the baseline in force."""
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
    per_date = {k: tuple(x for x in v if entry_owner(x) not in drop) for k, v in ledger.per_date.items()}
    return replace(ledger, per_date=per_date)
