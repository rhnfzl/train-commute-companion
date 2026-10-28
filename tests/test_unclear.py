from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from builders import AMS, _on_track, make_connection, make_leg, planned_at_alight

from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.providers.ns import map_trips
from train_commute_companion.unclear import disagreement, newest_copies, train_key

A = make_leg("AAA", "UT", "08:12", "08:40")  # arrives 08:40, reported
P = planned_at_alight(A)  # arrives 08:40, planned only
LATE = make_leg("AAA", "UT", "08:12", "08:40", delay_arr=6)  # arrives 08:46, reported
T0 = datetime(2026, 10, 27, 8, 0, tzinfo=AMS)


def test_agreeing_or_single_copies_are_clear():
    assert disagreement([], "UT") is None and disagreement([A], "UT") is None
    assert disagreement([A, A], "UT") is None


def test_time_and_provenance_show_each_copy():
    u = disagreement([LATE, P], "UT")
    assert (u.label, u.station) == ("IC 1234", "UT")
    assert u.copies == ("arrives 08:40 (planned only)", "arrives 08:46 (live)")


def test_order_of_copies_never_changes_the_result():
    assert disagreement([P, LATE], "UT") == disagreement([LATE, P], "UT")


def test_identical_copies_collapse():
    assert disagreement([P, LATE, P], "UT").copies == disagreement([P, LATE], "UT").copies


def test_cancelled_stop_disagrees():
    gone = replace(A, stops=(A.stops[0], replace(A.stops[1], cancelled=True)))
    assert disagreement([A, gone], "UT").copies == ("stop served", "stop cancelled")


def test_platform_disagrees_and_names_the_planned_one():
    on5 = replace(A, stops=(replace(A.stops[0], planned_track="5"), A.stops[1]))
    u = disagreement([_on_track(on5, "7"), on5], "UT")
    assert u.station == "AAA" and u.copies == ("platform 5", "platform 7 (planned 5)")


def test_bus_instead_of_train_disagrees_and_keeps_the_train_label():
    bus = replace(A, alternative_transport=True, category="Bus")
    u = disagreement([bus, A], "UT")
    assert u.label == "IC 1234" and u.copies == ("runs as a train", "bus instead of train")


FULL = make_leg("AAA", "RTD", "08:12", "09:24", stops=["UT"])
# the same train as a leg that ends at UT, as NS lists it: its last stop has no departure
TO_UT = replace(FULL, stops=(FULL.stops[0], replace(FULL.stops[1], planned_departure=None, departure=None)))  # fmt: skip


def test_a_copy_ending_at_the_change_station_agrees():
    assert disagreement([FULL, TO_UT], "RTD") is None
    assert disagreement([TO_UT, FULL], "RTD") is None


def test_a_truncated_copy_is_compared_only_at_shared_stops():
    assert disagreement([planned_at_alight(FULL), TO_UT], "RTD") is None  # RTD is listed once only


def test_an_agreeing_partial_copy_changes_nothing():
    other = replace(FULL, stops=(FULL.stops[0], replace(FULL.stops[1], cancelled=True),
                                 planned_at_alight(FULL).stops[2]))  # fmt: skip
    assert disagreement([FULL, other, TO_UT], "RTD") == disagreement(
        [FULL, other], "RTD"
    )  # TO_UT agrees at UT


def test_a_difference_after_the_users_stop_is_ignored():
    assert disagreement([FULL, planned_at_alight(FULL)], "UT") is None  # differs only at RTD
    assert disagreement([FULL, planned_at_alight(FULL)], "RTD") is not None


def test_departure_at_the_users_stop_is_ignored():
    ut = FULL.stops[1]
    dep = replace(ut.departure, value=ut.departure.value + timedelta(minutes=3))
    later = replace(FULL, stops=(FULL.stops[0], replace(ut, departure=dep), FULL.stops[2]))
    assert disagreement([FULL, later], "UT") is None
    assert disagreement([FULL, later], "RTD") is not None  # the same values matter riding on


