from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from builders import AMS, make_connection, make_leg

from train_commute_companion.board import (
    Catchability,
    catch_from,
    choose_replacement,
    is_good,
    max_transfers,
)
from train_commute_companion.classify import classify_leg
from train_commute_companion.model import Observation, Provenance
from train_commute_companion.providers.base import ProviderAnswer, Query


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=AMS)


def one_leg(a, b, dep, arr, **k):
    return make_connection([make_leg(a, b, dep, arr, **k)])


def arrives(station, hhmm, obs=Observation.SEEN, **k):
    leg = make_leg("AAA", station, "08:12", hhmm, **k)
    return replace(classify_leg(leg, station), observation=obs)


def kw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "board_station": "UT",
        "destination": "RTD",
        "catch": Catchability(dt("2026-10-27 08:30")),
        "kept_incoming": None,
        "done_transfers": 0,
        "max_transfers": 2,
        "profile_min": {"UT": 4},
    }
    return {**base, **over}


def answer(*conns):
    q = Query("UT", "RTD", dt("2026-10-27 08:30"))
    return ProviderAnswer(q, tuple(conns), q.at)


def test_suffix_2_min_after_kept_leg_with_4_min_minimum_fails():
    g = is_good(
        one_leg("UT", "RTD", "08:42", "09:20"),
        **kw(kept_incoming=arrives("UT", "08:40")),
    )
    assert not g.ok and "transfer" in g.reason


def test_good_after_kept_leg_with_enough_margin():
    c = one_leg("UT", "RTD", "08:50", "09:20")
    assert is_good(c, **kw(kept_incoming=arrives("UT", "08:40"))).ok


def test_uncertain_or_missing_leg_fails():
    c = one_leg("UT", "RTD", "08:50", "09:20")
    for obs in (Observation.UNCERTAIN, Observation.MISSING):
        assert not is_good(c, **kw(kept_incoming=arrives("UT", "08:40", obs=obs))).ok
    unseen = make_connection(
        [
            make_leg("UT", "XXX", "08:50", "09:00", provenance=Provenance.MISSING),
            make_leg("XXX", "RTD", "09:10", "09:30"),
        ]
    )
    assert not is_good(unseen, **kw(profile_min={"XXX": 4})).ok
    cancelled = one_leg("UT", "RTD", "08:50", "09:20", cancelled=True)
    assert not is_good(cancelled, **kw()).ok


def test_over_cap_fails():
    c = make_connection(
        [make_leg("UT", "XXX", "08:50", "09:00"), make_leg("XXX", "RTD", "09:10", "09:30")]
    )
    assert is_good(c, **kw(profile_min={"XXX": 4}, max_transfers=1)).ok
    g = is_good(c, **kw(profile_min={"XXX": 4}, max_transfers=0))
    assert not g.ok and "cap" in g.reason
    g = is_good(c, **kw(profile_min={"XXX": 4}, done_transfers=1, max_transfers=1))
    assert not g.ok and "cap" in g.reason
    assert max_transfers(("UT",)) == 2


def test_buffer_catchability_before_tap_and_after_station_report():
    now = dt("2026-10-27 08:00")
    assert catch_from(now, 10, None, False).not_before == dt("2026-10-27 08:10")
    tap = now + timedelta(minutes=5)
    assert catch_from(now, 10, tap, False).not_before == dt("2026-10-27 08:15")
    assert catch_from(now, 10, now - timedelta(minutes=30), False).not_before == now
    assert catch_from(now, 10, tap, True).not_before == now
    c = one_leg("UT", "RTD", "08:05", "09:00")
    assert not is_good(c, **kw(catch=catch_from(now, 10, None, False))).ok
    assert is_good(c, **kw(catch=catch_from(now, 10, None, True))).ok


def test_replacement_prefers_usual_pattern_then_new_route_then_none():
    direct_late = one_leg("UT", "RTD", "09:00", "09:40")
    detour_early = make_connection(
        [make_leg("UT", "XXX", "08:40", "08:50"), make_leg("XXX", "RTD", "09:00", "09:20")]
    )
    k = kw(profile_min={"XXX": 4}, max_transfers=2)
    c, why = choose_replacement(answer(detour_early, direct_late), usual_remaining=(), **k)
    assert (c, why) == (direct_late, "usual_pattern")
    c, why = choose_replacement(answer(detour_early), usual_remaining=(), **k)
    assert (c, why) == (detour_early, "new_route")
    c, why = choose_replacement(answer(), usual_remaining=(), **k)
    assert (c, why) == (None, "none")
    c, why = choose_replacement(
        answer(detour_early), usual_remaining=(), **{**k, "max_transfers": 0}
    )
    assert why == "none"


def test_cancelled_second_leg_relocks_suffix_from_transfer_station():
    kept = arrives("UT", "08:40")
    cancelled = one_leg("UT", "RTD", "08:46", "09:24", cancelled=True)
    next_ok = one_leg("UT", "RTD", "08:56", "09:34", number="2222")
    c, why = choose_replacement(
        answer(cancelled, next_ok), usual_remaining=(), **kw(kept_incoming=kept)
    )
    assert (c, why) == (next_ok, "usual_pattern")
