from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
    c = one_leg("UT", "RTD", "08:42", "09:20")
    g = is_good(c, answer(c), **kw(kept_incoming=arrives("UT", "08:40")))
    assert not g.ok and "transfer" in g.reason


def test_good_after_kept_leg_with_enough_margin():
    c = one_leg("UT", "RTD", "08:50", "09:20")
    assert is_good(c, answer(c), **kw(kept_incoming=arrives("UT", "08:40"))).ok


def test_uncertain_or_missing_leg_fails():
    c = one_leg("UT", "RTD", "08:50", "09:20")
    for obs in (Observation.UNCERTAIN, Observation.MISSING):
        assert not is_good(c, answer(c), **kw(kept_incoming=arrives("UT", "08:40", obs=obs))).ok
    unseen = make_connection(
        [
            make_leg("UT", "XXX", "08:50", "09:00", provenance=Provenance.MISSING),
            make_leg("XXX", "RTD", "09:10", "09:30"),
        ]
    )
    assert not is_good(unseen, answer(unseen), **kw(profile_min={"XXX": 4})).ok
    cancelled = one_leg("UT", "RTD", "08:50", "09:20", cancelled=True)
    assert not is_good(cancelled, answer(cancelled), **kw()).ok


def test_over_cap_fails():
    c = make_connection(
        [make_leg("UT", "XXX", "08:50", "09:00"), make_leg("XXX", "RTD", "09:10", "09:30")]
    )
    assert is_good(c, answer(c), **kw(profile_min={"XXX": 4}, max_transfers=1)).ok
    g = is_good(c, answer(c), **kw(profile_min={"XXX": 4}, max_transfers=0))
    assert not g.ok and "cap" in g.reason
    g = is_good(c, answer(c), **kw(profile_min={"XXX": 4}, done_transfers=1, max_transfers=1))
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
    assert not is_good(c, answer(c), **kw(catch=catch_from(now, 10, None, False))).ok
    assert is_good(c, answer(c), **kw(catch=catch_from(now, 10, None, True))).ok


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


def test_cut_short_candidate_fails_is_good():
    cut = one_leg("UT", "RTD", "08:50", "09:20", stops=["XXX"], cut_after="XXX")
    g = is_good(cut, answer(cut), **kw())
    assert not g.ok and "cut_short" in g.reason.lower().replace(" ", "_")


def planned_at(leg, index, field):
    s = leg.stops[index]
    o = getattr(s, field)
    stop = replace(s, **{field: replace(o, value=None, provenance=Provenance.PLANNED)})
    stops = list(leg.stops)
    stops[index] = stop
    return replace(leg, stops=tuple(stops))


def test_two_trains_sharing_the_first_boarding_minute_are_never_chosen():
    a = one_leg("UT", "RTD", "08:50", "09:20", number="1111")
    b = one_leg("UT", "RTD", "08:50", "09:30", number="2222")
    g = is_good(a, answer(a, b), **kw())
    assert not g.ok and "uncertain" in g.reason
    assert choose_replacement(answer(a, b), usual_remaining=(), **kw()) == (None, "none")


def test_two_trains_sharing_a_later_boarding_minute_are_never_chosen():
    first = make_leg("UT", "XXX", "08:40", "08:50", number="1111")
    a = make_connection([first, make_leg("XXX", "RTD", "09:00", "09:20", number="3000")])
    b = make_connection([first, make_leg("XXX", "RTD", "09:00", "09:25", number="3001")])
    k = kw(profile_min={"XXX": 4})
    assert "uncertain" in is_good(a, answer(a, b), **k).reason
    assert choose_replacement(answer(a, b), usual_remaining=("XXX",), **k) == (None, "none")


