"""The serialised writer's write-failure ledger (7.12 rule 22): only a written state is applied, and
what a failed write could not apply waits here, replayed onto whatever is written next."""

import logging
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field, fields, replace
from enum import Enum, StrEnum, auto
from typing import Any

from train_commute_companion.model import (
    Obligation,
    Severity,
    WebhookCopy,
    okey,
    split_direction_key,
)
from train_commute_companion.revisions import find_profile, same_route
from train_commute_companion.state import OccurrenceState, is_sent
from train_commute_companion.store import PersistedState, StorageError, Store
from train_commute_companion.webhook_queue import CopyQueue

log = logging.getLogger(__name__)


class Change(Enum):
    """What a failed write does with a change (rule 22): does it become the newest to retry, or
    only when nothing else waits; is it replayed onto whatever is written next, or recomputed."""

    RECOMPUTED = auto()  # a poll, a reload: the newest to retry, recomputed by the next poll
    TICK = auto()  # a watchdog tick: retried only when nothing else waits, recomputed as well
    ACTION = auto()  # a user action: the newest to retry and replayed, nothing recomputes it
    LOOKAHEAD = auto()  # a look-ahead run: retried only when nothing else waits, and replayed


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


class WritePhase(StrEnum):
    DURABLE = "durable"
    PROPOSED = "proposed"
    PENDING = "pending"
    DEGRADED = "degraded"


@dataclass(slots=True)
class _WriteJournal:
    base: PersistedState | None = None
    ops: list[Callable[[PersistedState], PersistedState]] = field(default_factory=list)
    marks: dict[str, Mapping[str, str]] = field(default_factory=dict)
    held: dict[str, Obligation] = field(default_factory=dict)
    gone: set[str] = field(default_factory=set)
    told: set[tuple[str | None, str, Severity]] = field(default_factory=set)
    shadow: PersistedState | None = None

    @property
    def pending(self) -> bool:
        return self.shadow is not None


