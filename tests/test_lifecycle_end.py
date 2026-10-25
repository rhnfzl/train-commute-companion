from dataclasses import replace

from builders import make_leg
from test_lifecycle_follow import (
    L1,
    L1X,
    L2,
    LATER,
    RIDE,
    ans,
    at,
    conn,
    ctx,
    journey,
    kinds,
    occ,
    poll,
)

from train_commute_companion.lifecycle import (
    DirectionState,
    Journey,
    LegState,
    NotTravelling,
    OccurrenceState,
    apply_action,
    ends,
    on_location,
    on_tick,
)
from train_commute_companion.location import LocationEvent
from train_commute_companion.model import EventKind, Severity

USUAL = conn(L1, L2)
DONE_RIDE = (LegState.DONE, LegState.RIDING)


def accepted_first(j: Journey) -> Journey:
    x = replace(j.legs[0], state=LegState.DONE, accepted_at=at("08:50"), accept_value=at("08:40"))
    return replace(j, legs=(x, *j.legs[1:]))


def polls(s: OccurrenceState, *whens: str, c: object = USUAL) -> OccurrenceState:
    for w in whens:
        s = poll(s, ctx(at(w)), ans("AAA", c, when=w)).state  # type: ignore[arg-type]
    return s


def arrived() -> OccurrenceState:
    """The last leg (09:24) accepted at 09:33, watched until 10:03."""
    s = polls(occ(accepted_first(journey([L1, L2], DONE_RIDE))), "09:29", "09:33")
    assert s.lock is not None and s.lock.legs[-1].state is LegState.DONE
    return s


def report(s: OccurrenceState, zone: str, when: str) -> OccurrenceState:
    e = LocationEvent(zone, "report", at(when), at(when))
    return on_location(s, ctx(at(when)), e).state


def test_repeated_estimate_before_its_time_does_not_end_journey():
    s = polls(occ(journey([L1, L2], RIDE)), "09:00", "09:10", "09:20", "09:24", "09:26", "09:28")
    assert s.lock is not None
    assert s.lock.legs[0].state is LegState.DONE  # 08:40 seen at 09:00 and 09:10
    assert s.lock.legs[1].state is not LegState.DONE
    t = on_tick(s, ctx(at("10:30")))
    assert t.events == () and t.state.direction_state is not DirectionState.DONE
    assert not ends(t.state, at("10:30"), 0)


def test_acceptance_needs_two_answers_4_min_apart_5_min_after_arrival():
    s = occ(accepted_first(journey([L1, L2], DONE_RIDE)))
    s = polls(s, "09:28")  # 4 min after the arrival: not yet
    assert s.lock is not None and s.lock.legs[1].accept_first_seen is None
    s = polls(s, "09:29", "09:32")  # 3 min apart
    assert s.lock is not None and s.lock.legs[1].state is LegState.RIDING
    s = polls(s, "09:33")
    assert s.lock is not None
    last = s.lock.legs[1]
    assert last.state is LegState.DONE and last.accept_value == at("09:24")
    assert [x.watch_until for x in s.lock.legs] == [at("10:03")] * 2  # every watch ends with it


def test_acceptance_restarts_when_the_arrival_changes():
    moved = conn(L1, make_leg("UT", "RTD", "08:50", "09:24", number="5678", delay_arr=1))
    s = polls(occ(accepted_first(journey([L1, L2], DONE_RIDE))), "09:29")
    s = polls(s, "09:34", c=moved)  # 09:25 now: a new first answer
    s = polls(s, "09:36", c=moved)
    assert s.lock is not None and s.lock.legs[1].state is LegState.RIDING
    s = polls(s, "09:38", c=moved)
    assert s.lock is not None and s.lock.legs[1].state is LegState.DONE


def test_accepted_arrival_reopens_when_moved_later():
    late = conn(L1, make_leg("UT", "RTD", "08:50", "09:24", number="5678", delay_arr=20))
    s = arrived()
    t = poll(s, ctx(at("09:40")), ans("AAA", late, when="09:40"))
    assert t.state.lock is not None
    last = t.state.lock.legs[1]
    assert last.state is LegState.RIDING and last.watch_until is None
    assert (EventKind.DELAYED, Severity.SILENT) in kinds(t)
    assert on_tick(t.state, ctx(at("10:03"))).events == ()  # watched again, not ended


def test_last_leg_without_location_evidence_ends_with_loud_stopped_following():  # OQ4 = A
    s = arrived()
    t = on_tick(s, ctx(at("10:02")))
    assert t.events == () and not ends(t.state, at("10:02"), 0)
    t = on_tick(s, ctx(at("10:03")))
    assert kinds(t) == [(EventKind.STOPPED_FOLLOWING, Severity.LOUD)]
    assert "arriving at 09:24" in t.events[0].text
    assert t.state.direction_state is DirectionState.DONE
    assert not ends(t.state, at("10:03"), 1)  # the warning is still pending
    assert ends(t.state, at("10:03"), 0)
    assert on_tick(t.state, ctx(at("10:05"))).events == ()  # sent once


