"""One look-ahead run of a direction (7.10, rule 21): probed through the shared budget, then applied
to the state as stored by then, with the owed webhook copies following its messages (rules 5, 18)."""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from train_commute_companion.calendar import TZ, to_utc
from train_commute_companion.lookahead import carried_ids, render_message, run_lookahead
from train_commute_companion.lookahead_probe import (
    ProbePlan,
    ProbeResult,
    adopt_key,
    bundle_key,
    evaluate,
    probe_dates,
    probe_queries,
    service_date,
    travels_on,
)
from train_commute_companion.model import Direction, Obligation, Profile, direction_key, present
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.revisions import baseline_on, commute_day_for, travel_replaces
from train_commute_companion.state import (
    BaselineHistory,
    LookAheadFact,
    LookAheadLedger,
    PollFailure,
)
from train_commute_companion.store import PersistedState
from train_commute_companion.timetable_receipts import (
    adoption_correction,
    merge_adoption_receipts,
)
from train_commute_companion.webhook import event_id
from train_commute_companion.webhook_queue import CopyQueue

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProbedRun:
    """A direction's observations, with the start stamps its write is judged on (rule 21)."""

    start: datetime
    revision: int
    baseline: int | None
    results: tuple[ProbeResult, ...]
    probe_finished: bool = True
    travel_revision: int = 0
    evening_day: date | None = None


def skipped_dates(st: PersistedState, p: Profile, d: Direction) -> set[date]:
    return {
        s.key.service_date
        for s in st.occurrences.values()
        if s.key.profile_id == p.id and s.key.direction is d and s.not_travelling
    }


async def probe_direction(
    st: PersistedState,
    p: Profile,
    d: Direction,
    works: PlannedWorks,
    start: datetime,
    fetch: Callable[[Sequence[Query]], Awaitable[tuple[ProviderAnswer, ...] | PollFailure]],
    *,
    day: date | None = None,
    evening: bool = False,
    snapshot: Sequence[ProviderAnswer] = (),
) -> ProbedRun | None:
    """Keep dated observations before a failed custom probe. The writer checks agreement."""
    config = present(st.config)
    today = start.astimezone(TZ).date()
    if evening and day != today + timedelta(days=1):
        return None
    hist = st.baselines.get(direction_key(p.id, d), BaselineHistory())
    histories = {x: st.baselines.get(direction_key(p.id, x), BaselineHistory()) for x in Direction}
    results: list[ProbeResult] = []
    skipped = skipped_dates(st, p, d)

    def replaced(sd: date) -> bool:
        return travel_replaces(st, p, d, commute_day_for(p, d, sd))

    plan = (
        probe_dates(p, today, direction=d, histories=histories, skipped=skipped, replaced=replaced)
        if day is None
        else ProbePlan((day,) if travels_on(p, d, day, skipped, replaced=replaced) else (), True)
    )
    for target in plan.dates:
        if p.journey_on(d, target).mode == "window":
            continue  # no usual train to probe, seed or adopt on a window day
        base = baseline_on(hist, p, d, service_date(p, d, target))
        queries = probe_queries(p, d, target, base)
        if day is not None and not evening:
            queries = queries[-1:]
        got = tuple(snapshot) or await fetch(queries)
        if isinstance(got, str):
            if evening:
                return ProbedRun(
                    start,
                    config.revision,
                    hist.latest,
                    (),
                    False,
                    travel_revision=st.travel_revision,
                    evening_day=day,
                )
            if not p.alerts.custom_timetable_horizon or not results:
                return None
            return ProbedRun(
                start,
                config.revision,
                hist.latest,
                tuple(results),
                False,
                travel_revision=st.travel_revision,
                evening_day=day if evening else None,
            )
        results += evaluate(p, d, target, got, works, base, dated=day is not None)
    return ProbedRun(
        start,
        config.revision,
        hist.latest,
        tuple(results),
        plan.complete and (not evening or works.coverage == "complete"),
        travel_revision=st.travel_revision,
        evening_day=day if evening else None,
    )


