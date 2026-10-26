import os
from dataclasses import replace

import pytest
from test_lifecycle_follow import L1, L2, L2X, LATER, ans, at, conn, ctx, journey, occ, poll
from test_location import USUAL, event, locate
from test_serde import full_state

from train_commute_companion.store import JsonFileStore, StorageError


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
        '{"version": 2, "write_counter": 1, "state": {}}',
        '{"version": 1, "write_counter": 1, "state": {"x": 1}}',
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
