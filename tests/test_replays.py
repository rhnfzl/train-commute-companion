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


HEAD = 'profile: p.yaml\nnow: "2026-10-27 07:50"\nuntil: "09:50"\n'


@pytest.mark.parametrize(
    "spec, answer",
    [
        (HEAD + 'queries:\n  - {at: "08:00", query: q}\n', ""),  # no answer_file
        (HEAD + "queries: [1]\n", ""),
        (HEAD + "queries: 5\n", ""),
        (HEAD + "events: [1]\n", ""),
        ("profile: p.yaml\nnow: !!int abc\n", ""),
        (HEAD + 'queries:\n  - {at: "08:00", query: q, answer_file: a.yaml}\n', "trips: [1]\n"),
        (
            HEAD + 'queries:\n  - {at: "08:00", query: q, answer_file: a.yaml}\n',
            "trips: [{legs: [{from: AAA}]}]\n",
        ),
        (
            HEAD + 'queries:\n  - {at: "08:00", query: q, answer_file: a.yaml}\n',
            "trips: [{day: 99999999999, legs: []}]\n",
        ),
        ("[" * 5000, ""),
        ("- " * 3000 + "a", ""),
        (HEAD + 'queries:\n  - {at: "08:00", query: q, answer_file: a.yaml}\n', "[" * 5000),
    ],
)
def test_a_malformed_replay_file_is_refused_without_a_traceback(tmp_path, capsys, spec, answer):
    (tmp_path / "a.yaml").write_text(answer)
    f = tmp_path / "r.yaml"
    f.write_text(spec)
    assert main(["replay", str(f)]) == 1
    assert capsys.readouterr().out.startswith("replay: ")
