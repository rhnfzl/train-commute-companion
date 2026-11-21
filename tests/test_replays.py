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
        "two_profiles_same_day",
        "scripted_events",
    ],
)
def test_owner_replay(name, capsys):
    out = replay_output(REPLAYS / f"{name}.yaml", capsys)
    assert out == expected(REPLAYS / f"{name}.out")
    assert out.rstrip().endswith("Data: NS")


def test_arrive_by_replay_filters_late_arrivals_and_replaces_a_cancelled_train(tmp_path, capsys):
    (tmp_path / "profile.yaml").write_text(
        (REPLAYS / "profiles" / "direct.yaml")
        .read_text()
        .replace(
            '{first_train: "08:12", transfers: [], buffer_min: 10}',
            '{mode: window, window: ["08:00", "09:00"], pick: arrive_by, '
            'arrive_by: "09:00", walk_after_min: 10, buffer_min: 10}',
        )
    )
    first = (
        "trips:\n"
        '  - legs: [{from: AAA, to: RTD, dep: "08:12", arr: "08:43", number: "1111"}]\n'
        '  - legs: [{from: AAA, to: RTD, dep: "08:24", arr: "08:50", number: "2222"}]\n'
        '  - legs: [{from: AAA, to: RTD, dep: "08:29", arr: "08:53", number: "3333"}]\n'
    )
    (tmp_path / "first.yaml").write_text(first)
    (tmp_path / "failed.yaml").write_text(
        first.replace('number: "2222"', 'number: "2222", cancelled: true')
    )
    script = tmp_path / "replay.yaml"
    script.write_text(
        'profile: profile.yaml\nnow: "2026-10-27 07:50"\nuntil: "07:57"\nqueries:\n'
        '  - {at: "07:50", query: "AAA>RTD", answer_file: first.yaml}\n'
        '  - {at: "07:54", query: "AAA>RTD", answer_file: failed.yaml}\n'
    )
    output = replay_output(script, capsys)
    assert "08:24 AAA to RTD train is cancelled" in output
    assert "Take the earlier train at 08:12 from AAA" in output
    assert "08:29" not in output


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


@pytest.mark.parametrize(
    "event, why",
    [
        ('{at: "07:50", location: RTD, kind: teleport}', "location kind 'teleport'"),
        ('{at: "07:50", action: dance}', "unknown event"),
    ],
)
def test_a_bad_scripted_event_is_refused_without_a_traceback(tmp_path, capsys, event, why):
    f = tmp_path / "r.yaml"
    f.write_text(
        f'profile: {REPLAYS}/profiles/direct.yaml\nnow: "2026-10-27 07:50"\nuntil: "07:55"\n'
        f"events:\n  - {event}\n"
    )
    assert main(["replay", str(f)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("replay: ") and why in out
