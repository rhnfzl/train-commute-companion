"""Transaction phases protect pending actions and historical store compatibility."""

from dataclasses import replace
from datetime import datetime

import pytest
from lifecycle_fixtures import full_state
from test_delivery import ob

from train_commute_companion.calendar import TZ
from train_commute_companion.model import EventKind
from train_commute_companion.replay import MemoryStore
from train_commute_companion.state_writer import Change, StateWriter
from train_commute_companion.store import StorageError
from train_commute_companion.webhook_queue import CopyQueue


class FlakyStore(MemoryStore):
    failing = False

    def save(self, state):
        if self.failing:
            raise StorageError("disk full")
        super().save(state)


def writer():
    store = FlakyStore()
    queue = CopyQueue(
        None, lambda: datetime.now(TZ), lambda _: None, lambda _: False, lambda _: None
    )
    return StateWriter(store, queue, lambda: ob(EventKind.STORAGE_FAILED)), store


def test_pending_action_cannot_be_discarded_by_starting_again():
    w, store = writer()
    w.begin(full_state())
    store.failing = True
    w.commit(replace(w.st, started_before=False), Change.ACTION)
    assert w.waiting
    with pytest.raises(RuntimeError, match="pending"):
        w.begin(full_state())
    assert w.waiting
    store.failing = False
    w.retry()
    assert not w.waiting
    assert store.load().started_before is False


def test_writer_phase_is_transactional_and_strict_failure_is_not_replayed():
    from train_commute_companion.state_writer import WritePhase

    w, store = writer()
    original = full_state()
    w.begin(original)
    assert w.phase is WritePhase.DURABLE
    candidate = replace(original, started_before=False)
    w.propose(candidate)
    assert w.phase is WritePhase.PROPOSED
    store.failing = True
    w.commit(candidate, Change.ACTION)
    assert w.phase is WritePhase.PENDING
    assert not w.commit_strict(original)
    store.failing = False
    w.retry()
    assert w.phase is WritePhase.DURABLE and w.st.started_before is False
    store.failing = True
    assert not w.commit_strict(original)
    assert w.phase is WritePhase.DEGRADED
    store.failing = False
    w.commit(w.st, Change.RECOMPUTED)
    assert w.st.started_before is False
    assert w.phase is WritePhase.DURABLE


def test_pre_refactor_state_load_save_read_preserves_schema(tmp_path):
    import json
    from pathlib import Path

    from train_commute_companion.serde import encode
    from train_commute_companion.store import JsonFileStore

    fixture = Path(__file__).parent / "data/state/pre-explicit-v2.json"
    raw = json.loads(fixture.read_text())
    path = tmp_path / "state.json"
    path.write_text(fixture.read_text())
    store = JsonFileStore(path)
    state = store.load()
    assert encode(state) == raw["state"]
    store.save(state)
    assert JsonFileStore(path).load() == state
    assert json.loads(path.read_text())["state"] == raw["state"]


def test_legacy_advice_migrates_then_survives_current_save(tmp_path):
    import json
    from pathlib import Path

    from train_commute_companion.serde import encode
    from train_commute_companion.store import JsonFileStore

    raw = json.loads((Path(__file__).parent / "data/state/pre-explicit-v2.json").read_text())
    raw["version"] = 1
    for state in raw["state"]["occurrences"].values():
        if state["advice"] is not None:
            state["advice"].pop("delivered")
    path = tmp_path / "state.json"
    path.write_text(json.dumps(raw))
    store = JsonFileStore(path)
    state = store.load()
    assert all(o.advice is None or o.advice.delivered for o in state.occurrences.values())
    migrated = encode(state)
    store.save(state)
    assert encode(JsonFileStore(path).load()) == migrated
