from dataclasses import replace
from datetime import UTC, datetime, time

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