def test_planned_or_missing_endpoint_times_fail_direct_and_multi_leg():
    for prov in (Provenance.PLANNED, Provenance.MISSING):
        c = one_leg("UT", "RTD", "08:50", "09:20", provenance=prov)
        assert not is_good(c, answer(c), **kw()).ok
    direct = make_leg("UT", "RTD", "08:50", "09:20")
    for leg in (planned_at(direct, 0, "departure"), planned_at(direct, -1, "arrival")):
        c = make_connection([leg])
        assert not is_good(c, answer(c), **kw()).ok
    one, two = make_leg("UT", "XXX", "08:40", "08:50"), make_leg("XXX", "RTD", "09:00", "09:20")
    k = kw(profile_min={"XXX": 4})
    for legs in ((planned_at(one, 0, "departure"), two), (one, planned_at(two, -1, "arrival"))):
        c = make_connection(list(legs))
        g = is_good(c, answer(c), **k)
        assert not g.ok and "planned or missing" in g.reason


def test_legs_that_do_not_meet_at_the_transfer_station_fail():
    c = make_connection(
        [make_leg("UT", "XXX", "08:40", "08:50"), make_leg("BBB", "RTD", "09:00", "09:20")]
    )
    g = is_good(c, answer(c), **kw(profile_min={"XXX": 4, "BBB": 4}))
    assert not g.ok and "transfer" in g.reason


def test_recovery_from_the_stop_a_kept_leg_was_cut_short_at_is_good():
    cut_at_ut = classify_leg(
        make_leg("AAA", "RTD", "08:12", "09:00", stops=["UT"], cut_after="UT"), "RTD"
    )
    c = one_leg("UT", "RTD", "08:50", "09:30", number="2222")
    assert is_good(c, answer(c), **kw(kept_incoming=cut_at_ut)).ok
    cut_before = classify_leg(
        make_leg("AAA", "RTD", "08:12", "09:00", stops=["BBB", "UT"], cut_after="BBB"), "RTD"
    )
    assert not is_good(c, answer(c), **kw(kept_incoming=cut_before)).ok


def test_catchability_across_the_repeated_hour_adds_real_minutes():
    now = datetime(2026, 10, 25, 2, 55, tzinfo=AMS)  # first 02:55, CEST
    got = catch_from(now, 10, None, False).not_before
    assert got.astimezone(UTC) == datetime(2026, 10, 25, 1, 5, tzinfo=UTC)


# one poll's answers a minute apart: the current copy of each train decides (item A, 6.2)
def at_answer(hhmm, *conns):
    q = Query("UT", "RTD", dt(f"2026-10-27 {hhmm}"))
    return ProviderAnswer(q, tuple(conns), q.at)


def polled(*conns_by_answer):
    from train_commute_companion.lifecycle import _pool

    return _pool([at_answer(f"08:0{i}", *cs) for i, cs in enumerate(conns_by_answer)])


V6666 = one_leg("UT", "RTD", "08:50", "09:20", number="6666")
V1300 = one_leg("UT", "RTD", "09:05", "09:35", number="1300")


def test_train_a_newer_answer_cancelled_is_never_good():
    gone = one_leg("UT", "RTD", "08:50", "09:20", number="6666", cancelled=True)
    pool = polled([V6666, V1300], [gone, V1300])
    g = is_good(V6666, pool, **kw())
    assert not g.ok and g.reason == "leg cancelled"
    c, how = choose_replacement(pool, usual_remaining=(), **kw())
    assert c is not None and c.legs[0].train.train_number == "1300" and how == "usual_pattern"
    assert choose_replacement(polled([V6666], [gone]), usual_remaining=(), **kw()) == (None, "none")


def two_legs(**late):
    return make_connection(
        [
            make_leg("AAA", "UT", "08:12", "08:40", number="1300", **late),
            make_leg("UT", "RTD", "08:46", "09:20", number="6666"),
        ]
    )


