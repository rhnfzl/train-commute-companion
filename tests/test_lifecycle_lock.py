from dataclasses import replace
from datetime import date, datetime

from builders import AMS, make_connection, make_leg

from train_commute_companion.lifecycle import (
    Choose,
    Commitment,
    Ctx,
    DirectionState,
    Journey,
    Leaving,
    LegState,
    LockSource,
    NotTravelling,
    OccurrenceState,
    apply_action,
    auto_lock,
    catch_from_state,
    journey_of,
    target_occurrence,
    whole_day_keys,
    write_lock,
)
from train_commute_companion.model import (
    Connection,
    Direction,
    EventKind,
    OccurrenceKey,
    Severity,
)
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.schedule import dated_schedule

PROFILES = """
profiles:
  - id: "p1"
    name: "office_a"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "UT"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
"""
DAY = date(2026, 10, 27)


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=AMS)


def at(hhmm: str) -> datetime:
    return dt(f"2026-10-27 {hhmm}")


def prof():
    ps, errs = parse_profiles(PROFILES, None)
    assert errs == []
    return ps[0]


def ctx(now: datetime, day: date = DAY) -> Ctx:
    p = prof()
    return Ctx(p, Direction.OUTBOUND, dated_schedule(p, Direction.OUTBOUND, day, None), None, now)


def fresh(day: date = DAY) -> OccurrenceState:
    return OccurrenceState(
        key=ctx(at("07:12"), day).schedule.key,
        generation=0,
        route_revision=1,
        settings_revision=1,
        baseline_version=None,
        direction_state=DirectionState.IDLE,
        lock=None,
        retained=(),
        travelling=False,
        tap_at=None,
        at_station=False,
        errand_deadline=None,
        not_travelling=False,
        cancellation_history=(),
        missing_sent=False,
        all_good_evaluated=False,
    )


def direct(dep: str, arr: str, number: str = "1234", day: date = DAY) -> Connection:
    return make_connection([make_leg("AAA", "UT", dep, arr, number=number, day=day)])


def answer(*conns: Connection) -> ProviderAnswer:
    q = Query("AAA", "UT", at("07:12"))
    return ProviderAnswer(q, conns, q.at)


USUAL = direct("08:12", "08:46", "1234")
LATER = direct("08:44", "09:24", "5678")


def locked() -> OccurrenceState:
    return auto_lock(fresh(), ctx(at("07:50")), answer(USUAL, LATER)).state


def first_dep(s: OccurrenceState) -> datetime:
    assert s.lock is not None
    return s.lock.legs[0].train.planned_departure


def chosen() -> OccurrenceState:
    return apply_action(locked(), ctx(at("08:00")), Choose(None, LATER)).state


def test_choose_beats_auto_and_auto_cannot_overwrite_choose():
    s = chosen()
    assert s.lock is not None and s.lock.source is LockSource.CHOOSE
    assert first_dep(s) == at("08:44")
    auto = journey_of(USUAL, LockSource.AUTO, Commitment.INTENDED)
    for src in (LockSource.AUTO, LockSource.STATION, LockSource.LOCATION):
        assert write_lock(s, replace(auto, source=src), expect=None, now=at("08:00")) is None
        assert write_lock(s, replace(auto, source=src), expect=s.lock, now=at("08:00")) is None
    s2 = replace(s, lock=None)  # an auto lock run never touches an existing choice either
    assert auto_lock(s, ctx(at("08:00")), answer(USUAL)).state is s
    assert first_dep(auto_lock(s2, ctx(at("08:00")), answer(USUAL)).state) == at("08:12")
    for src in (LockSource.MISSED, LockSource.TIMEOUT):  # these override a choice
        moved = write_lock(s, replace(auto, source=src), expect=None, now=at("08:00"))
        assert moved is not None and first_dep(moved) == at("08:12")


def test_cas_rejects_stale_expect():
    s = locked()
    stale = journey_of(LATER, LockSource.AUTO, Commitment.INTENDED)
    relock = journey_of(LATER, LockSource.RELOCK, Commitment.INTENDED)
    assert write_lock(s, relock, expect=stale, now=at("08:00")) is None
    moved = write_lock(s, relock, expect=s.lock, now=at("08:00"))
    assert moved is not None and first_dep(moved) == at("08:44")


def test_relock_moves_chosen_journey_when_expect_matches():
    s = chosen()
    relock = journey_of(USUAL, LockSource.RELOCK, Commitment.INTENDED)
    assert (
        write_lock(s, relock, expect=None, now=at("08:00")) is None
    )  # no expect: the choice stands
    moved = write_lock(s, relock, expect=s.lock, now=at("08:00"))
    assert moved is not None and first_dep(moved) == at("08:12")
    assert moved.lock is not None and moved.lock.source is LockSource.RELOCK


