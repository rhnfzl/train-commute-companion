"""One look-ahead run of a direction (7.10, rule 21): probed through the shared budget, then applied
to the state as stored by then, with the owed webhook copies following its messages (rules 5, 18)."""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from train_commute_companion.calendar import TZ, to_utc
from train_commute_companion.lookahead import carried_ids, render_message, run_lookahead
from train_commute_companion.lookahead_probe import (
    ProbeResult,
    adopt_key,
    bundle_key,
    evaluate,
    probe_dates,
    probe_queries,
    service_date,
)
from train_commute_companion.model import Direction, Obligation, Profile, direction_key, present
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.state import (
    BaselineHistory,
    LookAheadFact,
    LookAheadLedger,
    PollFailure,
)
from train_commute_companion.store import PersistedState
from train_commute_companion.webhook_queue import CopyQueue

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProbedRun:
    """A complete probe of a direction, with the start stamps its write is judged on (rule 21)."""

    start: datetime
    revision: int
    baseline: int | None
    results: tuple[ProbeResult, ...]


async def probe_direction(
    st: PersistedState,
    p: Profile,
    d: Direction,
    works: PlannedWorks,
    start: datetime,
    fetch_all: Callable[
        [Sequence[Query], bool, datetime | None],
        Awaitable[tuple[ProviderAnswer, ...] | PollFailure],
    ],
    auth: Callable[[PollFailure | None, str, datetime], None],
) -> ProbedRun | None:
    """`st`: the state as the run starts. None: an incomplete run, which writes nothing."""
    config = present(st.config)
    today = start.astimezone(TZ).date()
    hist = st.baselines.get(direction_key(p.id, d), BaselineHistory())
    bases = {
        x: st.baselines.get(direction_key(p.id, x), BaselineHistory()).in_force(today)
        for x in Direction
    }
    results: list[ProbeResult] = []
    for day in probe_dates(p, today, bases):
        base = hist.in_force(service_date(p, d, day))
        got = await fetch_all(probe_queries(p, d, day, base), False, None)
        if isinstance(got, str):
            auth(got, "lookahead", start)
            return None
        auth(None, "lookahead", start)
        results += evaluate(p, d, day, got, works, base)
    return ProbedRun(start, config.revision, hist.latest, tuple(results))


def applied_run(
    st: PersistedState, queue: CopyQueue, p: Profile, d: Direction, run: ProbedRun
) -> PersistedState | None:
    """`st` (the state now, at write time) with the run; None when it is discarded (rule 21), and
    then the owed copies in `queue` are left as they were."""
    cfg = st.config
    if cfg is None:
        return None
    dk = direction_key(p.id, d)
    # write time: the ledger as stored now with this run's start stamps (rule 21)
    stored = st.lookahead.get(dk) or LookAheadLedger({}, (), None, 0, None)
    ledger = replace(stored, started_revision=run.revision, started_baseline=run.baseline)
    current = st.baselines.get(dk, BaselineHistory())
    all_bases = {
        (x.id, y): st.baselines.get(direction_key(x.id, y), BaselineHistory())
        for x in cfg.profiles
        for y in Direction
        if (x.id, y) != (p.id, d)
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
    )
    if out is None:
        log.info("look-ahead for %s discarded: configuration or baseline changed", dk)
        return None
    new_hist, new_ledger, _ = out
    new_ledger = _copies_through(queue, p, d, stored, current, new_hist, new_ledger)
    mine = {o.id for o in stored.pending}
    return replace(
        st,
        baselines={**st.baselines, dk: new_hist},
        lookahead={**st.lookahead, dk: new_ledger},
        obligations=(*(o for o in st.obligations if o.id not in mine), *new_ledger.pending),
    )


def lookahead_done(st: PersistedState, since: datetime) -> bool:
    """Every configured direction has a look-ahead run written since `since`."""
    leds = [
        st.lookahead.get(direction_key(p.id, d))
        for p in (st.config.profiles if st.config else ())
        for d in Direction
    ]
    return all(
        x is not None and x.last_applied is not None and to_utc(x.last_applied) >= to_utc(since)
        for x in leds
    )


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
        n = replace(pending[new], webhook_copied=True)
        c = next((c for c in queue.copies.values() if c.obligation.id == old), None)
        if c is not None:  # the same copy: its webhook-id, receipts and tries stay
            queue.rewrite(c, n)
            pending[new] = n
        elif was[old].webhook_copied or old in queue.copied:  # it ended: no second one
            pending[new] = n
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
        b = new_hist.in_force(day)
        o = replace(
            o,
            baseline_version=b.version if b else None,
            fact_key=bundle_key(rest),
            event=replace(o.event, kind=kind, text=text),
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
    return replace(
        new_ledger,
        pending=tuple(pending.values()),
        facts={**carry, **rewritten, **new_ledger.facts},
    )