def test_transfer_is_judged_on_the_newer_arrival():
    older, newer = two_legs(), two_legs(delay_arr=5)  # 6 min to spare, then 1 against 4
    early = kw(board_station="AAA", catch=Catchability(dt("2026-10-27 08:00")))
    assert is_good(older, polled([older]), **early).ok
    g = is_good(older, polled([older], [newer]), **early)
    assert not g.ok and g.reason.startswith("transfer at UT broken")


def test_newer_planned_only_time_is_never_good():
    planned = one_leg("UT", "RTD", "08:50", "09:20", number="6666", provenance=Provenance.PLANNED)
    g = is_good(V6666, polled([V6666], [planned]), **kw())
    assert not g.ok and g.reason == "leg has a planned or missing time"


def test_choose_replacement_returns_the_current_copy():
    late = one_leg("UT", "RTD", "08:50", "09:20", number="6666", delay_dep=3)
    c, _ = choose_replacement(polled([V6666], [late]), usual_remaining=(), **kw())
    assert c is not None and c.legs[0].board.departure.value == dt("2026-10-27 08:53")


def test_choose_replacement_ranks_by_current_departure():
    x = one_leg("UT", "RTD", "08:20", "09:00", number="1300")
    y = one_leg("UT", "RTD", "08:25", "09:05", number="6666")
    x_late = one_leg("UT", "RTD", "08:20", "09:00", number="1300", delay_dep=22)
    early = kw(catch=Catchability(dt("2026-10-27 08:00")))
    c, _ = choose_replacement(polled([x, y], [x_late, y]), usual_remaining=(), **early)
    assert c is not None and c.legs[0].train.train_number == "6666"


def test_kept_pair_uses_its_own_change():
    runs_on = replace(
        make_leg("AAA", "RTD", "08:12", "09:24", number="1300", stops=["UT"]), change_possible=True
    )
    kept = classify_leg(runs_on, "UT")
    into = make_leg("AAA", "UT", "08:12", "08:40", number="1300")
    pool = polled([make_connection([runs_on]), make_connection([into, V6666.legs[0]])])
    assert is_good(V6666, pool, **kw(kept_incoming=kept)).ok
    no_change = make_connection([replace(into, change_possible=False), V6666.legs[0]])
    pool = polled([make_connection([runs_on]), no_change])
    g = is_good(V6666, pool, **kw(kept_incoming=kept))
    assert not g.ok and "change not possible" in g.reason


def test_conflicting_changes_at_one_fetch_are_never_good():
    yes, no = two_legs(), two_legs()
    yes = replace(yes, legs=(replace(yes.legs[0], change_possible=True), yes.legs[1]))
    no = replace(no, legs=(replace(no.legs[0], change_possible=False), no.legs[1]))
    early = kw(board_station="AAA", catch=Catchability(dt("2026-10-27 08:00")))
    assert is_good(yes, polled([yes]), **early).ok
    for order in ([yes, no], [no, yes]):
        g = is_good(yes, polled(order), **early)
        assert not g.ok and "change not possible" in g.reason


def test_replacement_transport_in_the_newer_copy_is_never_good():
    bus = replace(V6666, legs=(replace(V6666.legs[0], alternative_transport=True),))
    pool = polled([V6666], [bus])
    g = is_good(V6666, pool, **kw())
    assert not g.ok and g.reason == "not train only"
    assert choose_replacement(pool, usual_remaining=(), **kw()) == (None, "none")


def test_identity_is_judged_over_transfer_answers_too():
    from train_commute_companion.lifecycle import _Pool

    c = two_legs()
    early = kw(board_station="AAA", catch=Catchability(dt("2026-10-27 08:00")))
    origin = at_answer("08:01", c)
    other = one_leg("UT", "RTD", "08:46", "09:24", number="1234")  # same onward minute at UT
    pool = _Pool(origin.query, (c,), origin.fetched_at, (origin, at_answer("08:01", other)))
    assert is_good(c, origin, **early).ok
    g = is_good(c, pool, **early)
    assert not g.ok and g.reason == "leg uncertain"