def test_endpoint_only_copies_past_the_users_stop_are_ignored():
    end = replace(FULL, stops=(FULL.stops[0], FULL.stops[2]))  # NS endpoint fallback: AAA, RTD
    assert disagreement([FULL, end, planned_at_alight(end)], "UT") is None  # differ only at RTD
    assert disagreement([FULL, end, planned_at_alight(end)], "RTD") is not None


def test_a_stop_after_the_users_stop_at_the_same_time_is_ignored():
    # builders plan BBB and UT both at 08:12; the user gets off at BBB
    two = make_leg("AAA", "RTD", "08:12", "09:24", stops=["BBB", "UT"])
    gone = replace(two, stops=(*two.stops[:2], replace(two.stops[2], cancelled=True), two.stops[3]))
    assert disagreement([two, gone], "BBB") is None  # differs only at UT, after BBB
    assert disagreement([two, gone], "UT") is not None
    end = replace(gone, stops=(gone.stops[0], gone.stops[2], gone.stops[3]))  # sparse: no BBB
    assert disagreement([two, end], "BBB") is None  # UT follows BBB in the full copy


def test_several_alighting_stops_use_the_latest():
    copies = [FULL, planned_at_alight(FULL)]  # differ only at RTD
    assert disagreement(copies, "UT", "RTD") == disagreement(copies, "RTD", "UT") is not None
    assert disagreement(copies, "UT", "UT") is None


def test_a_late_planned_time_in_a_truncated_copy_never_moves_the_bound():
    ut = TO_UT.stops[1]
    late = replace(ut, planned_arrival=FULL.stops[2].planned_arrival + timedelta(minutes=30))
    short = replace(TO_UT, stops=(TO_UT.stops[0], late))  # AAA, UT with UT planned after RTD
    copies = [FULL, planned_at_alight(FULL), short]  # the full copies differ only at RTD
    u = disagreement(copies, "UT", "RTD")
    assert (u.station, u.copies) == (
        "RTD",
        ("arrives 09:24 (planned only)", "arrives 09:24 (live)"),
    )
    assert disagreement(copies, "RTD", "UT") == u


def test_a_copy_cut_short_before_the_bound_is_clipped_by_time():
    # lock rides to RTD, a retained journey to UT; the cut copy lists UT but not RTD
    full = make_leg("AAA", "RTD", "08:12", "09:24", stops=["UT", "BBB"])  # AAA, UT, BBB, RTD
    bbb = replace(full.stops[2], cancelled=True, planned_departure=None, departure=None)
    cut = replace(full, stops=(*full.stops[:2], bbb))  # AAA, UT, BBB, as NS ends a leg
    assert disagreement([full, cut], "RTD", "UT") is not None  # BBB is on the trip to RTD
    assert disagreement([full, cut], "UT") is None  # BBB is past UT


def test_a_missing_live_platform_means_as_planned():
    on5 = replace(A, stops=(replace(A.stops[0], planned_track="5"), A.stops[1]))
    assert disagreement([on5, _on_track(on5, "5")], "UT") is None


def test_an_empty_platform_is_no_platform():
    on = replace(A, stops=(replace(A.stops[0], planned_track="", actual_track=""), A.stops[1]))
    assert disagreement([A, on], "UT") is None  # (None, None) vs ("", ""): both no platform
    assert disagreement([replace(A, stops=(replace(A.stops[0], planned_track="5"), A.stops[1])),
                         replace(A, stops=(replace(A.stops[0], planned_track="5", actual_track=""),
                                           A.stops[1]))], "UT") is None  # fmt: skip


def test_times_on_another_day_say_the_day():
    s = A.stops[1]
    nxt = replace(s, planned_arrival=s.planned_arrival + timedelta(days=1),
                  arrival=replace(s.arrival, value=s.arrival.value + timedelta(days=1),
                                  planned=s.arrival.planned + timedelta(days=1)))  # fmt: skip
    u = disagreement([A, replace(A, stops=(A.stops[0], nxt))], "UT")
    assert u.copies == ("planned to arrive 08:40, arrives 08:40 (live)",
                        "planned to arrive 08:40 on Wed 28 Oct, arrives 08:40 on Wed 28 Oct (live)")  # fmt: skip


