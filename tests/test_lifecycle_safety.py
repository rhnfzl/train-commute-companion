"""Lifecycle safety regressions: warnings never wait for permission to relock, progress and
evidence per leg, retained remainders, generation and termination (7.2, 7.4, 7.7, 7.12)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from builders import AMS, make_leg
from test_lifecycle_end import accepted_first, arrived, report, retained_of
from test_lifecycle_follow import (
    DIRECT,
    FROM_BBB,
    L1,
    L2,
    L2X,
    LATER,
    R2,
    RIDE,
    T_BBB,
    T_FULL,
    ans,
    at,
    conn,
    ctx,
    cut_at_ut,
    journey,
    kinds,
    numbers,
    occ,
    poll,
    usual_locked,
)

from train_commute_companion.lifecycle import (
    Choose,
    DirectionState,
    Leaving,
    LegState,
    LockSource,
    NotTravelling,
    apply_action,
    ends,
    on_location,
    on_poll,
    on_tick,
    queries_needed,
    target_occurrence,
    write_lock,
)
from train_commute_companion.location import LocationEvent
from train_commute_companion.model import EventKind, Severity
from train_commute_companion.providers.base import Query

DONE = (LegState.DONE,)
LATE_L1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=12)  # 08:52 at UT


def test_ridden_direct_train_cancelled_is_warned_and_kept():
    s = occ(journey([T_FULL], RIDE))
    gone = make_leg(
        "AAA", "RTD", "08:12", "09:24", number="1234", stops=["BBB", "UT"], cancelled=True
    )
    t = poll(s, ctx(at("08:20"), DIRECT), ans("AAA", conn(gone)))
    assert (EventKind.CANCELLED, Severity.LOUD) in kinds(t)
    assert numbers(t.state) == ["1234"] and t.state.lock is not None
    assert t.state.lock.legs[0].state is LegState.RIDING  # a train the user may be on stays


def test_transfer_broken_after_onward_departed_is_warned_with_next_connection():
    s = occ(journey([L1, L2], RIDE))
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=15)  # 08:55
    t = poll(s, ctx(at("08:52")), ans("AAA", conn(late, L2)), ans("UT", conn(L2), conn(R2)))
    broken = [e for e in t.events if e.kind is EventKind.TRANSFER_BROKEN]
    assert len(broken) == 1 and broken[0].severity is Severity.LOUD
    assert "Next good connection: 09:05" in broken[0].text
    assert numbers(t.state) == ["1234", "5678"]  # the onward train may be ridden: not moved


def test_uncertain_transfer_leg_is_at_risk():  # 6.1, 7.3
    twin = make_leg("UT", "RTD", "08:50", "09:30", number="7777")
    s = occ(journey([L1, L2], RIDE))
    t = poll(s, ctx(at("08:20")), ans("AAA", conn(L1, L2)), ans("UT", conn(L2), conn(twin)))
    assert kinds(t) == [(EventKind.TRANSFER_AT_RISK, Severity.LOUD)]


def passing(leg):  # the train runs through the stop: it reports a time but does not call
    return replace(leg, stops=(*leg.stops[:-1], replace(leg.stops[-1], passing=True)))


def test_passing_alighting_stop_is_never_accepted_and_reopens_an_accepted_leg():
    s = occ(accepted_first(journey([L1, L2], (LegState.DONE, LegState.RIDING))))
    s = poll(s, ctx(at("09:29")), ans("AAA", conn(L1, L2), when="09:29")).state
    t = poll(s, ctx(at("09:33")), ans("AAA", conn(L1, passing(L2)), when="09:33"))
    assert t.state.lock is not None and t.state.lock.legs[1].state is not LegState.DONE
    assert any(sev is Severity.LOUD for _, sev in kinds(t))
    via_bbb = make_leg("UT", "RTD", "08:50", "09:24", number="5678", stops=["BBB"])
    t = poll(arrived(), ctx(at("09:40")), ans("AAA", conn(L1, passing(via_bbb)), when="09:40"))
    assert t.state.lock is not None and t.state.lock.legs[1].state is LegState.RIDING
    assert (EventKind.CUT_SHORT, Severity.LOUD) in kinds(t)


def test_done_incoming_leg_change_is_still_checked():  # rule 6, lifecycle 26
    s = occ(accepted_first(journey([L1, L2], DONE)))
    t = poll(s, ctx(at("08:42"), ut_min=9), ans("AAA", conn(L1, L2)), ans("UT", conn(L2)))
    assert kinds(t) == [(EventKind.TRANSFER_AT_RISK, Severity.LOUD)]


def test_writer_keeps_updates_to_done_legs():
    s = occ(accepted_first(journey([L1, L2], DONE)))
    assert s.lock is not None
    moved = replace(s.lock.legs[0], served_endpoint="BBB", alight_stop="BBB")
    new = replace(s.lock, legs=(moved, s.lock.legs[1]), source=LockSource.RELOCK)
    w = write_lock(s, new, expect=s.lock, now=at("08:42"))
    assert w is not None and w.lock is not None and w.lock.legs[0].alight_stop == "BBB"


def test_accepted_cut_short_leg_extended_before_recovery_boarding_reopens():
    j = journey([T_FULL, FROM_BBB], DONE)
    cut = replace(
        j.legs[0],
        served_endpoint="BBB",
        alight_stop="BBB",
        accepted_at=at("08:19"),
        accept_value=at("08:12"),
    )
    s = occ(replace(j, legs=(cut, j.legs[1])))
    t = poll(s, ctx(at("08:20"), DIRECT), ans("AAA", conn(T_FULL)), ans("BBB", conn(FROM_BBB)))
    assert numbers(t.state) == ["1234"]
    assert t.state.lock is not None
    x = t.state.lock.legs[0]
    assert (x.alight_stop, x.state, x.accepted_at) == ("RTD", LegState.RIDING, None)
    assert kinds(t) == [(EventKind.RESTORED, Severity.SILENT)]


def test_delayed_recovery_train_does_not_freeze_the_alighting_stop():
    s = cut_at_ut(LegState.PENDING)
    late_rec = make_leg("UT", "RTD", "08:30", "09:00", number="8888", delay_dep=30)
    answers = (ans("AAA", conn(T_BBB)), ans("BBB", conn(FROM_BBB)), ans("UT", conn(late_rec)))
    t = poll(s, ctx(at("08:31"), DIRECT), *answers)
    assert numbers(t.state) == ["1234", "9999"]
    assert t.state.lock is not None
    x = t.state.lock.legs[0]
    assert (x.alight_stop, x.alight_frozen) == ("BBB", False)
    assert (EventKind.CUT_SHORT, Severity.LOUD) in kinds(t)


def test_choice_after_done_leg_checks_the_change():  # rule 1
    s = occ(accepted_first(journey([L1, L2], DONE)))
    quick = conn(make_leg("UT", "RTD", "08:52", "09:30", number="4444"))  # 12 min after 08:40
    c = ctx(at("08:42"), ut_min=15)
    seen = (ans("AAA", conn(L1, L2)), ans("UT", quick))
    t = apply_action(s, c, Choose(None, quick, seen))
    assert t.state is s and [e.kind for e in t.events] == [EventKind.CHOOSE_REFUSED]
    assert "transfer at UT" in t.events[0].text
    # no evidence of the done leg: refused, never accepted unchecked
    t = apply_action(s, ctx(at("08:42")), Choose(None, quick, (ans("UT", quick),)))
    assert t.state is s and "not in NS data" in t.events[0].text


def test_relock_never_skips_the_change_from_an_unseen_kept_leg():
    s = occ(journey([L1, L2], RIDE))
    t = poll(s, ctx(at("08:20")), ans("UT", conn(L2X), conn(R2)))
    assert numbers(t.state) == ["1234", "5678"]
    cancelled = [e for e in t.events if e.kind is EventKind.CANCELLED]
    assert len(cancelled) == 1 and "not in NS data" in cancelled[0].text


def test_departure_moves_leg_and_direction_to_riding_with_next_connection():
    s = usual_locked()
    t = poll(s, ctx(at("08:13")), ans("AAA", conn(L1, L2), LATER, when="08:13"))
    assert t.state.lock is not None and t.state.lock.legs[0].state is LegState.RIDING
    assert t.state.direction_state is DirectionState.RIDING
    assert t.state.generation == s.generation + 1
    assert kinds(t) == [(EventKind.NEXT_CONNECTION, Severity.SILENT)]
    assert "If you are not on it, next good connection: 08:25" in t.events[0].text
    nxt = replace(occ(None), key=replace(s.key, service_date=date(2026, 10, 28)))
    nxt = replace(nxt, direction_state=DirectionState.IDLE)
    assert target_occurrence([t.state, nxt], Leaving(None, at("08:14"))) is nxt
    # a delayed train still at the platform is boarding, not riding
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=5)
    t = poll(s, ctx(at("08:13")), ans("AAA", conn(late, L2), when="08:13"))
    assert t.state.lock is not None and t.state.lock.legs[0].state is LegState.BOARDING
    assert t.state.direction_state is DirectionState.LOCKED


def test_retained_follow_keeps_observed_progress_and_asks_the_recovery_time():
    s = occ(journey(LATER.legs), retained=(journey([L1, L2], RIDE),))
    c = ctx(at("08:20"))
    t = poll(s, c, ans("AAA", conn(LATE_L1, L2), LATER), ans("UT", conn(L2), when="08:50"))
    assert any(e.kind is EventKind.TRANSFER_BROKEN for e in t.events)
    assert t.state.retained[0].legs[0].onward_from == at("08:56")
    assert Query("UT", "RTD", at("08:56")) in queries_needed(t.state, c)


def test_retained_remainder_is_asked_for_without_a_lock():
    s = occ(None, retained=(journey([L1, L2], RIDE),))
    assert queries_needed(s, ctx(at("08:20"))) == [
        Query("AAA", "RTD", at("08:12")),
        Query("UT", "RTD", at("08:50")),
    ]


def test_auto_lock_keeps_a_retained_remainder_with_other_onward_legs():
    x8888 = make_leg("UT", "RTD", "08:50", "09:24", number="8888")
    s = occ(None, retained=(journey([L1, L2], RIDE),))
    t = on_tick(s, ctx(at("08:20")), ans("AAA", conn(L1, x8888), when="08:12"))
    assert numbers(t.state) == ["1234", "8888"]
    assert [[x.train.train_number for x in r.legs] for r in t.state.retained] == [["1234", "5678"]]
    # the same journey is merged, keeping its progress
    t = on_tick(s, ctx(at("08:20")), ans("AAA", conn(L1, L2), when="08:12"))
    assert t.state.retained == () and numbers(t.state) == ["1234", "5678"]
    assert t.state.lock is not None and t.state.lock.legs[0].state is LegState.RIDING


def test_done_main_journey_with_retained_remainder_takes_actions_and_location():
    s = replace(retained_of(journey([L1, L2], RIDE)), direction_state=DirectionState.DONE)
    assert target_occurrence([s], NotTravelling(None, False)) is s
    assert "RTD" in report(s, "RTD", "09:31").zone_reports


def test_progress_changes_raise_the_generation():  # rule 21
    s = occ(accepted_first(journey([L1, L2], (LegState.DONE, LegState.RIDING))))
    s = poll(s, ctx(at("09:29")), ans("AAA", conn(L1, L2), when="09:29")).state
    t = poll(s, ctx(at("09:33")), ans("AAA", conn(L1, L2), when="09:33"))
    assert t.state.lock is not None and t.state.lock.legs[1].state is LegState.DONE
    assert t.state.generation == s.generation + 1
    end = on_tick(arrived(), ctx(at("10:03")))
    assert end.state.direction_state is DirectionState.DONE
    assert end.state.generation == arrived().generation + 1
    # a missed train with nothing to relock to is still a change
    e = LocationEvent("AAA", "report", at("08:13"), at("08:13"))
    m = on_location(usual_locked(), ctx(at("08:14")), e, ans("AAA", conn(L1, L2), when="08:14"))
    assert m.state.lock is not None and m.state.lock.legs[0].state is LegState.MISSED
    assert m.state.generation == usual_locked().generation + 1
    # the first platform report waives the buffer for the same station lock
    st = replace(usual_locked(), lock=replace(usual_locked().lock, source=LockSource.STATION))
    e = LocationEvent("AAA", "enter", at("08:00"), at("08:00"))
    a = on_location(st, ctx(at("08:00")), e, ans("AAA", conn(L1, L2), when="08:00"))
    assert a.state.at_station and a.state.generation == st.generation + 1


def test_newer_cancelled_copy_beats_an_older_running_one():
    s = usual_locked()
    old = ans("AAA", conn(L1, L2), when="07:49")
    new = ans(
        "AAA",
        conn(make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True), L2),
        LATER,
        when="07:50",
    )
    t = poll(s, ctx(at("07:50")), old, new)
    assert kinds(t)[0] == (EventKind.CANCELLED, Severity.LOUD)
    assert numbers(t.state) == ["1300", "6666"]


def test_acceptance_uses_the_time_of_the_answer_that_showed_the_leg():
    from test_lifecycle_follow import B1

    s = occ(accepted_first(journey([L1, L2], (LegState.DONE, LegState.RIDING))))
    for seen, other in (("09:29", "09:30"), ("09:32", "09:34")):  # 3 min apart
        s = poll(
            s, ctx(at(other)), ans("AAA", conn(L1, L2), when=seen), ans("BBB", conn(B1), when=other)
        ).state
    assert s.lock is not None and s.lock.legs[1].state is LegState.RIDING


def test_cancelled_onward_waits_for_a_search_from_the_reachable_time():  # 7.4
    s = occ(journey([L1, L2], RIDE))
    c = ctx(at("08:20"))
    t = poll(s, c, ans("AAA", conn(LATE_L1, L2X)), ans("UT", conn(L2X), when="08:50"))
    assert kinds(t) == [(EventKind.CANCELLED, Severity.LOUD)]  # 08:50 cannot prove none
    assert Query("UT", "RTD", at("08:56")) in queries_needed(t.state, c)
    t = poll(t.state, c, ans("AAA", conn(LATE_L1, L2X)), ans("UT", when="08:56"))
    assert kinds(t)[-1] == (EventKind.NO_CONNECTION, Severity.LOUD)


def test_cut_short_recovery_is_asked_from_the_reachable_time():
    s = occ(journey([T_FULL], RIDE))
    c = ctx(at("08:14"), DIRECT)
    t = poll(s, c, ans("AAA", conn(T_BBB)))
    assert kinds(t) == [(EventKind.CUT_SHORT, Severity.LOUD)]
    assert Query("BBB", "RTD", at("08:16")) in queries_needed(t.state, c)  # 08:12 + 4


def test_choosing_a_retained_journey_does_not_take_done_legs_of_the_lock():
    s = occ(journey(LATER.legs, DONE), retained=(journey([L1, L2], RIDE),))
    pick = conn(L1, R2)
    t = apply_action(s, ctx(at("08:20")), Choose(None, pick, (ans("AAA", pick),)))
    assert t.events == () and numbers(t.state) == ["1234", "6666"]


def test_same_minute_alternative_is_never_recommended():  # 6.1
    from builders import make_connection
    from test_lifecycle_lock import LATER as LOCK_LATER
    from test_lifecycle_lock import USUAL as LOCK_USUAL
    from test_lifecycle_lock import answer, fresh
    from test_lifecycle_lock import ctx as lock_ctx

    from train_commute_companion.lifecycle import auto_lock

    twin = make_connection(
        [make_leg("AAA", "UT", "08:12", "08:58", number="7777", delay_dep=23)]  # leaves 08:35
    )
    t = auto_lock(fresh(), lock_ctx(at("08:20")), answer(LOCK_USUAL, twin, LOCK_LATER))
    note = [e for e in t.events if e.kind is EventKind.NEXT_CONNECTION]
    assert len(note) == 1 and "08:44" in note[0].text
    refused = apply_action(
        fresh(), lock_ctx(at("08:00")), Choose(None, twin, (answer(LOCK_USUAL, twin),))
    )
    assert [e.kind for e in refused.events] == [EventKind.CHOOSE_REFUSED]
    assert "uncertain" in refused.events[0].text


def test_auto_lock_of_a_cancelled_usual_relocks_at_once_with_the_warning():  # 7.2
    from builders import make_connection
    from test_lifecycle_lock import LATER as LOCK_LATER
    from test_lifecycle_lock import answer, fresh
    from test_lifecycle_lock import ctx as lock_ctx

    from train_commute_companion.lifecycle import auto_lock

    gone = make_connection([make_leg("AAA", "UT", "08:12", "08:46", number="1234", cancelled=True)])
    t = auto_lock(fresh(), lock_ctx(at("07:50")), answer(gone, LOCK_LATER))
    assert [(e.kind, e.severity) for e in t.events] == [(EventKind.CANCELLED, Severity.LOUD)]
    assert "08:44" in t.events[0].text
    assert t.state.lock is not None and t.state.lock.legs[0].train.train_number == "5678"
    assert [h.train.train_number for h in t.state.cancellation_history] == ["1234"]


def fold(hhmm: str, f: int) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, 25, h, m, fold=f, tzinfo=AMS)


def test_dst_fold_queries_and_onward_time_use_instants():  # 7.13
    night = date(2026, 10, 24)  # builder: before 04:00 is the next day, the autumn change
    first = make_leg("AAA", "UT", "02:30", "02:50", number="1111", day=night)
    second = make_leg(
        "AAA", "UT", "02:30", "02:50", number="2222", day=night, fold_dep=1, fold_arr=1
    )
    s = occ(journey([first]), retained=(journey([second], RIDE),))
    qs = queries_needed(s, ctx(at("08:00")))
    assert [(q.origin, q.at.astimezone(UTC)) for q in qs] == [
        ("AAA", fold("02:30", 0).astimezone(UTC)),
        ("AAA", fold("02:30", 1).astimezone(UTC)),
    ]
    inc = make_leg("AAA", "UT", "01:40", "02:05", number="1234", day=night, fold_arr=1)
    out = make_leg("UT", "RTD", "02:20", "02:50", number="5678", day=night, fold_dep=1, fold_arr=1)
    s = occ(journey([inc, out], RIDE))
    now = fold("01:50", 0)
    t = on_poll(s, ctx(now), board_at(s, now, conn(inc, out)))
    assert t.state.lock is not None
    onward = t.state.lock.legs[0].onward_from
    assert onward is not None
    assert onward.astimezone(UTC) == fold("02:05", 1).astimezone(UTC) + timedelta(minutes=4)


def board_at(s, now, *conns):
    from train_commute_companion.health import PollRecord
    from train_commute_companion.lifecycle import Board
    from train_commute_companion.providers.base import ProviderAnswer

    q = Query("AAA", "RTD", now)
    return Board((ProviderAnswer(q, conns, now),), (PollRecord(now, True, None),), s.generation)


def test_late_acceptance_stops_at_the_150_minute_limit():  # rules 6, 7
    j = journey([L1, L2], (LegState.DONE, LegState.DONE))
    last = replace(
        j.legs[1], accepted_at=at("11:50"), accept_value=at("11:45"), watch_until=at("12:20")
    )
    first = replace(j.legs[0], watch_until=at("12:20"))
    s = occ(replace(j, legs=(first, last)))
    assert on_tick(s, ctx(at("11:53"))).events == ()  # 09:24 + 150 min is 11:54
    t = on_tick(s, ctx(at("11:54")))
    assert kinds(t) == [(EventKind.STOPPED_FOLLOWING, Severity.LOUD)]
    assert t.state.direction_state is DirectionState.DONE and ends(t.state, at("11:54"), 0)
    # a retained remainder too
    r = retained_of(replace(j, legs=(first, last)))
    t = on_tick(r, ctx(at("11:54")))
    assert [e.kind for e in t.events] == [EventKind.STOPPED_FOLLOWING] and t.state.retained == ()
