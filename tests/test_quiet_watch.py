"""Q9 quiet early watch: locked from the window start, only unusual alerts until the active point
(08:12, buffer 10: active at 07:47)."""

from dataclasses import replace

from builders import make_leg, planned_at_alight
from lifecycle_fixtures import (
    DIRECT,
    L1,
    L1X,
    L2,
    LATER,
    T_FULL,
    ans,
    at,
    conn,
    ctx,
    journey,
    numbers,
    occ,
    poll,
    usual_locked,
    with_track,
)

from train_commute_companion.lifecycle import queries_needed, quiet_watch
from train_commute_companion.model import Config, Ctx, EventKind, Severity
from train_commute_companion.revisions import effective_profile, same_route


def with_lead(c: Ctx, minutes: int) -> Ctx:
    p = c.profile
    return replace(c, profile=replace(p, outbound=replace(p.outbound, active_lead_min=minutes)))


def direct(delay: int, crowd: str | None = None):
    leg = make_leg(
        "AAA", "RTD", "08:12", "09:24", number="1234", stops=["BBB", "UT"],
        delay_dep=delay, delay_arr=delay,
    )  # fmt: skip
    return replace(leg, crowd_forecast=crowd) if crowd else leg


def direct_poll(s, when: str, delay: int, crowd: str | None = None):
    return poll(s, ctx(at(when), DIRECT), ans("AAA", conn(direct(delay, crowd)), when=when))


def kinds_of(t) -> list[EventKind]:
    return [e.kind for e in t.events]


def test_quiet_watch_ends_lead_minutes_before_departure():
    none = occ(None)
    assert quiet_watch(none, ctx(at("07:46"))) and not quiet_watch(none, ctx(at("07:47")))
    assert quiet_watch(none, with_lead(ctx(at("07:31")), 40))
    assert not quiet_watch(none, with_lead(ctx(at("07:32")), 40))
    later = occ(journey(LATER.legs))  # a relock to a later train keeps the usual's point
    assert quiet_watch(later, ctx(at("07:46"))) and not quiet_watch(later, ctx(at("07:47")))
    early = occ(journey([make_leg("AAA", "UT", "07:42", "08:10", number="1100"), L2]))
    assert quiet_watch(early, ctx(at("07:16"))) and not quiet_watch(early, ctx(at("07:17")))


def test_quiet_holds_small_delay_and_crowd_then_states_them_at_active_point():
    s = occ(journey([T_FULL]))
    t = direct_poll(s, "07:30", 3, "high")
    assert t.events == ()
    assert not any(k.endswith("#d") for k in t.state.alert_keys)
    t = direct_poll(t.state, "07:48", 3, "high")
    delayed = [e for e in t.events if e.kind is EventKind.DELAYED]
    assert len(delayed) == 1 and delayed[0].key.endswith("|stated+3,3")
    assert EventKind.CROWD_CHANGE in kinds_of(t)


def test_quiet_passes_delay_of_five():
    t = direct_poll(occ(journey([T_FULL])), "07:30", 5)
    (e,) = t.events
    assert e.kind is EventKind.DELAYED and e.severity is Severity.SILENT
    assert e.key.endswith("|stated+5,5")


def test_quiet_passes_changes_to_a_told_delay():
    t = direct_poll(occ(journey([T_FULL])), "07:30", 7)
    assert kinds_of(t) == [EventKind.DELAYED]
    t = direct_poll(t.state, "07:34", 4)
    assert len(t.events) == 1 and t.events[0].key.endswith("|stated+4,4")
    t = direct_poll(t.state, "07:38", 0)
    assert kinds_of(t) == [EventKind.BACK_TO_NORMAL]


def test_quiet_recovery_of_an_untold_delay_says_nothing():
    t = direct_poll(occ(journey([T_FULL])), "07:30", 3)
    assert direct_poll(t.state, "07:34", 0).events == ()