class StateWriter:
    """`st` is what everything runs from, read-only: the last written (or loaded) state, plus a
    candidate proposed since the last commit. Only a commit decides a candidate's fate: written,
    it is applied; failed, `st` falls back to the written state, which while writes fail only
    gains what unwritten changes observed and told (poll records, the delivery ledger in
    `_marks`), never their decisions. `_durable` is that written state as written. Pending until
    one write lands: `_base` the newest change, retried by the watchdog (a poll's is recomputed
    anyway); `_ops` the changes nothing recomputes (a user action, a look-ahead run, delivery
    outcomes), replayed onto whatever is written next; `_held` their new alerts, held until recipient pins are durable
    and judged on `_shadow` (the last state a write tried); `_gone` obligations finished
    meanwhile; `_told` alerts sent from memory. `_notes`: `storage_failed`, which never uses the
    store. `outbox`: the webhook copies a write takes along, which live apart from the state."""

    def __init__(
        self, store: Store, outbox: CopyQueue, storage_note: Callable[[], Obligation]
    ) -> None:
        self._store, self._outbox, self._storage_note = store, outbox, storage_note
        self._st = PersistedState(
            config=None,
            config_snapshots={},
            baselines={},
            lookahead={},
            occurrences={},
            obligations=(),
            started_before=False,
        )
        self.store_ok = True
        self._durable = self._st
        self._journal = _WriteJournal()
        self._notes: dict[str, Obligation] = {}

    @property
    def st(self) -> PersistedState:
        return self._st

    def audience_written(self, o: Obligation) -> bool:
        """A private send needs its recipient pin in the last successful write."""
        if o.target_ids is None:
            return False
        saved = (
            *self._durable.obligations,
            *(x for ledger in self._durable.lookahead.values() for x in ledger.pending),
            *(copy.obligation for copy in self._durable.webhook_copies),
        )
        return any(
            x.id == o.id
            and x.target_ids == o.target_ids
            and x.primary_target_pending == o.primary_target_pending
            for x in saved
        )

    def copy_audience_written(self, copy: WebhookCopy) -> bool:
        """Auxiliary delivery uses only the copy's durably pinned destination IDs."""
        return copy.target_ids is not None and any(
            saved.id == copy.id and saved.target_ids == copy.target_ids
            for saved in self._durable.webhook_copies
        )

    @property
    def waiting(self) -> bool:
        """A change is waiting for a write that lands."""
        return self.phase is WritePhase.PENDING

    @property
    def phase(self) -> WritePhase:
        if self._journal.pending:
            return WritePhase.PENDING
        if self._st is not self._durable:
            return WritePhase.PROPOSED
        return WritePhase.DURABLE if self.store_ok else WritePhase.DEGRADED

    def judged_state(self) -> PersistedState:
        """What the alerts of unwritten changes are judged on: what the last write tried."""
        return self._st if self._journal.shadow is None else self._journal.shadow

    def begin(self, st: PersistedState) -> None:
        """The state a start runs from, as if just written."""
        if self.phase is WritePhase.PENDING:
            raise RuntimeError("cannot replace a transaction with pending changes")
        self._st = self._durable = st

    def propose(self, st: PersistedState) -> None:
        """An unwritten candidate everything runs from until the next commit decides it."""
        self._st = st

    def retry(self) -> None:
        """The waiting changes, written again."""
        if self._journal.base is not None:
            self._write(self._journal.base, stage=False)

    def undelivered(self) -> int:
        """Alerts with a failed send, written or not (rule 22)."""
        live = [o for o in self._st.obligations if o.id not in self._journal.gone]
        return sum(
            o.attempts > 0 for o in (*live, *self._journal.held.values(), *self._notes.values())
        )

    def marks_of(self, k: str) -> Mapping[str, str]:
        """What was told of this occurrence while the store was down (rule 22)."""
        return self._journal.marks.get(k, {})

    def pending(self) -> Iterator[tuple[Obligation, bool]]:
        """Each alert owed a send, and whether it is held in memory, judged as it comes up: a
        write landing during an earlier send may have written it, ended it or removed it."""
        for o in (*self._st.obligations, *self._journal.held.values(), *self._notes.values()):
            if o.id in self._journal.gone:
                continue  # finished while the store was down (rule 22)
            mem = o.id in self._journal.held or o.id in self._notes
            if mem or any(x.id == o.id for x in self._st.obligations):
                yield o, o.id in self._journal.held

    def settle(self, o: Obligation, kept: Obligation | None, sent: bool) -> bool:
        """A send's outcome if `o` is sent from memory (where it is now: a write landing during
        the send may have written a held one); False when it is a written alert."""
        if o.id in self._notes:
            self._notes = _with_obligation(self._notes, o.id, kept)
        elif o.id in self._journal.held:
            self._journal.held = _with_obligation(self._journal.held, o.id, kept)
            if kept is None:
                self._journal.gone.add(o.id)
                if sent:
                    self._journal.told.add(_held_id(o))
        else:
            return False
        return True

    def commit(self, st: PersistedState, kind: Change) -> None:
        """Rule 22: writes the candidate `st` with every change still waiting."""
        self._st = st
        self._write(
            st,
            keep=kind in (Change.ACTION, Change.LOOKAHEAD),
            stage=kind in (Change.RECOMPUTED, Change.ACTION),
        )

    def commit_strict(self, state: PersistedState) -> bool:
        """Recovery actions apply only after a write. A failed action is never queued for replay."""
        if self.phase is WritePhase.PENDING:
            return False
        try:
            written = _prune(self._outbox.with_copies(state))
            self._store.save(written)
        except StorageError as error:
            self.store_ok = False
            log.error("store write failed: %s", error)
            note = self._storage_note()
            self._notes[note.id] = note
            return False
        self._st = self._durable = written
        self.store_ok = True
        self._notes = {}
        return True

    def commit_delivery(
        self,
        st: PersistedState,
        outcome: Callable[[PersistedState], PersistedState],
        finished: Collection[str],
    ) -> None:
        """A delivery pass, `outcome` being what it did to any state: replayed onto whatever is
        written next after a failed write, which must not bring the `finished` alerts back."""
        self._st = st
        self._journal.gone |= set(finished)
        mem = set(self._journal.held) | set(
            self._notes
        )  # read before a landed write empties `_held`
        if self._write(st, stage=False, op=outcome):
            self._outbox.delivery_written(mem)

    def _write(
        self,
        cand: PersistedState,
        *,
        keep: bool = False,
        stage: bool = True,
        op: Callable[[PersistedState], PersistedState] | None = None,
    ) -> bool:
        """Rule 22: writes `cand` as one with every change still waiting; only once written is
        it applied. A failed write leaves the last written state running and keeps the change:
        as the newest to retry (`stage`), and when nothing recomputes it (`keep`, or its own
        `op`) to replay onto whatever is written next. Its new alerts go from memory. A first
        configuration runs anyway: nothing else could."""
        # only what this change did: restaging carries other waiting changes it must not replace
        mine = _Delta(self._durable, cand) if op is None and keep else None
        if self._journal.base is not None and cand is not self._journal.base:
            cand = self._restage(self._durable, cand)  # what waits on other occurrences too
        if mine is not None:
            op = mine
            for x in self._journal.ops:  # a newer action supersedes an older one (rule 22)
                if isinstance(x, _Delta):
                    x.supersede(mine)
        try:
            write = _prune(self._outbox.with_copies(self._compose(cand)))
            self._store.save(write)
        except StorageError as e:
            self._pend(cand, stage, op)
            if self.store_ok:  # one loud storage_failed per outage, held in memory only
                self.store_ok = False
                log.error("store write failed: %s", e)
                o = self._storage_note()
                self._notes[o.id] = o
            return False
        self._st = self._durable = write
        self._journal = _WriteJournal()
        self.store_ok = True
        return True

    def _compose(self, cand: PersistedState) -> PersistedState:
        """`cand` with every change still waiting (rule 22): the kept changes replayed, the
        newest poll records and delivery ledger, held alerts in and finished ones out."""
        if self._journal.shadow is None and not self._journal.held and not self._journal.gone:
            return cand
        st = cand
        for op in self._journal.ops:
            st = op(st)
        occs = {}
        for k, s in st.occurrences.items():
            # the running state holds the newest of both; an occurrence it lacks, the marks
            cur = self._st.occurrences.get(k)
            occs[k] = (
                replace(s, alert_keys=self._journal.marks.get(k, s.alert_keys))
                if cur is None
                else replace(s, alert_keys=cur.alert_keys, poll_records=cur.poll_records)
            )
        known = {o.id for o in self._durable.obligations}
        taken = {_held_id(o) for o in self._journal.held.values()} | self._journal.told
        # a held alert whose copy was made is written with its mark (rule 22)
        held = {k: self._outbox.marked(o) for k, o in self._journal.held.items()}
        obls = [
            held.get(o.id, o)
            for o in st.obligations
            if o.id not in self._journal.gone
            and (o.id in known or o.id in held or _held_id(o) not in taken)
        ]
        ids = {o.id for o in obls}
        obls += [o for o in held.values() if o.id not in ids]
        ledgers = {
            k: replace(
                v,
                pending=tuple(
                    held.get(o.id, o) for o in v.pending if o.id not in self._journal.gone
                ),
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
            self._journal.ops.append(op)
        if stage or self._journal.base is None:
            self._journal.base = self._restage(base, cand)
        known = {o.id for o in base.obligations}
        taken = {_held_id(o) for o in self._journal.held.values()} | self._journal.told
        for o in cand.obligations:
            if (
                o.id in known
                or o.id in self._journal.held
                or o.id in self._journal.gone
                or _held_id(o) in taken
            ):
                continue  # a recomputed alert already held or sent from memory is not again
            self._journal.held[o.id] = o
            taken.add(_held_id(o))
        for k, x in self._st.occurrences.items():  # the newest ledger, computed on the marks
            was = base.occurrences.get(k)
            if k in self._journal.marks or was is None or was.alert_keys != x.alert_keys:
                self._journal.marks[k] = x.alert_keys
        if base.config is None and cand.config is not None:
            base = replace(base, config=cand.config, config_snapshots=cand.config_snapshots)
            self._durable = base
        occs = {}
        for k, x in base.occurrences.items():
            cur = self._st.occurrences.get(k)
            occs[k] = replace(
                x,
                alert_keys=self._journal.marks.get(k, x.alert_keys),
                poll_records=x.poll_records if cur is None else cur.poll_records,
            )
        self._st = replace(base, occurrences=occs)
        # a journey fact told from memory that a healthy poll ended leaves the ledger: its
        # recurrence is news again (7.5), as it is with a working store
        self._journal.told = {
            t
            for t in self._journal.told
            if t[0] is None
            or (s := self._st.occurrences.get(t[0])) is not None
            and is_sent(s.alert_keys, t[1])
        }
        self._journal.shadow = self._compose(self._journal.base)

    def _restage(self, base: PersistedState, cand: PersistedState) -> PersistedState:
        """The newest change to retry: `cand`, keeping every occurrence an earlier waiting
        change made that `cand` left as written (a poll of another occurrence, a no-op)."""
        old = self._journal.base
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
