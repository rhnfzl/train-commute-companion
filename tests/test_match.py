from dataclasses import replace
from datetime import UTC, date, datetime, time

from builders import AMS, make_connection, make_leg

from train_commute_companion.match import (
    matches_pattern,
    remaining_pattern,
    route_changed,
    usual_connection,
)
from train_commute_companion.model import Signature, SignatureLeg
from train_commute_companion.providers.base import ProviderAnswer, Query


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=AMS)


def answer(*conns):
    q = Query("AAA", "BBB", dt("2026-10-27 08:12"))
    return ProviderAnswer(q, tuple(conns), q.at)


def direct():
    return make_connection([make_leg("AAA", "BBB", "08:12", "09:24")])


def one_transfer(second="09:24", num2="2000"):
    return make_connection(
        [
            make_leg("AAA", "UT", "08:12", "08:40", number="1000"),
            make_leg("UT", "BBB", "08:46", second, number=num2),
        ]
    )


def two_transfers():
    return make_connection(
        [
            make_leg("AAA", "UT", "08:12", "08:40", number="1000"),
            make_leg("UT", "BBB", "08:46", "09:00", number="2000"),
            make_leg("BBB", "RTD", "09:05", "09:30", number="3000"),
        ]
    )


def test_direct_one_transfer_two_transfers_match():
    assert matches_pattern(direct(), ())
    assert matches_pattern(one_transfer(), ("UT",))
    assert matches_pattern(two_transfers(), ("UT", "BBB"))
    assert not matches_pattern(one_transfer(), ())


def test_bus_leg_never_matches():
    bus = replace(make_leg("UT", "BBB", "08:46", "09:24"), alternative_transport=True)
    c = make_connection([make_leg("AAA", "UT", "08:12", "08:40"), bus])
    assert not matches_pattern(c, ("UT",))


def test_tie_break_two_connections_share_first_train():
    a = one_transfer("09:24", "2000")
    b = one_transfer("09:30", "2001")
    for order in ((b, a), (a, b)):
        c = usual_connection(answer(*order), "AAA", dt("2026-10-27 08:12"), ("UT",), None)
        assert c is not None and c.legs[-1].alight.planned_arrival == dt("2026-10-27 09:24")


def test_signature_beats_earliest_arrival():
    a = one_transfer("09:24", "2000")
    b = one_transfer("09:30", "2001")
    sig = Signature(
        (
            SignatureLeg("AAA", "UT", time(8, 12), 0, 28),
            SignatureLeg("UT", "BBB", time(8, 46), 0, 44),
        )
    )
    c = usual_connection(answer(a, b), "AAA", dt("2026-10-27 08:12"), ("UT",), sig)
    assert c is b


def test_wrong_departure_or_origin_is_none():
    ans = answer(one_transfer())
    assert usual_connection(ans, "AAA", dt("2026-10-27 08:13"), ("UT",), None) is None
    assert usual_connection(ans, "UT", dt("2026-10-27 08:12"), ("UT",), None) is None


def test_detour_is_never_usual():
    ans = answer(two_transfers())
    assert usual_connection(ans, "AAA", dt("2026-10-27 08:12"), ("UT",), None) is None


def test_suffix_matching_after_done_prefix():
    assert remaining_pattern(("UT", "BBB"), "UT") == ("BBB",)
    assert remaining_pattern(("UT", "BBB"), "BBB") == ()
    assert remaining_pattern(("UT", "BBB"), "AAA") == ("UT", "BBB")


def test_route_changed_on_suffix():
    locked = [make_leg("AAA", "UT", "08:12", "08:40", number="1000").train]
    assert route_changed(locked, answer(two_transfers()), ("UT",)) is not None
    assert route_changed(locked, answer(two_transfers(), one_transfer()), ("UT",)) is None
    assert route_changed(locked, answer(direct()), ("UT",)) is None  # other train: not offered
    assert route_changed([], answer(one_transfer()), ("UT",)) is None


def test_signature_uses_local_date_when_first_departure_is_utc():
    # 00:30 local is the previous day in UTC; day_offset must still be measured locally
    def late(second, num2):
        return make_connection(
            [
                make_leg("AAA", "UT", "00:30", "00:50", number="1000"),
                make_leg("UT", "BBB", "00:56", second, number=num2),
            ]
        )

    a, b = late("01:30", "2000"), late("01:40", "2001")
    sig = Signature(
        (
            SignatureLeg("AAA", "UT", time(0, 30), 0, 20),
            SignatureLeg("UT", "BBB", time(0, 56), 0, 44),
        )
    )
    first = dt("2026-10-28 00:30").astimezone(UTC)
    assert usual_connection(answer(a, b), "AAA", first, ("UT",), sig) is b


def without_arrival(c):
    last = c.legs[-1]
    end = replace(last.alight, planned_arrival=None, arrival=None)
    return make_connection([*c.legs[:-1], replace(last, stops=(*last.stops[:-1], end))])


def test_unknown_arrival_ranks_after_a_known_one():
    known = one_transfer("09:24", "2000")
    unknown = without_arrival(one_transfer("09:30", "2001"))
    for order in ((unknown, known), (known, unknown)):
        assert (
            usual_connection(answer(*order), "AAA", dt("2026-10-27 08:12"), ("UT",), None) is known
        )
    locked = [known.legs[0].train]
    detour = two_transfers()
    for order in ((without_arrival(detour), detour), (detour, without_arrival(detour))):
        assert route_changed(locked, answer(*order), ("UT",)) is detour


def test_earliest_arrival_across_the_repeated_hour_uses_instants():
    def late(arr, fold, num2):
        return make_connection(
            [
                make_leg("AAA", "UT", "01:00", "01:20", number="1000", day=date(2026, 10, 24)),
                make_leg(
                    "UT", "BBB", "01:30", arr, number=num2, day=date(2026, 10, 24), fold_arr=fold
                ),
            ]
        )

    second_0210 = late("02:10", 1, "2000")  # CET, 01:10 UTC
    first_0240 = late("02:40", 0, "2001")  # CEST, 00:40 UTC
    first = datetime(2026, 10, 25, 1, 0, tzinfo=AMS)
    assert (
        usual_connection(answer(second_0210, first_0240), "AAA", first, ("UT",), None) is first_0240
    )


def test_usual_journey_ranks_on_the_current_arrival():
    from train_commute_companion.lifecycle import _pool

    stale, alt = one_transfer("09:24", "2000"), one_transfer("09:33", "2001")
    late = make_connection([make_leg("UT", "BBB", "08:46", "09:24", number="2000", delay_arr=30)])
    older = answer(stale, alt)
    newer = ProviderAnswer(
        Query("UT", "BBB", dt("2026-10-27 08:13")), (late,), dt("2026-10-27 08:13")
    )
    first = dt("2026-10-27 08:12")
    assert usual_connection(older, "AAA", first, ("UT",), None) is stale
    assert usual_connection(_pool([older, newer]), "AAA", first, ("UT",), None) is alt
