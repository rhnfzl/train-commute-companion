from datetime import date, time, timedelta

import pytest
from builders import make_connection, make_leg
from tcc_app.commands import (
    NO_WORKS,
    REFUSAL_HIDDEN,
    Target,
    answer,
    command_word,
    for_profile,
    parse_target,
    render_status,
    render_webhook_tests,
    status_buttons,
)
from test_runtime import at

from train_commute_companion.health import BoardHealth
from train_commute_companion.lifecycle import DirectionState, LegState
from train_commute_companion.model import (
    AdviceRecord,
    Direction,
    OccurrenceKey,
    Provenance,
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
from train_commute_companion.webhook import WebhookTestResult

DAY = date(2026, 10, 27)


def directions(*names: str) -> tuple[DirectionInfo, ...]:
    return tuple(DirectionInfo(f"p{i}", n, d, 1) for i, n in enumerate(names, 1) for d in Direction)


def leg_view(
    dep="08:12", delay=None, platform=None, state="ok", board="AAA", alight="UT",
    reported=True, arr=None, planned_arr=None, progress=LegState.PENDING,
):  # fmt: skip
    t = at(dep)
    return LegView(
        "1234",
        board,
        alight,
        t,
        t if reported else None,
        at(planned_arr or arr) if planned_arr or arr else None,
        at(arr) if arr else None,
        platform,
        None,
        delay,
        None,
        state,
        progress,
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
    assert (
        "  08:12 AAA to UT: on time per NS, +3 min, platform 5, no reported arrival time" in lines
    )
    assert "  change at UT: ok (6 min, needs 4)" in lines
    assert (
        "  if you are on the 08:40: 08:40 AAA to UT: on time per NS, no reported arrival time"
        in lines
    )
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


def test_render_webhook_tests_names_hosts_and_results():
    ok = WebhookTestResult("hooks.example.invalid", 200, 200)
    bad = WebhookTestResult("hooks2.example.invalid", 503, "ConnectError")
    assert render_webhook_tests([ok, bad]).splitlines() == [
        "Webhook hooks.example.invalid: accepted the loud and the silent test.",
        (
            "Webhook hooks2.example.invalid (loud 503, silent ConnectError): not accepted."
            " Check the receiver, its secret and its token."
        ),
    ]


FRESH = at("04:00")
OLD = at("04:00", DAY - timedelta(days=2))
NEXT = {"tool": "get_next_connections", "profile": "p1", "direction": "outbound"}
WORKS = {"tool": "get_works_ahead", "profile": "p1"}


def test_mcp_status_equals_status_with_one_profile():
    v = view(occ(), store_ok=False)
    for p in ("", "p1", "office_a", "Office A"):
        assert answer(v, False, {"tool": "get_status", "profile": p})["text"] == render_status(
            v, False
        )
    assert answer(v, True, {"tool": "get_status", "profile": "p1"})["name"] == "Office A"


def test_two_profiles_never_mix():  # Review Focus 2
    o2 = occ("p2", profile_name="office_b", legs=(leg_view("08:25", board="BBB"),))
    v = view(
        occ(), o2, names=("office_a", "office_b"),
        works={"p1|outbound": (WorksDay(DAY + timedelta(days=7), None),),
               "p2|outbound": (WorksDay(DAY + timedelta(days=8), None),)},
        lookahead_last_ok={"p1|outbound": None, "p1|return": FRESH,
                           "p2|outbound": OLD, "p2|return": FRESH},
        moved=(Moved("p1", Direction.OUTBOUND, "office_a.outbound: NS moved your 08:12"),),
    )  # fmt: skip
    status = render_status(v, True).splitlines()
    for p in ("p2", "office_b", "Office B"):
        for req in ({"tool": "get_status"}, {**NEXT}, {**WORKS}):
            text = answer(v, True, {**req, "profile": p})["text"]
            assert "Planned-works check: not run yet" not in text and "08:12" not in text
            for line in text.splitlines():
                assert "Office A" not in line and "office_a" not in line
                assert line in status or line == "No journey is being followed now."
    r = answer(v, True, {**NEXT, "profile": "p2"})
    assert [j["service_date"] for j in r["journeys"]] == ["2026-10-27"]
    p1 = answer(v, True, {"tool": "get_status", "profile": "p1"})["text"]
    assert "Planned-works check: not run yet" in p1 and "Office B" not in p1 and "BBB" not in p1


def test_unknown_or_missing_profile_is_an_error():  # Review Focus 2
    v = view(names=("office_a", "office_b"))
    assert answer(v, True, {"tool": "get_status", "profile": "nobody"}) == {
        "error": "No profile nobody. One of: office_a, office_b."
    }
    assert answer(v, True, {"tool": "get_works_ahead", "profile": ""}) == {
        "error": "Which profile? One of: office_a, office_b."
    }


def test_two_occurrences_of_one_direction_both_listed():  # Review Focus 2, 7.13
    later = occ(key=OccurrenceKey("p1", Direction.OUTBOUND, DAY + timedelta(days=1)))
    r = answer(view(later, occ()), True, NEXT)
    assert [j["service_date"] for j in r["journeys"]] == ["2026-10-27", "2026-10-28"]
    lines = r["text"].splitlines()
    first = lines.index("Office A outbound Tue 27 Oct: locked")
    assert first < lines.index("Office A outbound Wed 28 Oct: locked")


def test_next_connection_matches_the_status_line():
    nxt = make_connection([make_leg("AAA", "RTD", "08:44", "09:24", number="2345")])
    v = view(occ(next_good=nxt))
    r = answer(v, True, NEXT)
    ng = r["journeys"][0]["next_good"]
    assert (ng["departure"], ng["from"]) == ("08:44", "AAA")
    assert ng["legs"] == [{
        "train_number": "2345", "board": "AAA", "alight": "RTD", "planned_departure": "08:44",
        "departure": "08:44", "planned_arrival": "09:24", "arrival": "09:24", "platform": None,
    }]  # fmt: skip
    status = render_status(v, True).splitlines()
    assert "  next good connection: 08:44 from AAA" in r["text"].splitlines()
    assert all(line in status for line in r["text"].splitlines())


def test_next_connection_planned_times_are_never_reported():  # rule 20
    nxt = make_connection([make_leg("AAA", "RTD", "08:44", "09:24", provenance=Provenance.PLANNED)])
    leg = answer(view(occ(next_good=nxt)), True, NEXT)["journeys"][0]["next_good"]["legs"][0]
    assert (leg["planned_departure"], leg["departure"]) == ("08:44", None)
    assert (leg["planned_arrival"], leg["arrival"]) == ("09:24", None)


def test_no_journey_followed_gives_no_journeys():
    r = answer(view(), True, NEXT)
    assert (r["journeys"], r["text"]) == ([], "No journey is being followed now.\nData: NS")


def test_unhealthy_states_show_the_same_warnings_as_status():  # Review Focus 3
    o = occ(health=BoardHealth(False, at("08:10"), 2, False))
    v = view(o, store_ok=False, key_rejected=True, undelivered=2)
    text = answer(v, False, {"tool": "get_status", "profile": "p1"})["text"]
    assert text == render_status(v, False)
    for w in ("State cannot be saved; retrying.", "Your NS key was rejected.",
              "2 alerts not delivered yet.", "  NS data: not fresh since 08:10",
              "Delivery not confirmed yet: send /test."):  # fmt: skip
        assert w in text.splitlines()


def test_works_ahead_days_and_check_age():
    v = view(
        works={"p1|outbound": (WorksDay(date(2026, 11, 3), None),)},
        lookahead_last_ok={"p1|outbound": FRESH, "p1|return": FRESH},
    )
    r = answer(v, True, WORKS)
    assert r["directions"] == [
        {"direction": "outbound", "days": [{"date": "2026-11-03", "leg": None}],
         "last_ok": FRESH.isoformat(), "stale": False},
        {"direction": "return", "days": [], "last_ok": FRESH.isoformat(),
         "stale": False},
    ]  # fmt: skip
    assert r["text"] == "Works ahead: Office A outbound: Tue 3 Nov\nData: NS"


def test_no_works_with_a_stale_or_missing_check_is_not_clean():  # Review Focus 3
    for checks, line in (
        ({"p1|outbound": None, "p1|return": None}, "Planned-works check: not run yet"),
        ({"p1|outbound": FRESH}, "Planned-works check: not run yet"),
        ({"p1|outbound": FRESH, "p1|return": OLD}, "Planned-works check: last ok Sun 25 Oct 04:00"),
    ):
        r = answer(view(lookahead_last_ok=checks), True, WORKS)
        assert any(d["stale"] for d in r["directions"])
        assert line in r["text"].splitlines() and NO_WORKS not in r["text"]
    clean = view(lookahead_last_ok={"p1|outbound": FRESH, "p1|return": FRESH})
    assert answer(clean, True, WORKS)["text"] == NO_WORKS + "\nData: NS"


def test_profile_ids_are_matched_exactly():
    dirs = directions("office_a") + tuple(
        DirectionInfo("p1|child", "p1|child", d, 1) for d in Direction
    )
    v = view(
        directions=dirs,
        works={"p1|outbound": (WorksDay(date(2026, 11, 3), None),),
               "p1|child|outbound": (WorksDay(date(2026, 11, 4), None),)},
        lookahead_last_ok={"p1|outbound": FRESH, "p1|return": FRESH,
                           "p1|child|outbound": None, "p1|child|return": FRESH},
    )  # fmt: skip
    mine = for_profile(v, "p1")
    assert set(mine.works) == {"p1|outbound"}
    assert set(mine.lookahead_last_ok) == {"p1|outbound", "p1|return"}
    want = "Works ahead: P1|Child outbound: Wed 4 Nov"
    assert want in render_status(v, True).splitlines()
    child = answer(v, True, {**WORKS, "profile": "p1|child"})["text"].splitlines()
    assert want in child and "Planned-works check: not run yet" in child
    p1 = answer(v, True, WORKS)["text"]
    assert "Child" not in p1 and "not run yet" not in p1


def test_long_profile_name_is_accepted():
    pid, name = "p" * 150, "office_" + "a" * 143
    v = view(directions=tuple(DirectionInfo(pid, name, d, 1) for d in Direction))
    for p in (pid, name):
        r = answer(v, True, {"tool": "get_status", "profile": p})
        assert r["profile"] == pid and r["text"].endswith("Data: NS")


def test_removed_profile_still_running_is_never_clean():
    dirs = directions("office_a") + (DirectionInfo("p2", "office_b", Direction.OUTBOUND, 1),)
    v = view(directions=dirs, lookahead_last_ok={"p1|outbound": FRESH, "p1|return": FRESH})
    r = answer(v, True, {**WORKS, "profile": "p2"})
    assert r["directions"] == [
        {"direction": "outbound", "days": [], "last_ok": None, "stale": True}
    ]
    assert "Planned-works check: not run yet" in r["text"].splitlines()
    assert NO_WORKS not in r["text"]


def test_every_tool_carries_the_warnings():  # Review Focus 3
    v = view(occ(), store_ok=False, key_rejected=True, undelivered=2)
    for req in (NEXT, WORKS):
        lines = answer(v, False, req)["text"].splitlines()
        for w in ("Your NS key was rejected.", "State cannot be saved; retrying.",
                  "2 alerts not delivered yet.", "Delivery not confirmed yet: send /test."):  # fmt: skip
            assert w in lines
        assert lines[-1] == "Data: NS"


def test_every_tool_shows_every_warning_status_shows():  # Review Focus 3, codex group fix
    stale = occ(health=BoardHealth(False, at("07:10"), 2, False))
    ret = OccurrenceKey("p1", Direction.RETURN, DAY)
    failed = occ(key=ret, health=BoardHealth(True, at("07:50"), 1, False))
    moved = Moved("p1", Direction.OUTBOUND, "office_a.outbound: NS moved your 08:12 to 08:25")
    v = view(stale, failed, moved=(moved,), store_ok=False,
             lookahead_last_ok={"p1|outbound": FRESH, "p1|return": OLD})  # fmt: skip
    warnings = [
        "  NS data: not fresh since 07:10", "  NS data: fresh, last poll failed",
        "State cannot be saved; retrying.", moved.text,
        "Planned-works check: last ok Sun 25 Oct 04:00", "Delivery not confirmed yet: send /test.",
    ]  # fmt: skip
    status = render_status(v, False).splitlines()
    for req in ({"tool": "get_status", "profile": "p1"}, NEXT, {**NEXT, "direction": "return"},
                WORKS):  # fmt: skip
        lines = answer(v, False, req)["text"].splitlines()
        for w in warnings:
            assert w in lines and w in status, (req, w)
    # a stale board with fresh, empty works checks still says the board is stale
    fresh = {"p1|outbound": FRESH, "p1|return": FRESH}
    lines = answer(view(stale, lookahead_last_ok=fresh), True, WORKS)["text"].splitlines()
    assert "  NS data: not fresh since 07:10" in lines and NO_WORKS in lines
    nxt = answer(view(stale, lookahead_last_ok=fresh), True, NEXT)["text"].splitlines()
    assert nxt.count("  NS data: not fresh since 07:10") == 1  # shown once, not twice


def test_leg_lines_never_read_as_proven():  # rule 20, checklist M12
    riding = LegState.RIDING
    o = occ(
        legs=(
            leg_view(arr="08:40"),
            leg_view("08:30", board="UT", alight="RTD", reported=False, arr="09:00"),
            leg_view("08:50", board="UT", alight="RTD", planned_arr="09:10", progress=riding),
            leg_view("09:00", state="delayed", delay=4, reported=False),
            leg_view("09:10", state="cancelled", reported=False),
        ),
        retained=((leg_view("08:40", progress=riding),),),
        transfers=(),
        health=BoardHealth(True, at("07:50"), 1, False),
    )
    lines = render_status(view(o), True).splitlines()
    for want in (
        "  08:12 AAA to UT: on time per NS",
        "  08:30 UT to RTD: on time per NS, no reported departure time",
        "  08:50 UT to RTD: on time per NS, no reported arrival time",
        "  09:00 AAA to UT: delayed, +4 min, no reported departure time, no reported arrival time",
        "  09:10 AAA to UT: cancelled",
        "  if you are on the 08:40: 08:40 AAA to UT: on time per NS, no reported arrival time",
        "  NS data: fresh, last poll failed",
    ):
        assert want in lines
    assert not any(line.endswith(": ok") or ": ok," in line for line in lines)


def test_refusal_text_never_reaches_mcp():  # Review Focus 5
    v = view(occ(), refusal="profiles.p1.traveller: expected a name, got 'test-ns-key-0001'")
    assert "test-ns-key-0001" in render_status(v, True)  # Telegram, unchanged
    for req in ({"tool": "get_status", "profile": "p1"}, NEXT, WORKS):
        r = answer(v, True, req)
        assert "test-ns-key-0001" not in str(r)
        assert "Profile file refused: " + REFUSAL_HIDDEN in r["text"].splitlines()


@pytest.mark.parametrize("req, error", [
    (None, "Bad request."), ([], "Bad request."), ({"tool": "get_status"}, "Bad request."),
    ({"tool": "get_status", "profile": 5}, "Bad request."),
    ({"tool": "choose", "profile": "p1"}, "Unknown tool."),
    ({"tool": ["get_status"], "profile": "p1"}, "Unknown tool."),
    ({"tool": "get_next_connections", "profile": "p1", "direction": "sideways"},
     "Direction must be outbound or return."),
    ({"tool": "get_next_connections", "profile": "p1"}, "Direction must be outbound or return."),
])  # fmt: skip
def test_bad_requests_get_fixed_errors(req, error):  # Review Focus 4
    assert answer(view(occ()), True, req) == {"error": error}
