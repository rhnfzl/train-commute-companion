from dataclasses import replace

from builders import make_connection, make_leg
from test_lifecycle_follow import at, ctx

from train_commute_companion.messages import body, render
from train_commute_companion.model import EventKind, Leg


def named(leg: Leg, board: str, alight: str) -> Leg:
    stops = (
        replace(leg.stops[0], name=board),
        *leg.stops[1:-1],
        replace(leg.stops[-1], name=alight),
    )
    return replace(leg, stops=stops)


def utrecht_rotterdam(dep: str = "08:12", arr: str = "08:50", **k: object) -> Leg:
    return named(make_leg("UT", "RTD", dep, arr, **k), "Utrecht Centraal", "Rotterdam Centraal")  # type: ignore[arg-type]


def r0844() -> object:
    leg = make_leg("UT", "RTD", "08:44", "09:20", number="9")
    return make_connection(
        [replace(leg, stops=(replace(leg.stops[0], actual_track="5"), leg.stops[1]))]
    )


def test_message_starts_with_profile_and_names_leg():
    assert render(
        EventKind.CANCELLED, ctx(at("08:00")), leg=utrecht_rotterdam(), replacement=r0844()
    ) == (
        "Office A: your Utrecht Centraal to Rotterdam Centraal train is cancelled. "
        "Next good connection: 08:44, platform 5."
    )


def test_cut_short_text_never_says_cancelled():
    t = render(
        EventKind.CUT_SHORT,
        ctx(at("08:00")),
        leg=utrecht_rotterdam(),
        stop=utrecht_rotterdam().alight,
    )
    assert "runs only to Rotterdam Centraal, change there" in t and "cancel" not in t.lower()


def test_change_journey_is_never_direct_and_ok_says_per_ns():
    l1, l2 = make_leg("AAA", "UT", "08:12", "08:40"), make_leg("UT", "RTD", "08:50", "09:24")
    t = render(EventKind.ALL_GOOD, ctx(at("08:00")), legs=[l1, l2])
    assert "on time per NS" in t and "change at UT" in t and "direct" not in t
    t = render(EventKind.CANCELLED, ctx(at("08:00")), leg=l1, replacement=make_connection([l1, l2]))
    assert "direct" not in t and "change at UT" in t


def test_no_replacement_points_to_the_app():
    t = render(EventKind.CANCELLED, ctx(at("08:00")), leg=utrecht_rotterdam())
    assert t.endswith("No good connection found, check the NS app.")


def test_every_kind_renders_without_em_dash():
    for k in EventKind:
        assert chr(0x2014) not in render(k, ctx(at("08:00")), leg=utrecht_rotterdam())


def test_every_transfer_station_is_named_in_order():
    l1 = make_leg("AAA", "UT", "08:12", "08:40")
    l2 = make_leg("UT", "BBB", "08:50", "09:05", number="2")
    l3 = make_leg("BBB", "RTD", "09:15", "09:30", number="3")
    t = render(EventKind.ALL_GOOD, ctx(at("08:00")), legs=[l1, l2, l3])
    assert "change at UT then BBB" in t
    t = render(
        EventKind.CANCELLED, ctx(at("08:00")), leg=l1, replacement=make_connection([l1, l2, l3])
    )
    assert "change at UT then BBB." in t


def test_data_unclear_lists_each_copy():
    t = render(
        EventKind.DATA_UNCLEAR,
        ctx(at("08:00")),
        label="IC 1234",
        station="Utrecht Centraal",
        copies=("arrives 08:40 (planned only)", "arrives 08:46 (live)"),
    )
    assert t == (
        "Office A: NS data unclear for IC 1234 at Utrecht Centraal. Copy 1: arrives 08:40"
        " (planned only). Copy 2: arrives 08:46 (live). Check the NS app or the station boards."
    )


def test_data_unclear_without_facts_still_reads():
    assert body(EventKind.DATA_UNCLEAR) == (
        "NS data unclear for your train. Check the NS app or the station boards."
    )