def test_every_write_raises_generation():
    s0 = fresh()
    s1 = auto_lock(s0, ctx(at("07:50")), answer(USUAL, LATER)).state
    s2 = apply_action(s1, ctx(at("07:55")), Leaving(None, at("07:55"))).state
    s3 = apply_action(s2, ctx(at("08:00")), Choose(None, LATER)).state
    relock = journey_of(USUAL, LockSource.RELOCK, Commitment.COMMITTED)
    s4 = write_lock(s3, relock, expect=s3.lock, now=at("08:00"))
    assert s4 is not None
    s5 = apply_action(s4, ctx(at("08:01")), NotTravelling(None, False)).state
    assert [x.generation for x in (s0, s1, s2, s3, s4, s5)] == [0, 1, 2, 3, 4, 5]


def test_auto_lock_at_first_train_minus_buffer_minus_15():
    early = auto_lock(fresh(), ctx(at("07:46")), answer(USUAL, LATER))
    assert early.state.lock is None and early.events == ()
    t = auto_lock(fresh(), ctx(at("07:47")), answer(USUAL, LATER))  # 08:12 - 10 - 15
    assert t.state.lock is not None and first_dep(t.state) == at("08:12")
    assert t.state.lock.source is LockSource.AUTO
    assert t.state.lock.commitment is Commitment.INTENDED
    assert t.state.direction_state is DirectionState.LOCKED
    assert t.events == ()


def test_usual_missing_before_lock_is_loud_and_next_good_locked():
    t = auto_lock(fresh(), ctx(at("07:50")), answer(LATER))
    assert [(e.kind, e.severity) for e in t.events] == [(EventKind.MISSING, Severity.LOUD)]
    assert t.events[0].text == "Office A: your usual 08:12 is not in NS data."
    assert t.state.missing_sent and first_dep(t.state) == at("08:44")
    # once per occurrence: a later lock attempt without a lock does not say it again
    again = auto_lock(replace(t.state, lock=None), ctx(at("07:52")), answer(LATER))
    assert all(e.kind is not EventKind.MISSING for e in again.events)
    none = auto_lock(fresh(), ctx(at("07:50")), answer())
    assert [e.kind for e in none.events] == [EventKind.MISSING, EventKind.NO_CONNECTION]
    assert none.state.lock is None


def test_start_after_auto_lock_time_locks_at_once():
    t = auto_lock(fresh(), ctx(at("08:00")), answer(USUAL, LATER))
    assert first_dep(t.state) == at("08:12") and t.events == ()
    # usual already departed: still locked (the user may be on it), next good shown
    late = auto_lock(fresh(), ctx(at("08:20")), answer(USUAL, LATER))
    assert first_dep(late.state) == at("08:12")
    assert [e.kind for e in late.events] == [EventKind.NEXT_CONNECTION]
    assert "08:44" in late.events[0].text


def test_leaving_keeps_buffer():
    t = apply_action(
        locked(), ctx(now=dt("2026-10-27 07:55")), Leaving(None, dt("2026-10-27 07:55"))
    )
    assert catch_from_state(t.state, 10, dt("2026-10-27 07:55")).not_before == dt(
        "2026-10-27 08:05"
    )
    assert t.state.travelling and t.state.lock is not None
    assert t.state.lock.commitment is Commitment.COMMITTED


def test_not_travelling_expires_and_is_the_only_silencer():
    s = locked()
    for action in (Leaving(None, at("07:55")), Choose(None, LATER)):
        t = apply_action(s, ctx(at("07:55")), action)
        assert t.state.direction_state is not DirectionState.EXPIRED
        assert not t.state.not_travelling
    t = apply_action(s, ctx(at("07:55")), NotTravelling(None, False))
    assert t.state.direction_state is DirectionState.EXPIRED and t.state.not_travelling
    unlocked = replace(t.state, lock=None)
    assert auto_lock(unlocked, ctx(at("08:00")), answer(LATER)).events == ()
    assert auto_lock(unlocked, ctx(at("08:00")), answer(LATER)).state.lock is None


def test_choose_over_cap_refused():
    s = locked()
    legs = [
        make_leg("AAA", "BBB", "08:12", "08:20", number="1"),
        make_leg("BBB", "RTD", "08:30", "08:40", number="2"),
        make_leg("RTD", "UT", "08:50", "09:24", number="3"),
    ]
    t = apply_action(s, ctx(at("07:55")), Choose(None, make_connection(legs)))
    assert t.state is s
    assert [(e.kind, e.severity) for e in t.events] == [(EventKind.CHOOSE_REFUSED, Severity.LOUD)]
    assert "refused" in t.events[0].text and "cap" in t.events[0].text
    # failing is_good is refused too: 08:12 cannot be caught at 08:05 with a 10-min buffer
    uncatchable = apply_action(s, ctx(at("08:05")), Choose(None, USUAL))
    assert uncatchable.state is s and "cannot be caught" in uncatchable.events[0].text


def riding_with_retained(retained_first: str, now: str) -> OccurrenceState:
    """Missed-first-train relock moved the lock to 08:44; the 08:12 left with the user maybe on
    it, so it is retained and riding."""
    del now  # the leg state already says the 08:12 departed
    ridden = journey_of(direct(retained_first, "08:46"), LockSource.AUTO, Commitment.COMMITTED)
    ridden = replace(ridden, legs=(replace(ridden.legs[0], state=LegState.RIDING),))
    lock = journey_of(LATER, LockSource.MISSED, Commitment.COMMITTED)
    return replace(
        fresh(),
        generation=3,
        direction_state=DirectionState.RIDING,
        lock=lock,
        retained=(ridden,),
        travelling=True,
        tap_at=at("07:55"),
    )


