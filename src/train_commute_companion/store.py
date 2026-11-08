"""Versioned atomic JSON store (7.12 rules 11 and 22). One file, replaced whole."""

import contextlib
import errno
import json
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from train_commute_companion.calendar import to_utc
from train_commute_companion.compensation_state import JourneyRecord
from train_commute_companion.model import (
    Config,
    EventKind,
    Obligation,
    WebhookCopy,
    okey,
)
from train_commute_companion.serde import decode, encode
from train_commute_companion.state import BaselineHistory, LookAheadLedger, OccurrenceState

VERSION = 2
_V1 = 1  # read for as long as such files may exist; only VERSION is ever written


@contextlib.contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise StorageError(
                    f"{path.name} is busy with another writer; retry later"
                ) from error
            raise
        try:
            yield
        finally:
            if sys.platform == "win32":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class StorageError(Exception):
    pass


class StorePhase(StrEnum):
    UNREAD = "unread"
    FRESH = "fresh"
    READY = "ready"
    LOST = "lost"
    QUARANTINED = "quarantined"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class PersistedState:
    config: Config | None
    config_snapshots: Mapping[int, Config]
    baselines: Mapping[str, BaselineHistory]
    lookahead: Mapping[str, LookAheadLedger]
    occurrences: Mapping[str, OccurrenceState]
    obligations: tuple[Obligation, ...]
    started_before: bool
    webhook_copies: tuple[WebhookCopy, ...] = ()  # owed webhook copies (rule 22)
    # `url_id` -> end of its rest (UTC) for every webhook URL whose last try failed
    webhook_down: Mapping[str, datetime] = field(default_factory=dict)
    works_digest_weeks: Mapping[str, date] = field(default_factory=dict)
    journey_records: Mapping[str, JourneyRecord] = field(default_factory=dict)


class Store(Protocol):
    def load(self) -> PersistedState | None:
        """The saved state; None when there is no file yet. Raises StorageError when the state is
        lost or unusable; a file that cannot be read or is corrupt may first be set aside, and is
        never overwritten."""
        ...

    def save(self, s: PersistedState) -> None:
        """Raises StorageError when the state was not written, the only failure the runtime
        handles: an OSError must be wrapped."""
        ...


def _owes_leave_now(s: OccurrenceState, k: str, obligations: tuple[Obligation, ...]) -> bool:
    a = s.advice
    return (
        a is not None
        and not a.withdrawn
        and a.sent_at is not None
        and any(
            o.event.kind is EventKind.LEAVE_NOW
            and o.occurrence is not None
            and okey(o.occurrence) == k
            and o.event.train == a.advised_train
            and to_utc(o.event.created_at) == to_utc(a.sent_at)
            for o in obligations
        )
    )


def _from_v1(raw: Any) -> PersistedState:
    """A version 1 file as the current model has it. That number covered formats that differ:
    an advice record may lack `delivered` (it counts as told, unless its LEAVE_NOW is still owed),
    a LEAVE_NOW may hold its planned departure as a frozen expiry, and an id may name neither
    profile nor direction, so two profiles' alerts may share it."""
    for s in raw.get("occurrences", {}).values():
        if s.get("advice") is not None:
            s["advice"].setdefault("delivered", True)
    st: PersistedState = decode(PersistedState, raw)
    occs = {
        k: replace(s, advice=replace(s.advice, delivered=False))
        if s.advice is not None and _owes_leave_now(s, k, st.obligations)
        else s
        for k, s in st.occurrences.items()
    }
    obligations = tuple(
        replace(o, expires_at=None) if o.event.kind is EventKind.LEAVE_NOW else o
        for o in st.obligations
    )
    ids = Counter(o.id for o in obligations)
    obligations = tuple(
        o
        if ids[o.id] == 1
        else replace(
            o,
            id=f"{o.event.profile_id}|"
            f"{o.event.direction.value if o.event.direction else ''}|{o.id}",
        )
        for o in obligations
    )
    return replace(st, occurrences=occs, obligations=obligations)


def _check_version(version: object) -> None:
    if type(version) is not int or version not in (_V1, VERSION):
        raise ValueError(f"unknown version {version!r}")


def read_state(version: object, raw: Any) -> PersistedState:
    """The state saved under `version`, as the current model has it. Raises ValueError for a
    version this code cannot read."""
    _check_version(version)
    if version == _V1:
        return _from_v1(raw)
    st: PersistedState = decode(PersistedState, raw)
    return st


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
    except (ValueError, KeyError, TypeError, RecursionError) as e:
        raise StorageError(f"{path.name} is not a readable store file") from e
    if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
        raise StorageError(f"{path.name} has no valid write counter")
    return counter


class JsonFileStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._marker = path.with_name(path.name + ".started")
        self._last: int | None = None  # the counter this store last read or wrote; None: no file
        self.phase = StorePhase.UNREAD

    def _set_aside(self, why: str) -> StorageError:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        broken = self._path.with_name(f"{self._path.name}.broken-{stamp}")
        try:
            os.replace(self._path, broken)
        except OSError as e:
            return StorageError(f"{why}; could not move {self._path.name} aside: {e}")
        self._last = None
        self.phase = StorePhase.QUARANTINED
        return StorageError(f"{why}; kept as {broken.name}")

    def load(self) -> PersistedState | None:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            self._last = None
            if self._marker.exists():
                self.phase = StorePhase.LOST
                raise StorageError(
                    f"{self._path.name} is missing but this app started before: state lost"
                ) from None
            self.phase = StorePhase.FRESH
            return None
        except OSError as e:
            raise self._set_aside(f"{self._path.name} is unreadable ({e})") from e
        try:
            env: Any = json.loads(raw)
            # first, because the reason reaches the user in the "state was lost" warning
            if not isinstance(env, dict):
                raise TypeError(f"unknown version {env!r}")
            _check_version(env.get("version"))
            counter = env["write_counter"]
            if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
                raise TypeError("bad write counter")
            state = read_state(env["version"], env["state"])
        # JSON errors are ValueErrors; AttributeError: a version 1 state of the wrong shape;
        # RecursionError: JSON nested deeper than the parser goes
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as e:
            raise self._set_aside(
                f"{self._path.name} is corrupt or cannot be migrated ({e})"
            ) from e
        self._last = counter
        self.phase = StorePhase.READY
        return state

    def save(self, s: PersistedState) -> None:
        try:
            # Keep the counter check and rename under the same process ownership lock.
            with _write_lock(self._path):
                self._save_owned(s)
        except OSError as error:
            raise StorageError(f"cannot own {self._path.name}: {error}") from error

    def _save_owned(self, s: PersistedState) -> None:
        if self.phase is StorePhase.CONFLICT:
            raise StorageError("store ownership was lost; reload before writing another history")
        if _read_counter(self._path) != self._last:
            self.phase = StorePhase.CONFLICT
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
        self.phase = StorePhase.READY
