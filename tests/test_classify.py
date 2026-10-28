from dataclasses import replace
from datetime import date, datetime, timedelta

from builders import AMS, _cut, make_connection, make_leg

from train_commute_companion.classify import (
    classify_leg,
    current_connection,
    current_run,
    find_leg,
    pair_transfer,
    sort_by_expected_departure,
)
from train_commute_companion.model import LegKind, Observation, Provenance, TrainId
from train_commute_companion.providers.base import ProviderAnswer, Query


def answer(*legs):
    q = Query("UT", "RTD", legs[0].train.planned_departure if legs else None)  # type: ignore[arg-type]
    return ProviderAnswer(q, tuple(make_connection([leg]) for leg in legs), q.at)


def train(board, hhmm, number):
    leg = make_leg(board, "RTD", hhmm, "23:00", number=number)
    return leg.train


def test_cut_short_is_never_cancelled():
    cl = classify_leg(make_leg("AAA", "RTD", "08:12", "09:00", stops=["UT"], cut_after="UT"), "RTD")
    assert cl.kind is LegKind.CUT_SHORT and cl.last_served is not None
    assert cl.last_served.code == "UT"


def test_cut_after_alight_station_is_ok():
    leg = make_leg("AAA", "RTD", "08:12", "09:00", stops=["UT"], cut_after="UT")
    assert classify_leg(leg, "UT").kind is LegKind.OK


def test_first_stop_cancelled_is_cancelled():
    leg = make_leg("AAA", "RTD", "08:12", "09:00", cancelled=True)
    cl = classify_leg(leg, "RTD")
    assert cl.kind is LegKind.CANCELLED and cl.last_served is None


def test_delay_3_min_is_delayed_2_is_ok():
    assert (
        classify_leg(make_leg("UT", "RTD", "08:46", "09:24", delay_dep=2), "RTD").kind is LegKind.OK
    )
    cl = classify_leg(make_leg("UT", "RTD", "08:46", "09:24", delay_dep=3), "RTD")
    assert cl.kind is LegKind.DELAYED and cl.dep_delay_min == 3


def test_arrival_delay_alone_delays():
    assert (
        classify_leg(make_leg("UT", "RTD", "08:46", "09:24", delay_arr=4), "RTD").kind
        is LegKind.DELAYED
    )


def test_two_trains_same_minute_is_uncertain():
    ans = answer(
        make_leg("UT", "RTD", "08:46", "09:24", number="1"),
        make_leg("UT", "RTD", "08:46", "09:30", number="2"),
    )
    assert find_leg(ans, train("UT", "08:46", "1"), "UT") == (None, Observation.UNCERTAIN)


def test_same_minute_other_number_is_uncertain():
    ans = answer(make_leg("UT", "RTD", "08:46", "09:24", number="2"))
    assert find_leg(ans, train("UT", "08:46", "1"), "UT") == (None, Observation.UNCERTAIN)


def test_exact_match_is_seen_even_when_listed_twice():
    leg = make_leg("UT", "RTD", "08:46", "09:24", number="1")
    found, obs = find_leg(answer(leg, leg), train("UT", "08:46", "1"), "UT")
    assert obs is Observation.SEEN and found == leg


def test_absent_is_missing():
    ans = answer(make_leg("UT", "RTD", "08:50", "09:24", number="1"))
    assert find_leg(ans, train("UT", "08:46", "1"), "UT") == (None, Observation.MISSING)


def test_empty_answer_is_missing():
    q = Query("UT", "RTD", train("UT", "08:46", "1").planned_departure)
    ans = ProviderAnswer(q, (), q.at)
    assert find_leg(ans, train("UT", "08:46", "1"), "UT") == (None, Observation.MISSING)


def test_other_board_station_does_not_match():
    ans = answer(make_leg("AAA", "RTD", "08:46", "09:24", number="1"))
    assert find_leg(ans, train("UT", "08:46", "1"), "UT")[1] is Observation.MISSING


