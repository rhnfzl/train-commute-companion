"""The core store with the rule 22 "ran before" marker kept in the config entry, so losing the
state directory together with its own marker still reads as state lost."""

import threading
from collections.abc import Callable

from train_commute_companion.store import JsonFileStore, PersistedState, StorageError


class EntryMarkedStore:
    def __init__(
        self,
        inner: JsonFileStore,
        started: bool,
        on_first_save: Callable[[], None],
        retired: threading.Event,
    ) -> None:
        self._inner = inner
        # set once the host gives this runtime up, so a runtime that outlived its stop never
        # overwrites the state its successor owns
        self._retired = retired
        self._started = started
        self._on_first_save: Callable[[], None] | None = on_first_save

    def load(self) -> PersistedState | None:
        state = self._inner.load()
        if state is None and self._started:
            raise StorageError(
                "the saved state file is missing but this integration ran before: state lost"
            )
        return state

    def save(self, s: PersistedState) -> None:
        if self._retired.is_set():
            raise StorageError("this runtime was stopped; its successor owns the state file")
        self._inner.save(s)
        if (mark := self._on_first_save) is not None:
            self._on_first_save = None
            mark()