def _ns_stop(
    code,
    uic,
    at,
    *,
    dep=True,
    arr=True,
    track=None,
    atrack=None,
    live=None,
    ltrack=None,
    latrack=None,
):
    """One NS trip stop as the API sends it; `code=None` sends the UIC number only."""
    t = f"2026-10-27T{at}:00+0100"
    lt = f"2026-10-27T{live or at}:00+0100"
    raw = {"uicCode": uic, "name": code or uic, "cancelled": False, "passing": False}
    if code:
        raw["stationCode"] = code
    if dep:
        raw |= {"plannedDepartureDateTime": t, "actualDepartureDateTime": lt}
        raw |= {"plannedDepartureTrack": track} if track else {}
        raw |= {"actualDepartureTrack": ltrack} if ltrack else {}
    if arr:
        raw |= {"plannedArrivalDateTime": t, "actualArrivalDateTime": lt}
        raw |= {"plannedArrivalTrack": atrack} if atrack else {}
        raw |= {"actualArrivalTrack": latrack} if latrack else {}
    return raw


def _ns_copies(*stop_lists):
    """The train 1234 once per stop list, each its own trip of one leg, mapped by `map_trips`."""
    trip = lambda stops: {"legs": [{"product": {"number": "1234", "categoryCode": "IC", "type": "TRAIN"},
                                    "stops": stops}]}  # fmt: skip
    a = map_trips({"trips": [trip(x) for x in stop_lists]}, Query("AAA", "RTD", T0), T0)
    return [c.legs[0] for c in a.connections]


def test_a_station_named_by_uic_in_one_copy_is_the_same_stop():
    # copy 1 names UT only by its UIC number (no stop in its trip pairs the two); copy 2 by code
    one = [_ns_stop("AAA", "1000001", "08:12", arr=False), _ns_stop(None, "1000002", "08:40", live="08:40"),
           _ns_stop(None, "1000009", "09:00"), _ns_stop("RTD", "1000003", "09:24", dep=False)]  # fmt: skip
    two = [_ns_stop("AAA", "1000001", "08:12", arr=False), _ns_stop("UT", "1000002", "08:40", live="08:46"),
           _ns_stop("BBB", "1000009", "09:00", live="09:05"), _ns_stop("RTD", "1000003", "09:24", dep=False)]  # fmt: skip
    copies = _ns_copies(one, two)
    assert (
        copies[0].stops[1].code == "1000002" and copies[1].stops[1].code == "UT"
    )  # the mapper as it is
    u = disagreement(copies, "RTD")
    assert u is not None  # 08:40 vs 08:46 at one station
    assert all(any(f"arrives {t} (live)" in c for c in u.copies) for t in ("08:40", "08:46"))
    assert disagreement(copies, "UT") is not None  # UT alone: still compared at UT
    # the exclusion past the bound: the full copy lists BBB after UT under its UIC number only,
    # two sparse copies (no UT, so the time bound) list BBB by code at the same minute as UT
    full = [_ns_stop("AAA", "1000001", "08:12", arr=False), _ns_stop("UT", "1000002", "08:40"),
            _ns_stop(None, "1000009", "08:40"), _ns_stop("RTD", "1000003", "09:24", dep=False)]  # fmt: skip
    sparse = lambda live: [_ns_stop("AAA", "1000001", "08:12", arr=False),
                           _ns_stop("BBB", "1000009", "08:40", live=live),
                           _ns_stop("RTD", "1000003", "09:24", dep=False)]  # fmt: skip
    copies = _ns_copies(full, sparse("08:40"), sparse("08:45"))
    assert disagreement(copies, "UT") is None  # BBB is past UT, whatever it is called
    assert disagreement(copies, "RTD") is not None


