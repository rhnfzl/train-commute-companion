"""The serialised writer's write-failure ledger (7.12 rule 22): only a written state is applied, and
what a failed write could not apply waits here, replayed onto whatever is written next."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import fields, replace
from typing import Any, Protocol

from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.model import (
    SENT_PREFIX,
    Obligation,
    Severity,
    okey,
    split_direction_key,
)
from train_commute_companion.revisions import find_profile, same_route
from train_commute_companion.store import PersistedState, StorageError, Store

log = logging.getLogger(__name__)


class Outbox(Protocol):
    """What a write takes from the webhook copies, which live apart from the state."""

    copied: set[str]

    def written(self, st: PersistedState) -> PersistedState: ...


def _held_id(o: Obligation) -> tuple[str | None, str, Severity]:
    """An alert's identity across recomputations of the same change (rule 22)."""
    return None if o.occurrence is None else okey(o.occurrence), o.event.key, o.event.severity


def _with_obligation(
    d: Mapping[str, Obligation], i: str, r: Obligation | None
) -> dict[str, Obligation]:
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
        pid = split_direction_key(dk)[0]
        then, now = find_profile(self.cfg, pid), find_profile(st.config, pid)
        return then is not None and now is not None and same_route(then, now)

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


class StateWriter:
    """`st` is the last written (or loaded) state and everything runs from it; while writes fail
    it only gains what unwritten changes observed and told (poll records, the delivery ledger in
    `marks`), never their decisions. `_durable` is that written state as written. Pending until
    one write lands: `_base` the newest change, retried by the watchdog (a poll's is recomputed
    anyway); `_ops` the changes nothing recomputes (a user action, a look-ahead run, delivery
    outcomes), replayed onto whatever is written next; `held` their new alerts, sent from memory
    and judged on `_shadow` (the last state a write tried); `gone` obligations finished meanwhile;
    `_told` alerts sent from memory. `notes`: `storage_failed`, which never uses the store."""

    def __init__(
        self, store: Store, outbox: Outbox, storage_note: Callable[[], Obligation]
    ) -> None:
        self._store, self._outbox, self._storage_note = store, outbox, storage_note
        self.st = PersistedState(None, {}, {}, {}, {}, (), False)
        self.store_ok = True
        self._durable = self.st
        self._base: PersistedState | None = None
        self._ops: list[Callable[[PersistedState], PersistedState]] = []
        self.marks: dict[str, Mapping[str, str]] = {}
        self.held: dict[str, Obligation] = {}
        self.gone: set[str] = set()
        self._told: set[tuple[str | None, str, Severity]] = set()
        self._shadow: PersistedState | None = None
        self.notes: dict[str, Obligation] = {}

    @property
    def waiting(self) -> bool:
        """A change is waiting for a write that lands."""
        return self._shadow is not None

    def judged(self) -> PersistedState:
        """What the alerts of unwritten changes are judged on: what the last write tried."""
        return self.st if self._shadow is None else self._shadow

    def begin(self, st: PersistedState) -> None:
        """The state a start runs from, as if just written."""
        self.st = self._durable = st

    def retry(self) -> None:
        """The waiting changes, written again."""
        if self._shadow is not None:
            self.save(stage=False, cand=self._base)

    def undelivered(self) -> int:
        """Alerts with a failed send, written or not (rule 22)."""
        live = [o for o in self.st.obligations if o.id not in self.gone]
        return sum(o.attempts > 0 for o in (*live, *self.held.values(), *self.notes.values()))

    def settle(self, o: Obligation, kept: Obligation | None, sent: bool) -> bool:
        """A send's outcome if `o` is sent from memory (where it is now: a write landing during
        the send may have written a held one); False when it is a written alert."""
        if o.id in self.notes:
            self.notes = _with_obligation(self.notes, o.id, kept)
        elif o.id in self.held:
            self.held = _with_obligation(self.held, o.id, kept)
            if kept is None:
                self.gone.add(o.id)
                if sent:
                    self._told.add(_held_id(o))
        else:
            return False
        return True

    def save(
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
        cand = self.st if cand is None else cand
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
            write = _prune(self._outbox.written(self._compose(cand)))
            self._store.save(write)
        except StorageError as e:
            self._pend(cand, stage, op)
            if self.store_ok:  # one loud storage_failed per outage, held in memory only
                self.store_ok = False
                log.error("store write failed: %s", e)
                o = self._storage_note()
                self.notes[o.id] = o
            return False
        self.st = self._durable = write
        self._base, self._ops, self.marks, self._shadow = None, [], {}, None
        self.held, self.gone, self._told = {}, set(), set()
        self.store_ok = True
        return True

    def _compose(self, cand: PersistedState) -> PersistedState:
        """`cand` with every change still waiting (rule 22): the kept changes replayed, the
        newest poll records and delivery ledger, held alerts in and finished ones out."""
        if self._shadow is None and not self.held and not self.gone:
            return cand
        st = cand
        for op in self._ops:
            st = op(st)
        occs = {}
        for k, s in st.occurrences.items():
            # the running state holds the newest of both; an occurrence it lacks, the marks
            cur = self.st.occurrences.get(k)
            occs[k] = (
                replace(s, alert_keys=self.marks.get(k, s.alert_keys))
                if cur is None
                else replace(s, alert_keys=cur.alert_keys, poll_records=cur.poll_records)
            )
        known = {o.id for o in self._durable.obligations}
        taken = {_held_id(o) for o in self.held.values()} | self._told
        # a held alert whose copy was made is written with its mark (rule 22)
        held = {
            k: replace(o, webhook_copied=True) if k in self._outbox.copied else o
            for k, o in self.held.items()
        }
        obls = [
            held.get(o.id, o)
            for o in st.obligations
            if o.id not in self.gone and (o.id in known or o.id in held or _held_id(o) not in taken)
        ]
        ids = {o.id for o in obls}
        obls += [o for o in held.values() if o.id not in ids]
        ledgers = {
            k: replace(
                v,
                pending=tuple(held.get(o.id, o) for o in v.pending if o.id not in self.gone),
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
        taken = {_held_id(o) for o in self.held.values()} | self._told
        for o in cand.obligations:
            if o.id in known or o.id in self.held or o.id in self.gone or _held_id(o) in taken:
                continue  # a recomputed alert already held or sent from memory is not again
            self.held[o.id] = o
            taken.add(_held_id(o))
        for k, x in self.st.occurrences.items():  # the newest ledger, computed on the marks
            was = base.occurrences.get(k)
            if k in self.marks or was is None or was.alert_keys != x.alert_keys:
                self.marks[k] = x.alert_keys
        if base.config is None and cand.config is not None:
            base = replace(base, config=cand.config, config_snapshots=cand.config_snapshots)
            self._durable = base
        occs = {}
        for k, x in base.occurrences.items():
            cur = self.st.occurrences.get(k)
            occs[k] = replace(
                x,
                alert_keys=self.marks.get(k, x.alert_keys),
                poll_records=x.poll_records if cur is None else cur.poll_records,
            )
        self.st = replace(base, occurrences=occs)
        # a journey fact told from memory that a healthy poll ended leaves the ledger: its
        # recurrence is news again (7.5), as it is with a working store
        self._told = {
            t
            for t in self._told
            if t[0] is None
            or (s := self.st.occurrences.get(t[0])) is not None
            and SENT_PREFIX + t[1] in s.alert_keys
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