def test_other_operating_date_is_uncertain():
    other = make_leg("UT", "RTD", "08:46", "09:24", number="1")
    want = TrainId(other.train.operating_date.replace(day=26), "1", other.train.planned_departure)
    assert find_leg(answer(other), want, "UT")[1] is Observation.UNCERTAIN


def test_cancelled_last_stop_with_reported_arrival_still_served():
    # rule 14: NS flags the final stop cancelled yet reports an actual arrival there
    leg = make_leg("AAA", "RTD", "08:12", "09:00", stops=["UT"], cut_after="UT")
    last = leg.stops[-1]
    reported = replace(last.arrival, provenance=Provenance.REPORTED, value=last.arrival.planned)
    leg = replace(leg, stops=(*leg.stops[:-1], replace(last, arrival=reported)))
    assert classify_leg(leg, "RTD").kind is LegKind.OK


def test_sort_by_expected_departure_uses_delay():
    late = make_connection([make_leg("UT", "RTD", "08:00", "09:00", delay_dep=20)])
    early = make_connection([make_leg("UT", "RTD", "08:10", "09:10")])
    assert sort_by_expected_departure([late, early]) == [early, late]


def test_train_not_calling_at_alight_station_is_cut_short_at_its_last_stop():
    cl = classify_leg(make_leg("AAA", "UT", "08:12", "08:40"), "RTD")
    assert cl.kind is LegKind.CUT_SHORT and cl.arr_delay_min is None
    assert cl.last_served is not None and cl.last_served.code == "UT"


def test_passed_boarding_or_alighting_stop_is_never_ok():
    leg = make_leg("AAA", "RTD", "08:12", "09:00", stops=["UT"])
    passes_board = replace(leg, stops=(replace(leg.stops[0], passing=True), *leg.stops[1:]))
    assert classify_leg(passes_board, "RTD").kind is LegKind.CANCELLED
    passes_alight = replace(
        leg, stops=(leg.stops[0], replace(leg.stops[1], passing=True), leg.stops[2])
    )
    assert classify_leg(passes_alight, "UT").kind is LegKind.CANCELLED
    longer = make_leg("AAA", "RTD", "08:12", "09:00", stops=["BBB", "UT"])
    passes = replace(
        longer, stops=(*longer.stops[:2], replace(longer.stops[2], passing=True), longer.stops[3])
    )
    cl = classify_leg(passes, "UT")
    assert cl.kind is LegKind.CUT_SHORT and cl.last_served is not None
    assert cl.last_served.code == "BBB"


def test_delay_across_the_repeated_hour_uses_instants():
    # planned at the first 02:50 (CEST), expected at the second 02:05 (CET): 15 min late
    leg = make_leg("AAA", "UT", "01:30", "02:50", day=date(2026, 10, 24))
    last = leg.stops[-1]
    late = replace(last.arrival, value=last.arrival.planned.replace(minute=5, fold=1))
    leg = replace(leg, stops=(leg.stops[0], replace(last, arrival=late)))
    cl = classify_leg(leg, "UT")
    assert cl.arr_delay_min == 15 and cl.kind is LegKind.DELAYED


def test_sort_across_the_repeated_hour_uses_instants():
    second = make_connection(
        [make_leg("UT", "RTD", "02:10", "03:00", day=date(2026, 10, 24), fold_dep=1)]
    )
    first = make_connection([make_leg("UT", "RTD", "02:40", "03:30", day=date(2026, 10, 24))])
    assert sort_by_expected_departure([second, first]) == [first, second]


# one poll's answers a minute apart; train 6666 boarding at UT, alight RTD (item A, 6.2)
OLDER, NEWER = "09:04:40", "09:05:10"