def connection_of(j: Journey) -> Connection:
    first = j.legs[0].train
    return direct(first.planned_departure.strftime("%H:%M"), "08:46", first.train_number)


def test_choose_retained_remainder_user_rides():  # review-codex-rev11 N3 (required)
    s = riding_with_retained(
        retained_first="08:12", now="08:18"
    )  # lock moved to 08:44, user is on the 08:12
    t = apply_action(s, ctx(now=dt("2026-10-27 08:18")), Choose(None, connection_of(s.retained[0])))
    assert t.state.lock.legs[0].train.planned_departure == dt("2026-10-27 08:12")
    assert t.state.lock.legs[0].state is LegState.RIDING  # ridden prefix kept
    assert not any(e.kind is EventKind.CANCELLED for e in t.events)
    assert "refused" not in " ".join(e.text for e in t.events)
    assert t.state.lock.source is LockSource.CHOOSE
    assert t.state.retained == ()  # the 08:44 was never boarded, so it is dropped


def test_relock_retains_a_train_the_user_may_be_on():
    s = riding_with_retained("08:12", "08:18")
    s = replace(s, lock=s.retained[0], retained=())  # riding the 08:12 as the lock
    moved = write_lock(
        s, journey_of(LATER, LockSource.MISSED, Commitment.COMMITTED), expect=None, now=at("08:18")
    )
    assert moved is not None and first_dep(moved) == at("08:44")
    assert [j.legs[0].train for j in moved.retained] == [s.lock.legs[0].train]


def riding_occurrence_with_retained() -> OccurrenceState:
    return riding_with_retained("08:12", "08:18")


def next_window_occurrence() -> OccurrenceState:
    return fresh(date(2026, 10, 28))


def retained_connection() -> Connection:
    return direct("08:12", "08:46", "1234")


def test_choose_retained_targets_the_riding_occurrence():  # N3, 7.13
    states = [riding_occurrence_with_retained(), next_window_occurrence()]
    assert target_occurrence(states, Choose(None, retained_connection())) is states[0]


def test_target_occurrence_rules():
    states = [riding_occurrence_with_retained(), next_window_occurrence()]
    other = direct("08:44", "09:24", "5678", day=date(2026, 10, 28))
    assert target_occurrence(states, Choose(None, other)) is states[1]  # oldest not riding
    assert target_occurrence(states, Leaving(None, at("08:00"))) is states[1]
    assert target_occurrence(states, NotTravelling(None, False)) is states[0]  # riding included
    assert target_occurrence(states, NotTravelling(date(2026, 10, 28), False)) is states[1]
    expired = [replace(states[0], direction_state=DirectionState.EXPIRED), states[1]]
    assert target_occurrence(expired, NotTravelling(None, False)) is expired[1]


def test_relock_retains_a_departed_train_whose_leg_is_still_pending():
    late = auto_lock(fresh(), ctx(at("08:20")), answer(USUAL, LATER)).state
    assert late.lock is not None and late.lock.legs[0].state is LegState.PENDING
    missed = journey_of(LATER, LockSource.MISSED, Commitment.COMMITTED)
    moved = write_lock(late, missed, expect=None, now=at("08:20"))
    assert moved is not None and [j.legs[0].train for j in moved.retained] == [
        late.lock.legs[0].train
    ]
    # choosing that retained train back keeps it although its leg never reached RIDING
    t = apply_action(moved, ctx(at("08:21")), Choose(None, connection_of(moved.retained[0])))
    assert t.events == () and first_dep(t.state) == at("08:12")
    # not yet departed: nothing to retain
    early = write_lock(locked(), missed, expect=None, now=at("08:05"))
    assert early is not None and early.retained == ()


def test_auto_lock_does_not_judge_an_answer_that_cannot_contain_the_usual():
    after = ProviderAnswer(Query("AAA", "UT", at("08:30")), (LATER,), at("08:30"))
    elsewhere = ProviderAnswer(Query("BBB", "UT", at("07:12")), (LATER,), at("07:50"))
    for a in (after, elsewhere):
        t = auto_lock(fresh(), ctx(at("07:50")), a)
        assert t.state == fresh() and t.events == ()


def test_whole_day_keys_cover_both_directions_of_the_commute_day():
    night = PROFILES.replace(
        '{first_train: "17:52", buffer_min: 5}',
        '{first_train: "06:30", buffer_min: 5, day_offset: 1}',
    )
    ps, errs = parse_profiles(night, None)
    assert errs == []
    ret = OccurrenceKey("p1", Direction.RETURN, date(2026, 10, 28))
    keys = whole_day_keys(ps[0], ret)
    assert keys == (OccurrenceKey("p1", Direction.OUTBOUND, DAY), ret)
    assert whole_day_keys(ps[0], keys[0]) == keys
