from dataclasses import replace
from datetime import date, datetime

from builders import AMS, make_connection, make_leg

from train_commute_companion.advice import advise, still_advised
from train_commute_companion.lifecycle import (
    Commitment,
    Ctx,
    DirectionState,
    LockSource,
    OccurrenceState,
    journey_of,
)
from train_commute_companion.model import Connection, Direction, EventKind, Severity
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
USUAL_ARRIVAL = datetime(2026, 10, 27, 18, 26, tzinfo=AMS)


def at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 10, 27, int(h), int(m), tzinfo=AMS)


def ctx(now: datetime, direction: Direction = Direction.RETURN) -> Ctx:
    ps, errs = parse_profiles(PROFILES, None)
    assert errs == []
    p = ps[0]
    return Ctx(p, direction, dated_schedule(p, direction, DAY, None), None, now)


def fresh(c: Ctx) -> OccurrenceState:
    return OccurrenceState(
        key=c.schedule.key,
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


def conn(dep: str, arr: str, number: str, **k: object) -> Connection:
    return make_connection([make_leg("UT", "AAA", dep, arr, number=number, **k)])  # type: ignore[arg-type]


def answer(*conns: Connection) -> ProviderAnswer:
    q = Query("UT", "AAA", at("17:00"))
    return ProviderAnswer(q, conns, q.at)


EARLY = conn("17:22", "17:56", "1000")
SLOW = conn("17:52", "18:42", "1234")  # planned 16 min slower than usual
LATER = conn("18:22", "18:56", "2000")


def test_planned_slower_journey_triggers_advice():
    c = ctx(at("17:00"))
    rec, events = advise(fresh(c), c, answer(EARLY, SLOW, LATER), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]
    assert events[0].severity is Severity.LOUD
    assert rec.advised_train == EARLY.legs[0].train and rec.sent_at == at("17:00")


def test_healthy_usual_gives_no_advice():
    c = ctx(at("17:00"))
    ok = conn("17:52", "18:26", "1234")
    rec, events = advise(fresh(c), c, answer(EARLY, ok), USUAL_ARRIVAL)
    assert events == [] and rec.advised_train is None


def test_cancellation_with_good_replacement_still_advises():
    c = ctx(at("17:00"))
    cancelled = conn("17:52", "18:26", "1234", cancelled=True)
    rec, events = advise(fresh(c), c, answer(EARLY, cancelled, LATER), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]
    assert rec.advised_train == EARLY.legs[0].train


def test_saving_measured_against_replacement():
    # the replacement arrives 18:56; an earlier train arriving 18:53 saves only 3 min: no advice
    c = ctx(at("17:00"))
    cancelled = conn("17:52", "18:26", "1234", cancelled=True)
    near = conn("17:30", "18:53", "1001")
    _, events = advise(fresh(c), c, answer(near, cancelled, LATER), USUAL_ARRIVAL)
    assert events == []
    # measured against the cancelled usual arrival (18:26) it would look like a loss as well
    far = conn("17:30", "18:50", "1002")
    _, events = advise(fresh(c), c, answer(far, cancelled, LATER), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]


def test_off_by_default_outbound():
    c = ctx(at("07:00"), Direction.OUTBOUND)
    usual = make_connection([make_leg("AAA", "UT", "08:12", "08:46", number="1", cancelled=True)])
    early = make_connection([make_leg("AAA", "UT", "07:42", "08:16", number="2")])
    q = Query("AAA", "UT", at("07:00"))
    rec, events = advise(fresh(c), c, ProviderAnswer(q, (early, usual), q.at), at("08:46"))
    assert events == [] and rec.advised_train is None


def test_withdrawn_once():
    c = ctx(at("17:00"))
    s = fresh(c)
    rec, _ = advise(s, c, answer(EARLY, SLOW, LATER), USUAL_ARRIVAL)
    s = replace(s, advice=rec)
    ok = conn("17:52", "18:26", "1234")
    rec2, events = advise(s, c, answer(EARLY, ok, LATER), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.EARLIER_TRAIN_WITHDRAWN]
    assert events[0].severity is Severity.SILENT and rec2.withdrawn
    rec3, again = advise(replace(s, advice=rec2), c, answer(EARLY, ok, LATER), USUAL_ARRIVAL)
    assert again == [] and rec3 == rec2


def test_advice_is_not_repeated_for_the_same_train():
    c = ctx(at("17:00"))
    s = fresh(c)
    rec, _ = advise(s, c, answer(EARLY, SLOW), USUAL_ARRIVAL)
    _, events = advise(replace(s, advice=rec), c, answer(EARLY, SLOW), USUAL_ARRIVAL)
    assert events == []


def test_no_advice_when_earlier_train_cannot_be_caught():
    c = ctx(at("17:20"))  # 17:22 train is inside the 5 min buffer
    _, events = advise(fresh(c), c, answer(EARLY, SLOW), USUAL_ARRIVAL)
    assert events == []


def test_still_advised_needs_the_saving_too():
    c = ctx(at("17:00"))
    train = EARLY.legs[0].train
    assert still_advised(fresh(c), c, answer(EARLY, SLOW), train, USUAL_ARRIVAL) == EARLY
    # the advised train now arrives 18:48, 6 min after waiting's ride (18:42): no longer advised
    slower = conn("17:22", "17:56", "1000", delay_arr=52)
    assert still_advised(fresh(c), c, answer(slower, SLOW), train, USUAL_ARRIVAL) is None


def test_buffer_boundary_exactly_catchable_one_minute_short_not():
    ok = ctx(at("17:17"))  # 17:22 minus 5 min buffer: exactly catchable
    _, events = advise(fresh(ok), ok, answer(EARLY, SLOW), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]
    late = ctx(at("17:18"))  # 4 min before departure: inside the buffer
    _, events = advise(fresh(late), late, answer(EARLY, SLOW), USUAL_ARRIVAL)
    assert events == []


def test_no_advice_when_travelling():
    c = ctx(at("17:00"))
    s = fresh(c)
    j = journey_of(SLOW, LockSource.AUTO, Commitment.INTENDED)
    s = replace(s, direction_state=DirectionState.LOCKED, lock=j, travelling=True)
    _, events = advise(s, c, answer(EARLY, SLOW), USUAL_ARRIVAL)
    assert events == []


def test_locked_not_travelling_still_advises_on_the_locked_train():
    c = ctx(at("17:00"))
    j = journey_of(SLOW, LockSource.AUTO, Commitment.INTENDED)
    s = replace(fresh(c), direction_state=DirectionState.LOCKED, lock=j)
    _, events = advise(s, c, answer(EARLY, SLOW), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]


def test_no_baseline_picks_soonest_arrival_not_earliest_departure():
    # usual is cancelled and nothing later runs: no baseline, so the soonest arrival wins
    c = ctx(at("17:00"))
    cancelled = conn("17:52", "18:26", "1234", cancelled=True)
    slow_early = conn("17:22", "18:10", "1000")
    fast_later = conn("17:30", "17:56", "1001")
    rec, events = advise(fresh(c), c, answer(slow_early, fast_later, cancelled), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]
    assert rec.advised_train == fast_later.legs[0].train


def test_text_names_stations_and_live_departure():
    c = ctx(at("17:00"))
    delayed = conn("17:22", "17:56", "1000", delay_dep=3)  # planned 17:22, live 17:25
    _, events = advise(fresh(c), c, answer(delayed, SLOW), USUAL_ARRIVAL)
    text = events[0].text
    assert "17:25" in text and "17:22" not in text
    assert "UT" in text and "AAA" in text


def test_renewed_disruption_after_withdrawal_advises_again_with_a_new_identity():  # 7.6
    c = ctx(at("17:00"))
    s = fresh(c)
    rec, first = advise(s, c, answer(EARLY, SLOW, LATER), USUAL_ARRIVAL)
    ok = conn("17:52", "18:26", "1234")
    later = ctx(at("17:05"))
    rec2, withdrawn = advise(replace(s, advice=rec), later, answer(EARLY, ok, LATER), USUAL_ARRIVAL)
    assert [e.kind for e in withdrawn] == [EventKind.EARLIER_TRAIN_WITHDRAWN]
    again = ctx(at("17:08"))
    rec3, renewed = advise(
        replace(s, advice=rec2), again, answer(EARLY, SLOW, LATER), USUAL_ARRIVAL
    )
    assert [e.kind for e in renewed] == [EventKind.LEAVE_NOW]
    assert renewed[0].key != first[0].key  # the ledger does not drop it as a repeat
    assert rec3.advised_train == EARLY.legs[0].train and not rec3.withdrawn
    # and a second withdrawal is its own message too
    _, w2 = advise(replace(s, advice=rec3), ctx(at("17:10")), answer(EARLY, ok), USUAL_ARRIVAL)
    assert [e.kind for e in w2] == [EventKind.EARLIER_TRAIN_WITHDRAWN]
    assert w2[0].key != withdrawn[0].key


def test_boarding_locked_leg_does_not_stop_advice_before_travel():  # rule 17
    from train_commute_companion.lifecycle import LegState

    c = ctx(at("17:48"))  # SLOW 17:52 is within the 5 min buffer: BOARDING
    j = journey_of(SLOW, LockSource.AUTO, Commitment.INTENDED)
    j = replace(j, legs=(replace(j.legs[0], state=LegState.BOARDING),))
    s = replace(fresh(c), direction_state=DirectionState.LOCKED, lock=j, at_station=True)
    escape = conn("17:49", "18:20", "1003")  # catchable from the platform
    _, events = advise(s, c, answer(escape, SLOW), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]
    _, events = advise(
        replace(s, travelling=True, tap_at=at("17:40")), c, answer(escape, SLOW), USUAL_ARRIVAL
    )
    assert events == []  # a tap is travel evidence


def test_usual_connection_gone_still_advises_against_the_next_good_one():  # 7.6, rule 2
    c = ctx(at("17:00"))
    rec, events = advise(fresh(c), c, answer(EARLY, LATER), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]
    assert rec.advised_train == EARLY.legs[0].train


def test_lock_not_listed_as_one_connection_is_rebuilt_from_its_legs():
    c = ctx(at("17:00"))
    j = journey_of(SLOW, LockSource.AUTO, Commitment.INTENDED)
    s = replace(fresh(c), direction_state=DirectionState.LOCKED, lock=j)
    onward = make_leg("AAA", "BBB", "18:50", "19:00", number="3000")
    spread = make_connection([SLOW.legs[0], onward])  # the locked train, listed with more
    _, events = advise(s, c, answer(EARLY, spread), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]


def test_fragment_ending_before_the_alighting_stop_is_not_the_locked_arrival():  # 7.6, 6.1
    # the locked UT -> AAA 17:52 is only offered as UT -> BBB plus an onward train to AAA at
    # 18:56: its 18:10 arrival at BBB is not the destination arrival
    c = ctx(at("17:00"))
    j = journey_of(conn("17:52", "18:26", "1234"), LockSource.AUTO, Commitment.INTENDED)
    s = replace(fresh(c), direction_state=DirectionState.LOCKED, lock=j)
    part = make_leg("UT", "BBB", "17:52", "18:10", number="1234")
    onward = make_leg("BBB", "AAA", "18:20", "18:56", number="3000")
    board = answer(EARLY, make_connection([part, onward]))
    rec, events = advise(s, c, board, USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]  # unavailable, so advise
    _, again = advise(replace(s, advice=rec), ctx(at("17:05")), board, USUAL_ARRIVAL)
    assert again == []  # still needed: never withdrawn on the fragment
