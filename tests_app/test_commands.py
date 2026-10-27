from datetime import date, time, timedelta

import pytest
from builders import make_connection, make_leg
from tcc_app.commands import (
    Target,
    command_word,
    parse_target,
    render_status,
    status_buttons,
)
from test_runtime import at

from train_commute_companion.health import BoardHealth
from train_commute_companion.lifecycle import DirectionState, LegState
from train_commute_companion.model import (
    AdviceRecord,
    Direction,
    OccurrenceKey,
    TrainId,
    TransferState,
)
from train_commute_companion.view import (
    DirectionInfo,
    LegView,
    Moved,
    OccurrenceView,
    RuntimeView,
    TransferView,
    WorksDay,
)

DAY = date(2026, 10, 27)


def directions(*names: str) -> tuple[DirectionInfo, ...]:
    return tuple(DirectionInfo(f"p{i}", n, d, 1) for i, n in enumerate(names, 1) for d in Direction)


def leg_view(dep="08:12", delay=None, platform=None, state="ok", board="AAA", alight="UT"):
    t = at(dep)
    return LegView(
        "1234",
        board,
        alight,
        t,
        t,
        None,
        None,
        platform,
        None,
        delay,
        None,
        state,
        LegState.PENDING,
    )


def occ(pid="p1", resolved=False, **kw) -> OccurrenceView:
    base = {
        "key": OccurrenceKey(pid, Direction.OUTBOUND, DAY), "profile_name": "office_a",
        "direction_state": DirectionState.LOCKED, "resolved": resolved, "not_travelling": False,
        "route_changed": False, "lock_source": None, "commitment": None,
        "legs": (leg_view(delay=3, platform="5"), leg_view("08:30", board="UT", alight="RTD")),
        "transfers": (TransferView("UT", TransferState.OK, 6, 4),), "retained": (), "next_good": None,
        "advice": None, "health": BoardHealth(True, at("07:50"), 0, False),
    }  # fmt: skip
    return OccurrenceView(**{**base, **kw})  # type: ignore[arg-type]


