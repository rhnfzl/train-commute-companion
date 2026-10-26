"""Versioned atomic JSON store (7.12 rules 11 and 22). One file, replaced whole."""

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from train_commute_companion.delivery import Obligation
from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.lookahead import BaselineHistory, LookAheadLedger
from train_commute_companion.model import Config
from train_commute_companion.serde import decode, encode

VERSION = 1


class StorageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PersistedState:
    config: Config | None
    config_snapshots: Mapping[int, Config]
    baselines: Mapping[str, BaselineHistory]
    lookahead: Mapping[str, LookAheadLedger]
    occurrences: Mapping[str, OccurrenceState]
    obligations: tuple[Obligation, ...]
    started_before: bool


class Store(Protocol):
    def load(self) -> PersistedState | None: ...
    def save(self, s: PersistedState) -> None: ...


def _read_counter(path: Path) -> int | None:
    """The write counter on disk; None when there is no file. Anything unreadable is an error."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise StorageError(f"cannot read {path.name}: {e}") from e
    try:
        counter = json.loads(raw)["write_counter"]
    except (ValueError, KeyError, TypeError) as e:
        raise StorageError(f"{path.name} is not a readable store file") from e
    if isinstance(counter, bool) or not isinstance(counter, int):
        raise StorageError(f"{path.name} has no valid write counter")
    return counter


class JsonFileStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._marker = path.with_name(path.name + ".started")
        self._last: int | None = None  # the counter this store last read or wrote; None: no file

    def _set_aside(self, why: str) -> StorageError:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        broken = self._path.with_name(f"{self._path.name}.broken-{stamp}")
        try:
            os.replace(self._path, broken)
        except OSError as e:
            return StorageError(f"{why}; could not move {self._path.name} aside: {e}")
        self._last = None
        return StorageError(f"{why}; kept as {broken.name}")

    def load(self) -> PersistedState | None:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            self._last = None
            if self._marker.exists():
                raise StorageError(
                    f"{self._path.name} is missing but this app started before: state lost"
                ) from None
            return None
        except OSError as e:
            raise self._set_aside(f"{self._path.name} is unreadable ({e})") from e
        try:
            env: Any = json.loads(raw)
            if not isinstance(env, dict) or env.get("version") != VERSION:
                raise ValueError(
                    f"unknown version {env.get('version') if isinstance(env, dict) else env!r}"
                )
            counter = env["write_counter"]
            if isinstance(counter, bool) or not isinstance(counter, int):
                raise TypeError("bad write counter")
            state = decode(PersistedState, env["state"])
        except (ValueError, KeyError, TypeError) as e:  # JSON errors are ValueErrors
            raise self._set_aside(
                f"{self._path.name} is corrupt or cannot be migrated ({e})"
            ) from e
        self._last = counter
        return state  # type: ignore[no-any-return]

    def save(self, s: PersistedState) -> None:
        if _read_counter(self._path) != self._last:
            raise StorageError(
                f"{self._path.name} was changed by someone else since this app last read or "
                "wrote it; refusing to merge two histories"
            )
        counter = (self._last or 0) + 1
        text = json.dumps(
            {"version": VERSION, "write_counter": counter, "state": encode(s)}, sort_keys=True
        )
        tmp = ""
        new_marker = False
        try:
            fd, tmp = tempfile.mkstemp(".new", "store-", self._path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            new_marker = not self._marker.exists()
            self._marker.touch()  # before the commit: nothing after os.replace can fail
            os.replace(tmp, self._path)  # the commit point
            tmp = ""
        except OSError as e:
            if tmp and new_marker:  # no store was ever written: a marker would mean "state lost"
                with contextlib.suppress(OSError):
                    self._marker.unlink()
            raise StorageError(f"cannot write {self._path.name}: {e}") from e
        finally:
            if tmp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
        self._last = counter
