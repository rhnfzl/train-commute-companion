import json
import os
import re
from dataclasses import replace
from pathlib import Path

import pytest
from lifecycle_fixtures import (
    L1,
    L2,
    L2X,
    LATER,
    ans,
    at,
    conn,
    ctx,
    full_state,
    journey,
    occ,
    poll,
)
from location_fixtures import USUAL, event, locate

from train_commute_companion.model import AdviceRecord, EventKind, Obligation, okey
from train_commute_companion.serde import encode
from train_commute_companion.state import OccurrenceState
from train_commute_companion.store import VERSION, JsonFileStore, PersistedState, StorageError


def test_corrupt_store_kept_aside_and_reported(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(StorageError, match=r"state\.json\.broken-"):
        JsonFileStore(path).load()
    assert not path.exists()
    (kept,) = tmp_path.glob("state.json.broken-*")
    assert kept.read_text() == "{not json"


@pytest.mark.parametrize(
    "text",
    [
        '{"version": 3, "write_counter": 1, "state": {}}',
        '{"version": 1, "write_counter": 1, "state": {"x": 1}}',
        '{"version": 1, "write_counter": 1, "state": {"occurrences": [1]}}',
        "[]",
    ],
)
def test_unknown_version_or_failed_migration_kept_aside(tmp_path, text):
    path = tmp_path / "state.json"
    path.write_text(text)
    with pytest.raises(StorageError, match="broken-"):
        JsonFileStore(path).load()
    assert len(list(tmp_path.glob("state.json.broken-*"))) == 1


def test_failed_write_leaves_old_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store = JsonFileStore(path)
    store.save(full_state())
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(StorageError):
        store.save(replace(full_state(), started_before=False))
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.new")) == []
    store.save(full_state())  # the failed attempt did not advance the counter


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    JsonFileStore(path).save(full_state())
    assert JsonFileStore(path).load() == full_state()


def test_missing_store_after_marker_is_state_loss(tmp_path):
    path = tmp_path / "state.json"
    JsonFileStore(path).save(full_state())
    path.unlink()
    with pytest.raises(StorageError, match="state lost"):
        JsonFileStore(path).load()


def test_store_detects_external_replace(tmp_path):
    path = tmp_path / "state.json"
    other = tmp_path / "backup.json"
    mine, theirs = JsonFileStore(path), JsonFileStore(other)
    mine.save(full_state())
    theirs.save(full_state())
    theirs.save(full_state())  # a different history: counter 2
    os.replace(other, path)  # user restores a backup while the app runs
    with pytest.raises(StorageError, match="someone else"):
        mine.save(full_state())
    assert JsonFileStore(path).load() == full_state()  # nothing merged, the backup is intact


def test_save_without_loading_an_existing_file_is_refused(tmp_path):
    path = tmp_path / "state.json"
    JsonFileStore(path).save(full_state())
    with pytest.raises(StorageError):
        JsonFileStore(path).save(full_state())


def test_first_install_no_store_no_error(tmp_path):
    store = JsonFileStore(tmp_path / "state.json")
    assert store.load() is None
    store.save(full_state())  # and the first save works


def test_retained_remainder_survives_restart(tmp_path):
    # the dwell fixture's missed-first-train context, saved and loaded between report and poll
    t = locate(occ(journey([L1, L2])), "08:14", event("AAA", "report", "08:13"), USUAL, LATER)
    assert t.state.retained
    path = tmp_path / "state.json"
    base = full_state()
    JsonFileStore(path).save(replace(base, occurrences={"k": t.state}))
    loaded = JsonFileStore(path).load()
    assert loaded is not None
    s = loaded.occurrences["k"]
    assert s == t.state
    a = poll(s, ctx(at("08:16")), ans("AAA", conn(L1, L2X), LATER, when="08:16"))
    assert any("if you are on the 08:12" in e.text for e in a.events)


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_run_fresh_after_storage_error_then_recovers(tmp_path, damage):
    """The caller may run on fresh state after a load error: the next save works and later loads are clean."""
    path = tmp_path / "state.json"
    JsonFileStore(path).save(full_state())
    if damage == "corrupt":
        path.write_text("{not json")
    else:
        path.unlink()
    store = JsonFileStore(path)
    with pytest.raises(StorageError):
        store.load()
    store.save(full_state())  # explicit fresh start, no extra reset of the marker needed
    assert JsonFileStore(path).load() == full_state()


def test_failed_marker_write_commits_nothing(tmp_path, monkeypatch):  # rule 22
    path = tmp_path / "state.json"
    store = JsonFileStore(path)
    store.save(full_state())
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(Path, "touch", boom)
    with pytest.raises(StorageError):
        store.save(replace(full_state(), started_before=False))
    monkeypatch.undo()
    assert path.read_bytes() == before  # memory and disk agree: the change was not applied
    store.save(full_state())  # the counter did not advance


def test_failed_first_write_is_not_state_loss(tmp_path, monkeypatch):
    path = tmp_path / "state.json"

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(StorageError):
        JsonFileStore(path).save(full_state())
    monkeypatch.undo()
    assert JsonFileStore(path).load() is None  # still a first installation


def _file(path: Path, version: int, st: PersistedState, *, keep_delivered: bool = True) -> None:
    raw = encode(st)
    if not keep_delivered:
        for s in raw["occurrences"].values():
            del s["advice"]["delivered"]
    path.write_text(json.dumps({"version": version, "write_counter": 1, "state": raw}))


def _leave_now(o: Obligation, s: OccurrenceState) -> Obligation:
    """`o` as the LEAVE_NOW still owed for the advice of `s`, with a frozen expiry."""
    assert s.advice is not None
    e = replace(
        o.event,
        kind=EventKind.LEAVE_NOW,
        train=s.advice.advised_train,
        created_at=s.advice.sent_at,
    )
    return replace(o, event=e, occurrence=s.key, expires_at=s.advice.sent_at)


def _owing(st: PersistedState) -> PersistedState:
    """`st` with one occurrence whose advice was told by its record but whose LEAVE_NOW is owed,
    and two obligations sharing an id."""
    s = next(iter(st.occurrences.values()))
    o = _leave_now(st.obligations[0], s)
    twin = replace(o, event=replace(o.event, profile_id="other"), expires_at=None)
    return replace(st, occurrences={okey(s.key): s}, obligations=(o, twin))


def test_v1_file_with_no_delivered_reads_as_told(tmp_path):
    path = tmp_path / "state.json"
    _file(path, 1, full_state(), keep_delivered=False)
    assert JsonFileStore(path).load() == full_state()  # told: full_state's record is


def test_v1_file_is_brought_into_the_current_model(tmp_path):
    path = tmp_path / "state.json"
    _file(path, 1, _owing(full_state()), keep_delivered=False)
    got = JsonFileStore(path).load()
    a = next(iter(got.occurrences.values())).advice
    assert isinstance(a, AdviceRecord) and a.delivered is False  # its LEAVE_NOW is still owed
    assert [o.expires_at for o in got.obligations] == [None, None]
    assert len({o.id for o in got.obligations}) == 2


def test_current_file_is_read_as_written_and_a_v1_file_is_written_back_current(tmp_path):
    path = tmp_path / "state.json"
    st = _owing(full_state())
    _file(path, VERSION, st)
    assert JsonFileStore(path).load() == st
    _file(path, 1, st)
    store = JsonFileStore(path)
    store.save(store.load())
    assert json.loads(path.read_text())["version"] == VERSION


def test_current_file_without_delivered_is_kept_aside(tmp_path):
    path = tmp_path / "state.json"
    _file(path, VERSION, full_state(), keep_delivered=False)
    with pytest.raises(StorageError, match="broken-"):
        JsonFileStore(path).load()


@pytest.mark.parametrize(
    "text, reason",
    [
        ("[1]", "unknown version [1]"),
        ("null", "unknown version None"),
        ("1", "unknown version 1"),
        ('{"version": 3}', "unknown version 3"),
        ('{"write_counter": 1, "state": {}}', "unknown version None"),
    ],
)
def test_a_file_of_no_known_version_is_set_aside_saying_so(tmp_path, text, reason):
    path = tmp_path / "state.json"
    path.write_text(text)
    with pytest.raises(StorageError, match=re.escape(f"migrated ({reason})")):
        JsonFileStore(path).load()
    assert not path.exists()


def test_too_deep_or_out_of_range_store_kept_aside(tmp_path):
    raw = encode(full_state())
    raw["webhook_down"] = {"u": "0001-01-01T00:00:00+14:00"}
    near_edge = {**raw, "webhook_down": {"u": "9999-12-31T22:30:00+00:00"}}
    for text in (
        "[" * 100_000,
        json.dumps({"version": VERSION, "write_counter": 1, "state": raw}),
        json.dumps({"version": VERSION, "write_counter": 1, "state": near_edge}),
    ):
        path = tmp_path / "state.json"
        path.write_text(text)
        with pytest.raises(StorageError, match="broken-"):
            JsonFileStore(path).load()
        assert not path.exists()