def view(*occs, names=("office_a",), **kw) -> RuntimeView:
    base = {
        "at": at("07:55"), "started": True, "profiles": (), "directions": directions(*names),
        "occurrences": tuple(occs), "works": {}, "moved": (), "refusal": None, "store_ok": True,
        "key_rejected": False, "undelivered": 0, "requests": 0,
    }  # fmt: skip
    return RuntimeView(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize("args, want", [
    ([], Target("p1", "office_a")),
    (["return", "08:44", "1234", "2026-10-27"],
     Target("p1", "office_a", Direction.RETURN, DAY, time(8, 44), "1234")),
    (["day"], Target("p1", "office_a", whole_day=True)),
    (["Office_A"], Target("p1", "office_a")),
    (["nobody"], "No profile nobody. One of: office_a."),
    (["08:44", "soon"], "I did not understand soon."),
    (["25:99"], "I did not understand 25:99."),
])  # fmt: skip
def test_parse_target_one_profile(args, want):
    assert parse_target(view(), args) == want


def test_parse_target_two_profiles_asks_which():
    v = view(names=("office_a", "office_b"))
    assert parse_target(v, ["08:44"]) == "Which profile? One of: office_a, office_b."
    assert parse_target(v, ["office b"]) == Target("p2", "office_b")


def test_parse_target_spaced_name_split_by_whitespace():
    v = view(names=("office_a", "office_b"))
    want = Target("p2", "office_b", departure=time(8, 44))
    assert parse_target(v, ["p=office", "b", "08:44"]) == want
    assert parse_target(v, ["08:44", "p=office", "b"]) == want


def test_parse_target_profile_id_like_a_keyword():
    v = view()
    v = (
        RuntimeView(
            **{
                **v.__dict__,
                "directions": (
                    DirectionInfo("return", "r", Direction.OUTBOUND, 1),
                    DirectionInfo("p2", "p", Direction.OUTBOUND, 1),
                ),
            }
        )
        if False
        else v
    )
    from dataclasses import replace

    v = replace(v, directions=(DirectionInfo("return", "ret", Direction.OUTBOUND, 1),
                               DirectionInfo("p2", "two", Direction.OUTBOUND, 1)))  # fmt: skip
    assert parse_target(v, ["p=return", "return"]) == Target("return", "ret", Direction.RETURN)
    assert parse_target(v, ["p=nobody"]) == "No profile nobody. One of: ret, two."


def test_render_status_two_occurrences_and_global_lines():
    nxt = make_connection([make_leg("AAA", "RTD", "08:44", "09:30", number="2345")])
    adv = AdviceRecord(TrainId(DAY, "99", at("07:58")), None, False)
    o1 = occ(next_good=nxt, advice=adv, not_travelling=True,
             retained=((leg_view("08:40"),),),
             health=BoardHealth(False, at("07:30"), 3, False))  # fmt: skip
    o2 = occ(key=OccurrenceKey("p1", Direction.RETURN, DAY + timedelta(days=1)))
    v = view(
        o1, o2, refusal="bad file", key_rejected=True, store_ok=False, undelivered=2,
        moved=(Moved("p1", Direction.OUTBOUND, "Office A: moved"),),
        works={"p1|outbound": (WorksDay(DAY, None),)},
        lookahead_last_ok={"p1|outbound": at("06:00", DAY - timedelta(days=2)),
                           "p1|return": None},
    )  # fmt: skip
    text = render_status(v, confirmed=False)
    lines = text.split("\n")
    assert "Office A outbound Tue 27 Oct: locked (not travelling)" in lines
    assert "Office A return Wed 28 Oct: locked" in lines
    assert "  08:12 AAA to UT: ok, +3 min, platform 5" in lines
    assert "  change at UT: ok (6 min, needs 4)" in lines
    assert "  if you are on the 08:40: 08:40 AAA to UT: ok" in lines
    assert "  next good connection: 08:44 from AAA" in lines
    assert "  earlier train advised: the 07:58" in lines
    assert "  NS data: not fresh since 07:30" in lines and "  NS data: fresh" in lines
    for g in ("Profile file refused: bad file", "Your NS key was rejected.",
              "State cannot be saved; retrying.", "2 alerts not delivered yet.",
              "Office A: moved", "Works ahead: Office A outbound: Tue 27 Oct",
              "Planned-works check: last ok Sun 25 Oct 06:00",
              "Planned-works check: not run yet",
              "Delivery not confirmed yet: send /test."):  # fmt: skip
        assert g in lines
    assert lines[-1] == "Data: NS"


def test_render_status_planned_works_not_run_yet():
    t = render_status(view(lookahead_last_ok={"p1|outbound": None}), True)
    assert "Planned-works check: not run yet" in t
    assert "No journey is being followed now." in t
    assert "Delivery not confirmed" not in t
    fresh = view(lookahead_last_ok={"p1|outbound": at("06:00")})
    assert "Planned-works" not in render_status(fresh, True)


def test_status_buttons_take_next_good():
    nxt = make_connection([make_leg("AAA", "RTD", "08:44", "09:30", number="2345")])
    b = status_buttons(view(occ(next_good=nxt), occ(resolved=True)))
    assert b is not None and len(b["inline_keyboard"]) == 1
    row = b["inline_keyboard"][0]
    assert [x["text"] for x in row] == ["I'm leaving", "Not travelling", "Take 08:44"]
    assert row[2]["callback_data"] == "c|p1|outbound|2026-10-27|08:44|2345"
    assert status_buttons(view()) is None
    long = make_connection([make_leg("AAA", "RTD", "08:44", "09:30", number="9" * 40)])
    row = status_buttons(view(occ(next_good=long)))["inline_keyboard"][0]  # type: ignore[index]
    assert row[2]["callback_data"] == "c|p1|outbound|2026-10-27|08:44"
    # delayed: the label shows the reported time, the data the planned one the core looks up
    for number, data in (("2345", "08:44|2345"), ("9" * 40, "08:44")):
        late = make_connection(
            [make_leg("AAA", "RTD", "08:44", "09:30", number=number, delay_dep=3)]
        )
        row = status_buttons(view(occ(next_good=late)))["inline_keyboard"][0]  # type: ignore[index]
        assert (row[2]["text"], row[2]["callback_data"]) == (
            "Take 08:47",
            f"c|p1|outbound|2026-10-27|{data}",
        )


def test_command_word_strips_the_bot_name():
    assert command_word("/Status@SomeBot now") == "/status"
    assert command_word("  ") == ""