def fetched(hms: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-27 {hms}").replace(tzinfo=AMS)


def at_(hms: str, *conns):
    q = Query("UT", "RTD", fetched(hms))
    return ProviderAnswer(q, tuple(conns), fetched(hms))


def r6666(alight="RTD", arr="09:24", **k):
    return make_leg("UT", alight, "08:50", arr, number="6666", **k)


def run_of(*answers, alight="RTD"):
    run = current_run(answers, r6666().train, "UT", alight)
    assert run is not None
    return run


def one(leg):
    return make_connection([leg])


def test_current_run_newest_cancelled_beats_older_running():
    seen, cl = run_of(at_(OLDER, one(r6666())), at_(NEWER, one(r6666(cancelled=True))))
    assert cl.kind is LegKind.CANCELLED and seen == fetched(NEWER)


def test_current_run_newest_explicit_cut_short_wins():
    cut = r6666(stops=["BBB"], cut_after="BBB")  # RTD listed and cancelled
    seen, cl = run_of(at_(OLDER, one(r6666(stops=["BBB"]))), at_(NEWER, one(cut)))
    assert cl.kind is LegKind.CUT_SHORT and seen == fetched(NEWER)


def test_current_run_truncated_newer_copy_keeps_the_full_run():
    short = r6666("BBB", "09:05")  # a connection that changes at BBB: nothing cancelled
    seen, cl = run_of(at_(OLDER, one(r6666(stops=["BBB"]))), at_(NEWER, one(short)))
    assert cl.kind is LegKind.OK and cl.leg.alight.code == "RTD" and seen == fetched(OLDER)


def test_current_run_newer_running_beats_older_cancelled():
    seen, cl = run_of(at_(OLDER, one(r6666(cancelled=True))), at_(NEWER, one(r6666())))
    assert cl.kind is LegKind.OK and seen == fetched(NEWER)


def test_current_run_passing_stop_is_no_evidence():
    short = r6666("BBB", "09:05", stops=["XA"])
    short = replace(
        short, stops=(short.stops[0], replace(short.stops[1], passing=True), short.stops[2])
    )
    seen, cl = run_of(at_(OLDER, one(r6666(stops=["XA", "BBB"]))), at_(NEWER, one(short)))
    assert cl.kind is LegKind.OK and seen == fetched(OLDER)


def test_current_run_ties_go_to_the_newest_answer():
    planned = r6666(provenance=Provenance.PLANNED)
    seen, cl = run_of(at_(OLDER, one(r6666())), at_(NEWER, one(planned)))
    assert cl.leg.board.departure.provenance is Provenance.PLANNED and seen == fetched(NEWER)


def test_current_run_more_stops_never_beat_newer_timing():
    longer = r6666(stops=["XA", "XB", "XC"])
    planned = r6666(provenance=Provenance.PLANNED)
    seen, cl = run_of(at_(OLDER, one(longer)), at_(NEWER, one(planned)))
    assert cl.leg.board.departure.provenance is Provenance.PLANNED and seen == fetched(NEWER)


def test_current_run_truncated_newer_copy_gives_the_departure():
    short = r6666("BBB", "09:05", provenance=Provenance.PLANNED)
    seen, cl = run_of(at_(OLDER, one(r6666(stops=["BBB"]))), at_(NEWER, one(short)))
    assert cl.kind is LegKind.OK and cl.leg.alight.code == "RTD" and seen == fetched(OLDER)
    assert cl.leg.board.departure.provenance is Provenance.PLANNED


def test_current_run_spliced_departure_keeps_the_arrival_time():
    short = r6666("BBB", "09:05", delay_dep=2)
    seen, cl = run_of(at_(OLDER, one(r6666(stops=["BBB"]))), at_(NEWER, one(short)))
    assert seen == fetched(OLDER) and cl.dep_delay_min == 2


R1300 = make_leg("AAA", "UT", "08:25", "08:40", number="1300")


def test_current_connection_keeps_transfer_fields():
    c = make_connection([replace(R1300, transfer_time_to_next=7, change_possible=True), r6666()])
    cur = current_connection([at_(NEWER, one(r6666(delay_dep=4)))], c)
    assert cur.legs[1].board.departure.value == r6666(delay_dep=4).board.departure.value
    assert (cur.legs[0].transfer_time_to_next, cur.legs[0].change_possible) == (7, True)


def test_current_connection_takes_the_newest_copy_of_a_transfer():
    old_in = replace(R1300, change_possible=True)
    old_out = replace(r6666(), transfer_message_min=3)
    c = make_connection([old_in, old_out])
    new = make_connection(
        [
            replace(R1300, change_possible=False),
            replace(r6666(), transfer_message_min=8, reachable=False),
        ]
    )
    cur = current_connection([at_(OLDER, c), at_(NEWER, new)], c)
    assert cur.legs[0].change_possible is False and cur.legs[0].reachable is None
    assert (cur.legs[1].transfer_message_min, cur.legs[1].reachable) == (8, False)


def test_current_connection_keeps_a_distinct_transfer():
    out = make_leg("BBB", "RTD", "09:15", "09:40", number="1300")
    c = make_connection([replace(r6666("BBB", "09:05"), transfer_time_to_next=4), out])
    other = make_leg("BBB", "RTD", "09:15", "09:40", number="1234")
    newer = make_connection([replace(r6666("BBB", "09:05"), transfer_time_to_next=9), other])
    assert current_connection([at_(NEWER, newer)], c).legs[0].transfer_time_to_next == 4


def test_pair_transfer_ignores_another_connections_change():
    runs_on = replace(
        make_leg("AAA", "RTD", "08:25", "09:24", number="1300", stops=["UT"]), change_possible=True
    )
    change = make_connection([replace(R1300, change_possible=False), r6666()])
    inc, out = classify_leg(runs_on, "UT"), classify_leg(r6666(), "RTD")
    a, _ = pair_transfer([at_(NEWER, one(runs_on), change)], inc, out)
    assert a.leg.change_possible is False
    a, b = pair_transfer([at_(NEWER, one(runs_on))], inc, out)
    assert a.leg.change_possible is None and b.leg.reachable is None


def test_is_good_judges_a_cut_short_whose_copy_omits_the_alight():
    from test_board import kw

    from train_commute_companion.board import is_good
    from train_commute_companion.lifecycle import _pool

    raw = one(r6666())
    short = r6666("BBB", "09:05", stops=["XA"])
    short = replace(short, stops=(short.stops[0], _cut(short.stops[1]), short.stops[2]))
    pool = _pool([at_(OLDER, raw), at_(NEWER, one(short))])
    g = is_good(raw, pool, **kw())
    assert not g.ok and g.reason == "leg cut_short"


# conflicting copies at the newest fetch never read as good (7.3, 7.12: no missed alarm)
def test_current_run_conflicting_copies_at_one_fetch_read_cancelled(caplog):
    gone, runs = one(r6666(cancelled=True)), one(r6666())
    for order in ((runs, gone), (gone, runs)):
        caplog.clear()
        seen, cl = run_of(at_(OLDER, one(r6666())), at_(NEWER, *order))
        assert cl.kind is LegKind.CANCELLED and seen == fetched(NEWER)
        assert "conflicting copies" in caplog.text


def test_pair_transfer_conflicting_changes_at_one_fetch_take_the_worst(caplog):
    inc, out = classify_leg(R1300, "UT"), classify_leg(r6666(), "RTD")
    yes = make_connection([replace(R1300, change_possible=True, transfer_time_to_next=3), r6666()])
    no = make_connection([replace(R1300, change_possible=False, transfer_time_to_next=6), r6666()])
    for order in ((yes, no), (no, yes)):
        a, _ = pair_transfer([at_(NEWER, *order)], inc, out)
        assert (a.leg.change_possible, a.leg.transfer_time_to_next) == (False, 6)
    assert "conflicting copies of the change" in caplog.text


def test_find_leg_judges_identity_over_every_answer_of_the_pool():
    from train_commute_companion.lifecycle import _Pool

    c = make_connection([R1300, r6666()])
    origin = ProviderAnswer(Query("AAA", "RTD", fetched(NEWER)), (c,), fetched(NEWER))
    other = make_leg("UT", "RTD", "08:50", "09:24", number="1234")  # same onward minute
    transfer = at_(NEWER, one(other))
    # the runtime's origin pool: candidates from the origin, every answer as evidence
    pool = _Pool(origin.query, (c,), fetched(NEWER), (origin, transfer))
    assert find_leg(pool, r6666().train, "UT") == (None, Observation.UNCERTAIN)
    assert find_leg(origin, r6666().train, "UT")[1] is Observation.SEEN


# transport is read from the newest fetch, apart from which copy wins on stops (6.3)
def bus(leg):
    return replace(leg, alternative_transport=True, category="BUS")


def test_a_truncated_newer_bus_copy_keeps_its_transport():
    full = r6666(stops=["XA"])
    short = bus(r6666("XA", "09:05"))  # ends where its own connection changes
    seen, cl = run_of(at_(OLDER, one(full)), at_(NEWER, one(short)))
    assert seen == fetched(OLDER) and cl.leg.alight.code == "RTD"  # stops from the full copy
    assert cl.leg.alternative_transport and cl.leg.category == "BUS"


def test_tied_train_and_bus_copies_read_as_bus_in_either_order(caplog):
    for order in ((one(r6666()), one(bus(r6666()))), (one(bus(r6666())), one(r6666()))):
        caplog.clear()
        _, cl = run_of(at_(NEWER, *order))
        assert cl.leg.alternative_transport
        assert "conflicting transport" in caplog.text


def test_current_run_same_fetch_timings_take_the_worst_in_any_order():
    # 7.3, 7.12: copies at one fetch that disagree on times never depend on their order
    early, late = r6666(delay_dep=0), r6666(delay_dep=2, delay_arr=5)
    for order in ((one(early), one(late)), (one(late), one(early))):
        _, cl = run_of(at_(NEWER, *order))
        assert cl.arr_delay_min == 5  # the later arrival
        assert cl.dep_delay_min == 0  # the earlier departure
    planned = r6666(provenance=Provenance.PLANNED)
    for order in ((one(early), one(planned)), (one(planned), one(early))):
        _, cl = run_of(at_(NEWER, *order))
        assert cl.leg.board.departure.provenance is Provenance.PLANNED
        assert cl.leg.alight.arrival.provenance is Provenance.PLANNED


# every judgement at one fetch is the worst of its copies, in any order (7.3, 7.12)
def both(x, y):
    return ((one(x), one(y)), (one(y), one(x)))


def test_a_truncated_copy_still_gives_the_earliest_departure():
    full, short = r6666(stops=["BBB"], delay_dep=5), r6666("BBB", "09:05")
    for order in both(full, short):
        _, cl = run_of(at_(NEWER, *order))
        assert cl.kind is LegKind.OK
        assert cl.dep_delay_min == 0 and cl.leg.alight.code == "RTD"


def test_cut_short_copies_take_the_earliest_cut():
    far = r6666(stops=["XA", "XB"], cut_after="XB")
    near = r6666(stops=["XA", "XB"], cut_after="XA")
    for order in both(far, near):
        _, cl = run_of(at_(NEWER, *order))
        assert cl.kind is LegKind.CUT_SHORT and cl.last_served.code == "XA"
    for order in both(far, r6666(cancelled=True)):
        _, cl = run_of(at_(NEWER, *order))
        assert cl.kind is LegKind.CANCELLED


def test_equal_time_copies_keep_a_changed_platform():
    def on(track):
        leg = r6666()
        board = replace(leg.stops[0], planned_track="5", actual_track=track)
        return replace(leg, stops=(board, *leg.stops[1:]))

    for order in both(on("5"), on("7")):
        _, cl = run_of(at_(NEWER, *order))
        assert (cl.leg.board.planned_track, cl.leg.board.actual_track) == ("5", "7")
    for order in both(on(None), on("7")):
        assert run_of(at_(NEWER, *order))[1].leg.board.actual_track == "7"


def test_equal_time_copies_keep_every_message():
    a, b = replace(r6666(), messages=("works A",)), replace(r6666(), messages=("works B",))
    for order in both(a, b):
        assert run_of(at_(NEWER, *order))[1].leg.messages == ("works A", "works B")
    for order in both(r6666(), a):
        assert run_of(at_(NEWER, *order))[1].leg.messages == ("works A",)
    cut = replace(r6666(stops=["XA"], cut_after="XA"), messages=("works B",))
    for order in both(cut, a):
        assert run_of(at_(NEWER, *order))[1].leg.messages == ("works A", "works B")


def test_cut_short_copies_take_the_latest_arrival_at_the_cut():  # 7.12: a missed change costs more
    def cut(late):
        leg = r6666(stops=["XA", "XB"], cut_after="XA")
        xa = leg.stops[1]
        arr = replace(xa.arrival, value=xa.arrival.planned + timedelta(minutes=late))
        return replace(leg, stops=(leg.stops[0], replace(xa, arrival=arr), *leg.stops[2:]))

    for order in both(cut(0), cut(5)):
        _, cl = run_of(at_(NEWER, *order))
        assert cl.kind is LegKind.CUT_SHORT and cl.last_served.code == "XA"
        assert cl.last_served.arrival == cut(5).stops[1].arrival
    early = r6666(stops=["XA", "XB"], cut_after="XA")
    late = r6666(stops=["XA", "XB"], cut_after="XA", delay_dep=5)
    for order in both(early, late):
        _, cl = run_of(at_(NEWER, *order))
        assert cl.dep_delay_min == 0 and cl.last_served.arrival == late.stops[1].arrival


def at_xa(leg, late=0, plan=0, provenance=Provenance.REPORTED):
    """`leg` with its first stop after boarding planned `plan` min later, reported `late` after."""
    xa = leg.stops[1]
    planned = xa.arrival.planned + timedelta(minutes=plan)
    arr = replace(
        xa.arrival, planned=planned, value=planned + timedelta(minutes=late), provenance=provenance
    )
    xa = replace(xa, planned_arrival=planned, arrival=arr)
    return replace(leg, stops=(leg.stops[0], xa, *leg.stops[2:]))


def test_copies_take_the_worst_arrival_at_every_stop():  # 7.12: a frozen change stop too
    running, cut = r6666(stops=["XA"]), r6666(stops=["XA", "XB"], cut_after="XB")
    for leg, kind in ((running, LegKind.OK), (cut, LegKind.CUT_SHORT)):
        late = at_xa(leg, late=8)
        for order in both(at_xa(leg), late):
            _, cl = run_of(at_(NEWER, *order))
            assert cl.kind is kind and cl.leg.stops[1].arrival == late.stops[1].arrival
        for order in both(at_xa(leg), at_xa(leg, provenance=Provenance.PLANNED)):
            assert run_of(at_(NEWER, *order))[1].leg.stops[1].arrival.provenance is (
                Provenance.PLANNED
            )


def test_copies_keep_the_latest_planned_arrival():  # 7.10: a short planned change is kept
    planned_late, reported_late = (
        at_xa(r6666(stops=["XA"]), late=-3, plan=3),
        at_xa(r6666(stops=["XA"]), late=1),
    )
    for order in both(planned_late, reported_late):
        xa = run_of(at_(NEWER, *order))[1].leg.stops[1]
        assert xa.planned_arrival == planned_late.stops[1].planned_arrival
        assert xa.arrival == reported_late.stops[1].arrival
