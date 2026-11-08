import json
from dataclasses import replace

import pytest
from lifecycle_fixtures import at, ctx, usual_locked

from train_commute_companion.lifecycle import queries_needed
from train_commute_companion.lookahead_probe import probe_queries
from train_commute_companion.model import AccessibilitySettings, Direction, Profile
from train_commute_companion.profile import parse_profiles
from train_commute_companion.serde import decode, dumps, encode, loads


def test_accessibility_profile_defaults_and_roundtrip():
    p = ctx(at("07:50")).profile
    assert not p.accessibility.require_accessible
    enabled = replace(p, accessibility=AccessibilitySettings(True))
    assert loads(Profile, dumps(enabled)) == enabled
    old = encode(p)
    old.pop("accessibility")
    assert decode(Profile, old).accessibility == AccessibilitySettings()


@pytest.mark.parametrize("value", ["yes", 1, None, {"other": True}, {"require_accessible": "true"}])
def test_accessibility_rejects_unsupported_settings(value):
    from lifecycle_fixtures import PROFILES

    text = PROFILES.replace("UT_MIN", "4").replace("TRANSFERS", "[UT]")
    text += "    accessibility: " + json.dumps(value) + "\n"
    profiles, errors = parse_profiles(text, None)
    assert not profiles and any("accessibility" in e.path for e in errors)


def test_all_main_recovery_and_lookahead_queries_carry_preference():
    c = ctx(at("07:50"))
    p = replace(c.profile, accessibility=AccessibilitySettings(True))
    c = replace(c, profile=p)
    queries = queries_needed(usual_locked(), c)
    queries += probe_queries(p, Direction.OUTBOUND, c.schedule.key.service_date, None)
    assert len(queries) >= 4
    assert all(q.require_accessible for q in queries)


def test_toggle_preserves_locked_route_search_preference():
    from train_commute_companion.model import Config
    from train_commute_companion.revisions import effective_profile, same_route

    old = ctx(at("07:50")).profile
    new = replace(old, accessibility=AccessibilitySettings(True))
    assert not same_route(old, new)
    effective = effective_profile({1: Config(1, (old,))}, Config(2, (new,)), usual_locked())
    assert effective is not None and not effective.accessibility.require_accessible


def test_newest_answer_clears_stale_boarding_metadata_and_view_exposes_it():
    from lifecycle_fixtures import L1, ans, conn, journey

    from train_commute_companion.classify import classify_leg, current_run
    from train_commute_companion.model import Observation
    from train_commute_companion.view import _leg_view

    board = replace(L1.board, accessibility_notes=("Book assistance",))
    end = replace(L1.alight, exit_side="right", accessibility_notes=("Alight with assistance",))
    old = replace(L1, stops=(board, end))
    progress = journey([old]).legs[0]
    view = _leg_view(progress, classify_leg(old, end.code), Observation.SEEN)
    assert view.boarding_notes == ("Book assistance",)
    assert view.alighting_notes == ("Alight with assistance",)
    assert view.exit_side == "right"
    run = current_run(
        [ans("AAA", conn(old), when="08:00"), ans("AAA", conn(L1), when="08:01")],
        L1.train,
        "AAA",
        end.code,
    )
    assert run is not None
    assert run[1].leg.board.accessibility_notes == ()
    assert run[1].leg.alight.exit_side is None


async def test_runtime_poll_and_explicit_choice_use_accessible_filter(tmp_path):
    from datetime import time

    from runtime_rig import PROFILE, Rig

    rig = Rig(tmp_path)
    rig.write(
        text=PROFILE.replace("NAME", "office_a").replace(
            '    name: "office_a"',
            '    name: "office_a"\n    accessibility: {require_accessible: true}',
        )
    )
    await rig.rt.start()
    await rig.rt.poll_once()
    await rig.rt.choose_at("p1", time(8, 12))
    assert rig.provider.calls
    assert all(q.require_accessible for q in rig.provider.calls)


def test_partial_new_answer_cannot_retain_old_alighting_guidance():
    from lifecycle_fixtures import L1, ans, conn

    from train_commute_companion.classify import current_run

    end = replace(L1.alight, exit_side="right", accessibility_notes=("Old assistance",))
    middle = replace(L1.alight, code="BBB")
    old = replace(L1, stops=(L1.board, middle, end))
    partial = replace(L1, stops=(L1.board, middle))
    run = current_run(
        [ans("AAA", conn(old), when="08:00"), ans("AAA", conn(partial), when="08:01")],
        L1.train,
        "AAA",
        end.code,
    )
    assert run is not None and run[1].leg.alight.code == end.code
    assert run[1].leg.alight.exit_side is None
    assert run[1].leg.alight.accessibility_notes == ()


def test_conflicting_current_exit_sides_are_unknown():
    from lifecycle_fixtures import L1, ans, conn

    from train_commute_companion.classify import current_run

    left = replace(L1, stops=(L1.board, replace(L1.alight, exit_side="left")))
    right = replace(L1, stops=(L1.board, replace(L1.alight, exit_side="right")))
    run = current_run([ans("AAA", conn(left)), ans("AAA", conn(right))], L1.train, "AAA", "UT")
    assert run is not None and run[1].leg.alight.exit_side is None


def test_old_stored_stop_has_unknown_boarding_data():
    from lifecycle_fixtures import L1

    from train_commute_companion.model import Stop

    raw = encode(L1.board)
    raw.pop("accessibility_notes")
    raw.pop("exit_side")
    old = decode(Stop, raw)
    assert old.accessibility_notes == () and old.exit_side is None