def test_quiet_passes_cancellation_platform_and_route_change():
    c = ctx(at("07:30"), DIRECT)
    t = poll(occ(journey([T_FULL])), c, ans("AAA", conn(with_track(T_FULL, "1", "5"))))
    assert [(e.kind, e.severity) for e in t.events] == [
        (EventKind.PLATFORM_CHANGE, Severity.SILENT)
    ]
    t = poll(usual_locked(), ctx(at("07:30")), ans("AAA", conn(L1X, L2), LATER))
    assert (EventKind.CANCELLED, Severity.LOUD) in [(e.kind, e.severity) for e in t.events]
    assert numbers(t.state) == ["1300", "6666"]
    onward = (
        make_leg("UT", "BBB", "08:52", "09:00", number="3100"),
        make_leg("BBB", "RTD", "09:10", "09:40", number="3200"),
    )
    t = poll(usual_locked(), ctx(at("07:30")), ans("AAA", conn(L1, *onward)))
    assert kinds_of(t) == [EventKind.ROUTE_CHANGED]  # the 08:50 gone from it is held (MISSING)


def test_quiet_holds_at_risk_missing_and_unclear():
    tight = (ans("AAA", conn(L1, L2)),)  # 10 min at UT against 9 needed: at risk
    assert poll(usual_locked(), ctx(at("07:30"), ut_min=9), *tight).events == ()
    t = poll(usual_locked(), ctx(at("07:48"), ut_min=9), *tight)
    assert EventKind.TRANSFER_AT_RISK in kinds_of(t)
    assert poll(usual_locked(), ctx(at("07:30")), ans("AAA")).events == ()
    assert EventKind.MISSING in kinds_of(poll(usual_locked(), ctx(at("07:48")), ans("AAA")))
    split = ans("AAA", conn(T_FULL), conn(planned_at_alight(T_FULL)))
    t = poll(occ(journey([T_FULL])), ctx(at("07:30")), split)
    assert t.events == () and not any(k.startswith("unclear#") for k in t.state.alert_keys)
    t = poll(t.state, ctx(at("07:48")), split)
    assert EventKind.DATA_UNCLEAR in kinds_of(t)


def test_quiet_asks_only_the_locked_first_departure():
    s = occ(journey([L1, L2]))
    quiet = queries_needed(s, ctx(at("07:30")))
    assert [(q.origin, q.at) for q in quiet] == [("AAA", at("08:12"))]
    active = queries_needed(s, ctx(at("07:48")))
    assert [(q.origin, q.at) for q in active] == [("AAA", at("08:12")), ("UT", at("08:50"))]


def test_active_lead_is_a_setting_not_a_route_change():
    old = ctx(at("07:50")).profile
    new = with_lead(ctx(at("07:50")), 40).profile
    assert same_route(old, new)  # rule 10: applies to a running occurrence
    effective = effective_profile({1: Config(1, (old,))}, Config(2, (new,)), usual_locked())
    assert effective is not None and effective.outbound.active_lead_min == 40


def test_window_start_relock_offers_no_train_before_the_active_point():  # Q9 review
    from location_fixtures import event

    from train_commute_companion.lifecycle import on_location
    from train_commute_companion.lifecycle_end import on_tick

    early = conn(
        make_leg("AAA", "UT", "07:42", "08:10", number="1100"),
        make_leg("UT", "RTD", "08:20", "08:54", number="2200"),
    )
    late = conn(
        make_leg("AAA", "UT", "08:42", "09:10", number="1500"),
        make_leg("UT", "RTD", "09:20", "09:54", number="2500"),
    )

    def answer(when: str):
        return ans("AAA", early, conn(L1X, L2), late, when=when)

    # 07:12 window start, usual 08:12 cancelled: no 07:42 the user never planned (active 07:47)
    t = on_tick(occ(None), ctx(at("07:12")), answer("08:12"))
    assert numbers(t.state)[0] == "1500"
    assert "08:42" in t.events[0].text
    # so a phone at rest at home before 07:42 gives no loud "missed the 07:42"
    s = on_location(t.state, ctx(at("07:37")), event("home", "still", "07:37"), answer("07:37"))
    t = poll(s.state, ctx(at("07:43")), answer("07:43"))
    assert EventKind.MISSED_FIRST_TRAIN not in kinds_of(t)