def apply_run(
    st: PersistedState,
    queue: CopyQueue,
    p: Profile,
    d: Direction,
    run: ProbedRun,
    *,
    track_adoptions: bool = False,
    pending_ledger: LookAheadLedger | None = None,
) -> PersistedState | None:
    """`st` (the state now, at write time) with the run, its owed copies in `queue` following its
    messages; None when it is discarded (rule 21), and then those copies are left as they were."""
    cfg = st.config
    if cfg is None or st.travel_revision != run.travel_revision:
        return None
    skipped = skipped_dates(st, p, d)

    def replaced(sd: date) -> bool:
        return travel_replaces(st, p, d, commute_day_for(p, d, sd))

    if run.evening_day is not None and not travels_on(
        p, d, run.evening_day, skipped, replaced=replaced
    ):
        return None
    dk = direction_key(p.id, d)
    # write time: the ledger as stored now with this run's start stamps (rule 21)
    stored = st.lookahead.get(dk) or LookAheadLedger(
        per_date={}, pending=(), last_run=None, started_revision=0, started_baseline=None
    )
    ledger = replace(stored, started_revision=run.revision, started_baseline=run.baseline)
    current = st.baselines.get(dk, BaselineHistory())
    if pending_ledger is not None:
        ledger = replace(ledger, adoption_receipts=merge_adoption_receipts(ledger, pending_ledger))
    all_bases = {
        (x.id, y): st.baselines.get(direction_key(x.id, y), BaselineHistory())
        for x in cfg.profiles
        for y in Direction
        if (x.id, y) != (p.id, d)
    }
    # A queued adoption or an already-attempted copy can still report a late acceptance.
    started = (*stored.pending, *(c.obligation for c in queue.copies.values() if c.first_try))
    awaiting = {
        correction
        for o in started
        if o.look_ahead is not None
        and o.look_ahead[:2] == (p.id, d)
        and (correction := adoption_correction(o)) is not None
        and ledger.adoption_ids.get(correction) in (None, o.id)
    }
    out = run_lookahead(
        p,
        d,
        current,
        ledger,
        run.results,
        revision=cfg.revision,
        today=run.start.astimezone(TZ).date(),
        now=run.start,
        others=[x for x in cfg.profiles if x.id != p.id],
        bases=all_bases,
        skipped=skipped,
        probe_finished=run.probe_finished,
        replaced=replaced,
        awaiting_adoption=awaiting,
        track_adoptions=track_adoptions,
        evening_day=run.evening_day,
    )
    if out is None:
        log.info("look-ahead for %s discarded: configuration or baseline changed", dk)
        return None
    new_hist, new_ledger, _ = out
    new_ledger = _copies_through(queue, p, d, stored, current, new_hist, new_ledger)
    mine = {o.id for o in stored.pending}
    return replace(
        st,
        baselines={**st.baselines, dk: new_hist} if run.evening_day is None else st.baselines,
        lookahead={**st.lookahead, dk: new_ledger},
        obligations=(*(o for o in st.obligations if o.id not in mine), *new_ledger.pending),
    )


def directions_due(
    st: PersistedState, since: datetime | None, *, profile_id: str | None = None
) -> list[tuple[str, Direction]]:
    """Directions that need a daily attempt. An explicit run passes no start time."""
    due = []
    for p in st.config.profiles if st.config else ():
        if p.home_station == p.work_station or profile_id is not None and p.id != profile_id:
            continue
        for d in Direction:
            ledger = st.lookahead.get(direction_key(p.id, d))
            stamp = ledger.last_applied if ledger is not None else None
            if since is None or stamp is None or to_utc(stamp) < to_utc(since):
                due.append((p.id, d))
    return due


def lookahead_done(st: PersistedState, since: datetime) -> bool:
    return not directions_due(st, since)


def _carry_copies(
    queue: CopyQueue,
    was: Mapping[str, Obligation],
    stored: LookAheadLedger,
    new_ledger: LookAheadLedger,
) -> dict[str, Obligation]:
    """The new ledger's pending messages, each carried one marked as copied when its copy
    goes on under the new message or has already ended."""
    pending = {o.id: o for o in new_ledger.pending}
    for old, new in carried_ids(stored, new_ledger).items():
        if old not in was or new not in pending:
            continue
        prior = was[old]
        c = next((c for c in queue.copies.values() if c.obligation.id == old), None)
        pending[new] = n = replace(
            pending[new],
            target_ids=prior.target_ids,
            primary_target_pending=prior.primary_target_pending,
            primary_suppressed=prior.primary_suppressed,
            webhook_copied=c is not None or prior.webhook_copied or old in queue.copied,
            alert_history_origin=prior.alert_history_origin
            or (event_id(prior.id), prior.event.created_at),
        )
        if c is not None:  # the same copy: its webhook-id, receipts and tries stay
            queue.rewrite(c, n)
    return pending


def _rewrite_withdrawn(
    queue: CopyQueue,
    p: Profile,
    d: Direction,
    was: Mapping[str, Obligation],
    stored: LookAheadLedger,
    current: BaselineHistory,
    new_hist: BaselineHistory,
) -> dict[str, tuple[LookAheadFact, ...]]:
    """Owed copies whose bundled adoption this run withdrew keep their other facts."""
    rewritten: dict[str, tuple[LookAheadFact, ...]] = {}
    gone = {v.version for v in current.versions} - {v.version for v in new_hist.versions}
    for c in list(queue.copies.values()):
        o = c.obligation
        if o.look_ahead is None or o.look_ahead[:2] != (p.id, d) or o.id in was:
            continue
        if o.baseline_version is None or o.baseline_version not in gone:
            continue
        fs = stored.facts.get(o.id, ())
        rest = tuple(f for f in fs if f.key != adopt_key(o.baseline_version))
        if not rest or len(rest) == len(fs):
            continue  # only the adoption (dropped at its next try), or none bundled
        day = o.look_ahead[2]
        kind, text = render_message(p, day, rest)
        b = baseline_on(new_hist, p, d, day)
        fact_key = bundle_key(rest)
        o = replace(
            o,
            baseline_version=b.version if b else None,
            fact_key=fact_key,
            event=replace(
                o.event,
                kind=kind,
                text=text,
                key=o.event.key.removesuffix(o.fact_key) + fact_key,
            ),
        )
        queue.rewrite(c, o)
        rewritten[o.id] = rest
    return rewritten


def _copies_through(
    queue: CopyQueue,
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
    pending = _carry_copies(queue, was, stored, new_ledger)
    rewritten = _rewrite_withdrawn(queue, p, d, was, stored, current, new_hist)
    owed = {c.obligation.id for c in queue.copies.values()}
    carry = {i: f for i, f in stored.facts.items() if i in owed}
    facts = {**carry, **rewritten, **new_ledger.facts}
    return replace(new_ledger, pending=tuple(pending.values()), facts=facts)
