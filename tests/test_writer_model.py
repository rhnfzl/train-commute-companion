"""Exhaustive traces of the single-writer protocol, bounded to four operations."""

from dataclasses import dataclass, replace
from enum import Enum
from itertools import product

from lifecycle_fixtures import at, ctx, occ

from train_commute_companion.model import AlertEvent, EventKind, Obligation, Severity, okey
from train_commute_companion.replay import MemoryStore
from train_commute_companion.state_writer import Change, StateWriter
from train_commute_companion.store import PersistedState, StorageError
from train_commute_companion.webhook_queue import CopyQueue


class Operation(Enum):
    SKIP = "skip"
    POLL = "poll"
    LOOKAHEAD = "lookahead"
    RETRY = "retry"


@dataclass(frozen=True)
class SkipModel:
    saved: bool = False
    requested: bool = False

    def step(self, operation: Operation, disk_up: bool) -> "SkipModel":
        requested = self.requested or operation is Operation.SKIP
        return SkipModel(requested if disk_up else self.saved, requested)


class InterruptedStore(MemoryStore):
    available = True

    def save(self, state):
        if not self.available:
            raise StorageError("disk full")
        super().save(state)


def test_skip_survives_every_short_write_trace():
    occurrence = occ(None)
    key = okey(occurrence.key)
    now = at("07:50")
    profile = ctx(now).profile
    alert = AlertEvent(
        profile.id,
        profile.name,
        occurrence.key.direction,
        occurrence.key.service_date,
        None,
        EventKind.STORAGE_FAILED,
        Severity.LOUD,
        None,
        "Disk full",
        "disk-full",
        now,
    )
    note = Obligation("disk-full", alert, None, None, 1, None, "disk-full", None, 0)
    initial = PersistedState(None, {}, {}, {}, {key: occurrence}, (), True)
    steps = tuple(product(Operation, (False, True)))
    for length in range(1, 5):
        for trace in product(steps, repeat=length):
            store = InterruptedStore()
            store.save(initial)
            copies = CopyQueue(None, lambda: now, lambda _: None, lambda _: False, lambda _: None)
            writer = StateWriter(store, copies, lambda: note)
            writer.begin(initial)
            model = SkipModel()
            for index, (operation, disk_up) in enumerate(trace):
                store.available = disk_up
                state = writer.st
                current = state.occurrences[key]
                if operation is Operation.SKIP:
                    skipped = replace(current, not_travelling=True)
                    writer.commit(replace(state, occurrences={key: skipped}), Change.ACTION)
                elif operation is Operation.POLL:
                    polled = replace(current, generation=current.generation + 1)
                    writer.commit(replace(state, occurrences={key: polled}), Change.RECOMPUTED)
                elif operation is Operation.LOOKAHEAD:
                    writer.commit(state, Change.LOOKAHEAD)
                else:
                    writer.retry()
                model = model.step(operation, disk_up)
                observed = store.load()
                assert observed is not None
                prefix = " → ".join(
                    f"{op.value} ({'saved' if success else 'disk full'})"
                    for op, success in trace[: index + 1]
                )
                assert observed.occurrences[key].not_travelling == model.saved, prefix
                assert writer.st.occurrences[key].not_travelling == model.saved, prefix
