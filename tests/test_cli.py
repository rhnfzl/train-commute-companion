import json
from datetime import date, time
from pathlib import Path

import pytest

from train_commute_companion.cli import main
from train_commute_companion.lookahead import BaselineHistory, BaselineVersion
from train_commute_companion.model import Signature, SignatureLeg
from train_commute_companion.serde import encode
from train_commute_companion.store import VERSION, PersistedState

ROOT = Path(__file__).parent.parent
STARTER = str(ROOT / "examples" / "profiles.yaml")
BAD = str(ROOT / "tests" / "data" / "profiles" / "bad_starter.yaml")


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("NS_API_KEY", raising=False)


def stations(tmp: Path, *codes: str) -> list[str]:
    f = tmp / "stations.json"
    f.write_text(json.dumps({"payload": [{"code": c} for c in codes or ("AAA", "UT", "RTD")]}))
    return ["--stations-file", str(f)]


def test_check_profile_starter_file_clean(tmp_path, capsys):
    assert main(["check-profile", STARTER, *stations(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_check_profile_without_a_station_list_is_not_reported_valid(capsys):
    assert main(["check-profile", STARTER]) == 1
    assert "station codes not checked" in capsys.readouterr().out


def test_check_profile_names_every_error_and_suggests(capsys):
    assert main(["check-profile", BAD]) == 1
    out = capsys.readouterr().out
    assert "transfer_min" in out and "id" in out and f"{2:02d}:00" in out
    assert "(" in out  # a suggestion rides along


def test_check_profile_unreadable_file(capsys):
    assert main(["check-profile", "/nonexistent/profiles.yaml"]) == 1
    assert "cannot read" in capsys.readouterr().out


def test_check_profile_unknown_station_with_list(tmp_path, capsys):
    stations = tmp_path / "s.json"
    stations.write_text(json.dumps({"payload": [{"code": "AAA"}, {"code": "UT"}]}))
    assert main(["check-profile", STARTER, "--stations-file", str(stations)]) == 1
    assert "RTD" in capsys.readouterr().out


def store_with_move(path: Path, to: time) -> None:
    sig = Signature(
        (SignatureLeg("AAA", "UT", to, 0, 28), SignatureLeg("UT", "RTD", time(8, 50), 0, 34))
    )
    hist = BaselineHistory((BaselineVersion(1, sig, date(2026, 11, 3), True),))
    state = PersistedState(None, {}, {"p1|outbound": hist}, {}, {}, (), True)
    path.write_text(json.dumps({"version": VERSION, "write_counter": 1, "state": encode(state)}))


def test_check_profile_says_ns_moved_the_train(tmp_path, capsys):
    store = tmp_path / "state.json"
    store_with_move(store, time(8, 15))
    assert main(["check-profile", STARTER, "--store", str(store), *stations(tmp_path)]) == 0
    assert "NS moved your 08:12 to 08:15 from 2026-11-03; update your profile?" in (
        capsys.readouterr().out
    )


def test_check_profile_same_time_in_store_is_quiet(tmp_path, capsys):
    store = tmp_path / "state.json"
    store_with_move(store, time(8, 12))
    assert main(["check-profile", STARTER, "--store", str(store), *stations(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_check_profile_leaves_a_broken_store_alone(tmp_path, capsys):
    store = tmp_path / "state.json"
    store.write_text("not json")
    flags = stations(tmp_path)
    assert main(["check-profile", STARTER, "--store", str(store), *flags]) == 0
    assert store.read_text() == "not json" and sorted(tmp_path.iterdir()) == sorted(
        [store, Path(flags[1])]
    )
    assert "cannot read" in capsys.readouterr().out


def test_run_refuses_without_key(monkeypatch, capsys):
    monkeypatch.delenv("NS_API_KEY", raising=False)
    assert main(["run", STARTER]) == 1
    assert "NS_API_KEY" in capsys.readouterr().out


def test_run_notifier_attributes_every_event(capsys):
    import asyncio
    from datetime import UTC, datetime

    from train_commute_companion.cli import _StdoutNotifier
    from train_commute_companion.model import AlertEvent, EventKind, Severity

    ev = AlertEvent(
        "p",
        "Office A",
        None,
        None,
        None,
        EventKind.ALL_GOOD,
        Severity.SILENT,
        None,
        "fine",
        "k",
        datetime(2026, 10, 26, 7, 0, tzinfo=UTC),
    )
    assert asyncio.run(_StdoutNotifier().send(ev)) is True
    assert capsys.readouterr().out.rstrip().endswith("Data: NS")