def test_a_through_platform_never_meets_an_arrival_platform():
    # through copy: arrives UT on 5, leaves on 7; the other copy ends its leg at UT on 5
    thru = [_ns_stop("AAA", "1000001", "08:12", arr=False, track="1"),
            _ns_stop("UT", "1000002", "08:40", track="7", atrack="5"),
            _ns_stop("RTD", "1000003", "09:24", dep=False, atrack="9")]  # fmt: skip
    end = [_ns_stop("AAA", "1000001", "08:12", arr=False, track="1"),
           _ns_stop("UT", "1000002", "08:40", dep=False, atrack="5")]  # fmt: skip
    copies = _ns_copies(thru, end)
    assert copies[0].stops[1].planned_track == "7"  # the mapper as it is: departure side first
    assert disagreement(copies, "UT") is None  # the user gets off at UT: arrival 5 vs arrival 5
    assert disagreement(copies, "RTD") is None  # 7 (departure) and 5 (arrival) are not compared
    moved = [*end[:1], _ns_stop("UT", "1000002", "08:40", dep=False, atrack="6")]
    # the user gets off at UT: thru's arrival platform there counts though UT is not its last stop
    assert disagreement(_ns_copies(thru, moved), "UT").copies == ("platform 5", "platform 6")
    assert disagreement(_ns_copies(end, moved), "UT").copies == ("platform 5", "platform 6")


def test_a_copy_with_no_departure_platform_says_nothing_about_it():
    # both copies arrive UT on 5; only copy 2 states a departure platform, 7
    first = _ns_stop("AAA", "1000001", "08:12", arr=False, track="1")
    last = _ns_stop("RTD", "1000003", "09:24", dep=False, atrack="9")
    one = _ns_stop("UT", "1000002", "08:40", atrack="5")
    two = _ns_stop("UT", "1000002", "08:40", track="7", atrack="5")
    assert disagreement(_ns_copies([first, one, last], [first, two, last]), "RTD") is None


def test_through_copies_arriving_on_other_platforms_where_the_user_gets_off_disagree():
    first = _ns_stop("AAA", "1000001", "08:12", arr=False, track="1")
    last = _ns_stop("RTD", "1000003", "09:24", dep=False, atrack="9")
    ut = lambda a: _ns_stop("UT", "1000002", "08:40", track="7", atrack=a)
    copies = _ns_copies([first, ut("5"), last], [first, ut("6"), last])
    assert disagreement(copies, "UT").copies == ("platform 5", "platform 6")
    assert disagreement(copies, "RTD") is None  # riding through: UT's departure platform agrees


def test_a_live_arrival_platform_is_not_a_live_departure_platform():
    # both copies plan UT arrival 5 and departure 7 and report arrival 5; only copy 2 reports departure 7
    ut = lambda **kw: _ns_stop("UT", "1000002", "08:40", track="7", atrack="5", latrack="5", **kw)
    first = _ns_stop("AAA", "1000001", "08:12", arr=False, track="1")
    last = _ns_stop("RTD", "1000003", "09:24", dep=False, atrack="9")
    copies = _ns_copies([first, ut(), last], [first, ut(ltrack="7"), last])
    assert [(c.stops[1].planned_track, c.stops[1].actual_track) for c in copies] == [
        ("7", None),
        ("7", "7"),
    ]
    assert disagreement(copies, "RTD") is None  # no live departure platform means as planned: 7
    moved = _ns_copies([first, ut(), last], [first, ut(ltrack="8"), last])
    assert disagreement(moved, "RTD").copies == ("platform 7", "platform 8 (planned 7)")


def test_a_difference_under_a_minute_is_no_disagreement():
    s = A.stops[1]
    later = replace(s.arrival, value=s.arrival.value + timedelta(seconds=30))
    assert (
        disagreement([A, replace(A, stops=(A.stops[0], replace(s, arrival=later)))], "UT") is None
    )