def test_destination_report_after_accepted_arrival_ends_silently():
    for zone in ("RTD", "work"):
        s = report(arrived(), zone, "09:31")
        t = on_tick(s, ctx(at("10:03")))
        assert t.events == () and t.state.direction_state is DirectionState.DONE
        assert ends(t.state, at("10:03"), 0)


def test_destination_report_before_arrival_does_not_end():
    s = report(occ(accepted_first(journey([L1, L2], DONE_RIDE))), "RTD", "09:20")
    assert not ends(s, at("09:21"), 0)
    s = polls(s, "09:29", "09:33")
    t = on_tick(s, ctx(at("10:03")))
    assert kinds(t) == [(EventKind.STOPPED_FOLLOWING, Severity.LOUD)]


def test_intermediate_leg_cancelled_after_acceptance_reopens():
    s = occ(accepted_first(journey([L1, L2], DONE_RIDE)))
    t = poll(s, ctx(at("09:00")), ans("AAA", conn(L1X, L2), when="09:00"))
    assert t.state.lock is not None
    assert t.state.lock.legs[0].state is LegState.RIDING
    assert t.state.lock.legs[0].accepted_at is None
    assert (EventKind.CANCELLED, Severity.LOUD) in kinds(t)


def retained_of(j: Journey) -> OccurrenceState:
    """The lock moved to the 08:25; the user may still be on `j`."""
    return occ(journey(list(LATER.legs)), retained=(j,))


def test_retained_watch_ends_without_evidence_sends_own_stopped_following_and_lock_continues():
    r = journey([L1, L2], DONE_RIDE)
    s = polls(retained_of(r), "09:29", "09:33", c=conn(L1, L2))
    assert s.retained[0].legs[-1].state is LegState.DONE
    t = on_tick(s, ctx(at("10:03")))
    assert kinds(t) == [(EventKind.STOPPED_FOLLOWING, Severity.LOUD)]
    assert "if you are on the 08:12: NS showed it arriving at 09:24" in t.events[0].text
    assert t.state.retained == () and t.state.lock == s.lock
    assert t.state.direction_state is not DirectionState.DONE


def test_retained_150_min_limit_does_not_end_new_lock():
    r = journey([L1, L2], (LegState.MISSED,))
    s = retained_of(r)
    assert on_tick(s, ctx(at("11:53"))).events == ()
    t = on_tick(s, ctx(at("11:54")))  # 09:24 + 150 min
    assert kinds(t) == [(EventKind.STOPPED_FOLLOWING, Severity.LOUD)]
    assert "if you are on the 08:12: I stopped following it" in t.events[0].text
    assert t.state.retained == () and t.state.lock == s.lock
    assert t.state.direction_state is not DirectionState.DONE
    t = on_tick(t.state, ctx(at("12:09")))  # the lock's own limit: 09:39 + 150 min
    assert "I stopped following your journey" in t.events[0].text
    assert t.state.direction_state is DirectionState.DONE


def test_occurrence_not_archived_while_retained_remainder_exists():
    s = retained_of(journey([L1, L2], (LegState.MISSED,)))
    s = replace(s, lock=journey(list(LATER.legs), DONE_RIDE))
    s = polls(s, "09:44", "09:48", c=LATER)
    t = on_tick(s, ctx(at("10:18")))  # the lock's watch ended
    assert t.state.direction_state is DirectionState.DONE and t.state.retained != ()
    assert not ends(t.state, at("10:18"), 0)
    t = on_tick(t.state, ctx(at("11:54")))  # the retained limit
    assert t.state.retained == ()
    assert ends(t.state, at("11:54"), 0)


def test_window_closed_with_no_lock_stops_following():
    s = occ(None)
    assert on_tick(s, ctx(at("10:11"))).events == ()
    t = on_tick(s, ctx(at("10:12")))
    assert kinds(t) == [(EventKind.STOPPED_FOLLOWING, Severity.LOUD)]
    assert ends(t.state, at("10:12"), 0)


def test_not_travelling_ends_regardless_of_state():
    s = replace(occ(journey([L1, L2], RIDE)), not_travelling=True)
    assert ends(s, at("08:20"), 0)
    assert on_tick(s, ctx(at("12:00"))).events == ()


def test_not_travelling_ends_retained_remainder():  # retained end on NotTravelling (7.12 rule 7)
    s = retained_of(journey([L1, L2], (LegState.MISSED,)))
    t = apply_action(s, ctx(at("08:20")), NotTravelling(None, False))
    assert t.state.retained == () and ends(t.state, at("08:20"), 0)
