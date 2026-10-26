from pathlib import Path

import pytest

from train_commute_companion.cli import main

REPLAYS = Path(__file__).parent / "replays"


def replay_output(path: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["replay", str(path)]) == 0
    return capsys.readouterr().out


def expected(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "direct",
        "one_transfer",
        "two_transfers",
        "transfer_broken_by_delay",
        "cancelled_second_leg",
        "two_profiles_different_days",
    ],
)
def test_owner_replay(name, capsys):
    out = replay_output(REPLAYS / f"{name}.yaml", capsys)
    assert out == expected(REPLAYS / f"{name}.out")
    assert out.rstrip().endswith("Data: NS")


def test_owner_replay_same_day_profiles_refused(capsys):
    assert "refused" in replay_output(REPLAYS / "two_profiles_same_day.yaml", capsys)


def test_replay_names_a_bad_file(tmp_path, capsys):
    f = tmp_path / "r.yaml"
    f.write_text('profile: p.yaml\nnow: "2026-10-27 07:50"\nuntil: 0950\n')
    assert main(["replay", str(f)]) == 1
    assert "until" in capsys.readouterr().out