def test_differences_at_two_stops_prefix_the_later_one():
    other = replace(FULL, stops=(FULL.stops[0], replace(FULL.stops[1], cancelled=True),
                                 planned_at_alight(FULL).stops[2]))  # fmt: skip
    u = disagreement([FULL, other], "RTD")
    assert u.station == "UT"
    assert u.copies == ("stop served, at RTD arrives 09:24 (live)",
                        "stop cancelled, at RTD arrives 09:24 (planned only)")  # fmt: skip


def test_dst_fold_times_disagree_and_say_second():
    day = date(2026, 10, 24)  # builders put 02:xx on Sun 25 Oct, the DST-end night
    first = make_leg("AAA", "UT", "02:10", "02:30", day=day)
    second = make_leg("AAA", "UT", "02:10", "02:30", day=day, fold_arr=1)  # moves parr and arr
    u = disagreement([second, first], "UT")
    assert u.copies == (
        "planned to arrive 02:30 on Sun 25 Oct, arrives 02:30 on Sun 25 Oct (live)",
        "planned to arrive 02:30 (second) on Sun 25 Oct, arrives 02:30 (second) on Sun 25 Oct (live)",
    )
    assert u == disagreement([first, second], "UT")


def test_same_instant_in_another_zone_agrees():
    s = A.stops[1]
    utc = replace(s.arrival, value=s.arrival.value.astimezone(UTC))
    assert disagreement([A, replace(A, stops=(A.stops[0], replace(s, arrival=utc)))], "UT") is None


def test_fingerprint_is_hex_and_follows_the_values():
    u = disagreement([LATE, P], "UT")
    assert len(u.fingerprint) == 16 and set(u.fingerprint) <= set("0123456789abcdef")
    assert (
        disagreement([P, A], "UT").fingerprint != u.fingerprint
    )  # other values, other fingerprint


def test_reserved_characters_in_provider_strings_stay_out_of_keys():
    on = replace(A, stops=(replace(A.stops[0], planned_track="5|x"), A.stops[1]))
    u = disagreement([on, _on_track(on, "7#north")], "UT")
    assert set(u.fingerprint) <= set("0123456789abcdef")
    assert u.copies == ("platform 5|x", "platform 7#north (planned 5|x)")  # text keeps them
    odd = replace(A.train, train_number="12|3#4 ~")
    assert not set("|#;~ ") & set(train_key(odd))


def test_newest_copies_take_only_the_newest_fetch():
    q = Query("AAA", "RTD", T0)
    old = ProviderAnswer(q, (make_connection([P]),), T0)
    new = ProviderAnswer(
        q, (make_connection([A]), make_connection([LATE])), T0 + timedelta(minutes=2)
    )
    same = ProviderAnswer(q, (make_connection([P]),), (T0 + timedelta(minutes=2)).astimezone(UTC))
    assert newest_copies([old, new], A.train, "AAA") == [A, LATE]
    assert newest_copies([old, new, same], A.train, "AAA") == [A, LATE, P]
    assert newest_copies([old, new], A.train, "UT") == []


def test_newest_copies_find_a_copy_boarding_under_its_uic_number():
    # copy 1 names AAA only by its UIC number (no stop in its trip pairs the two); copy 2 by code
    one = [
        _ns_stop(None, "1000001", "08:12", arr=False),
        _ns_stop("UT", "1000002", "08:40", dep=False),
    ]
    two = [_ns_stop("AAA", "1000001", "08:12", arr=False),
           _ns_stop("UT", "1000002", "08:40", dep=False, live="08:46")]  # fmt: skip
    copies = _ns_copies(one, two)
    assert copies[0].board.code == "1000001"  # the mapper as it is; current_run drops this copy
    a = ProviderAnswer(Query("AAA", "RTD", T0), tuple(make_connection([c]) for c in copies), T0)
    for board in (
        "AAA",
        "1000001",
    ):  # live passes the followed code, the look-ahead the snapped copy's
        assert newest_copies([a], copies[1].train, board) == copies
    assert disagreement(copies, "UT") is not None  # 08:40 vs 08:46


def test_train_key():
    assert train_key(A.train) == "1234@20261027T0712Z"
