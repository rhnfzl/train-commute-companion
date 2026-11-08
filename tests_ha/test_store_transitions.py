import threading

import pytest

from custom_components.train_commute_companion.store import EntryMarkedStore
from train_commute_companion.store import JsonFileStore, PersistedState, StorageError


def test_first_save_establishes_local_ownership_even_if_directory_is_lost(tmp_path):
    path = tmp_path / "state.json"
    marked = []
    store = EntryMarkedStore(
        JsonFileStore(path), False, lambda: marked.append(True), threading.Event(), threading.Lock()
    )
    state = PersistedState(None, {}, {}, {}, {}, (), False)
    assert store.load() is None
    store.save(state)
    assert marked == [True]
    path.unlink()
    path.with_name(path.name + ".started").unlink()
    with pytest.raises(StorageError, match="state lost"):
        store.load()


def test_failed_entry_marker_remains_pending_for_retry(tmp_path):
    attempts = []

    def mark():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("entry loop unavailable")

    store = EntryMarkedStore(
        JsonFileStore(tmp_path / "state.json"),
        False,
        mark,
        threading.Event(),
        threading.Lock(),
    )
    state = PersistedState(None, {}, {}, {}, {}, (), False)
    with pytest.raises(StorageError, match="start marker"):
        store.save(state)
    assert store.load() is not None
    store.save(state)
    assert attempts == [True, True]
