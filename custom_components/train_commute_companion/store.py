"""The core store with the rule 22 "ran before" marker kept in the config entry, so losing the
state directory together with its own marker still reads as state lost."""

import threading
from collections.abc import Callable
from pathlib import Path

from train_commute_companion.store import JsonFileStore, PersistedState, StorageError

_LOCKS: dict[Path, threading.Lock] = {}


def file_lock(path: Path) -> threading.Lock:
    """One lock per state file, shared by every runtime generation that opens it."""
    return _LOCKS.setdefault(path, threading.Lock())


class EntryMarkedStore:
    def __init__(
        self,
        inner: JsonFileStore,
        started: bool,
        on_first_save: Callable[[], None],
        retired: threading.Event,
        lock: threading.Lock,
    ) -> None:
        self._inner = inner
        # set once the host gives this runtime up. Checked under the file lock and held across
        # the whole read or write, so a runtime that outlived its stop either finishes before its
        # successor touches the file or does nothing at all
        self._retired = retired
        self._lock = lock
        self._started = started
        self._on_first_save: Callable[[], None] | None = on_first_save

    def _check(self) -> None:
        if self._retired.is_set():
            raise StorageError("this runtime was stopped; its successor owns the state file")

    def load(self) -> PersistedState | None:
        with self._lock:
            self._check()
            state = self._inner.load()
        if state is None and self._started:
            raise StorageError(
                "the saved state file is missing but this integration ran before: state lost"
            )
        return state

    def save(self, s: PersistedState) -> None:
        with self._lock:
            self._check()
            self._inner.save(s)
            if (mark := self._on_first_save) is not None:
                self._on_first_save = None
                mark()
