from dataclasses import replace

from builders import make_leg
from test_lifecycle_follow import (
    L1,
    L1X,
    L2,
    L2X,
    LATER,
    R2,
    RIDE,
    ans,
    at,
    board,
    conn,
    ctx,
    journey,
    kinds,
    numbers,
    occ,
    poll,
)

from train_commute_companion.lifecycle import (
    Commitment,
    LegState,
    LockSource,
    OccurrenceState,
    Transition,
    ends,
    on_location,
    on_poll,
    on_tick,
)
from train_commute_companion.location import LocationEvent, Zone, resolve_zone
from train_commute_companion.model import EventKind, Severity

USUAL = conn(L1, L2)
E1 = make_leg("AAA", "UT", "07:42", "08:10", number="1100")
E2 = make_leg("UT", "RTD", "08:20", "08:54", number="2200")
EARLY = conn(E1, E2)


def event(zone: str, kind: str, when: str) -> LocationEvent:
    return LocationEvent(zone, kind, at(when), at(when))  # type: ignore[arg-type]


def locate(s: OccurrenceState, now: str, e: LocationEvent, *conns: object) -> Transition:
    a = ans("AAA", *conns, when=now) if conns else None  # type: ignore[arg-type]
    return on_location(s, ctx(at(now)), e, a)


def test_resolve_zone_inside_and_outside():
    zones = [Zone("city", 52.0, 5.0, 2000), Zone("UT", 52.0, 5.0, 150)]
    assert resolve_zone(52.0, 5.0, zones) == "UT"  # the smallest containing zone wins
    assert resolve_zone(52.0045, 5.0, zones) == "city"  # about 500 m north
    assert resolve_zone(52.05, 5.0, zones) is None  # about 5.5 km
    assert resolve_zone(52.0, 5.0, []) is None


def test_location_never_silences_alert():  # safety fixture
    s = occ(journey([L1, L2]))
    s = locate(s, "08:00", event("AAA", "enter", "08:00"), USUAL).state
    assert s.at_station and s.lock is not None and s.lock.source is LockSource.STATION
    s = locate(s, "08:01", event("RTD", "report", "08:01")).state  # a stray destination report
    assert not ends(s, at("08:01"), 0)
    t = poll(s, ctx(at("08:02")), ans("AAA", conn(L1X, L2), LATER, when="08:02"))
    assert (EventKind.CANCELLED, Severity.LOUD) in kinds(t)


def test_stale_location_counts_as_unknown():
    s = occ(journey([L1, L2]))
    t = locate(s, "07:45", event("home", "leave", "07:34"), USUAL)
    assert t.state == s and t.events == ()
    # a stale station report after the departure is not a missed train
    t = locate(s, "08:30", event("AAA", "report", "08:19"), USUAL, LATER)
    assert t.state == s and t.events == ()


def test_leaving_origin_sets_provisional_lock_with_event_time_plus_buffer():
    s = occ(journey([L1, L2]))
    t = locate(s, "07:31", event("home", "leave", "07:30"), EARLY, USUAL)
    assert t.state.lock is not None
    assert t.state.lock.source is LockSource.LOCATION
    assert t.state.lock.commitment is Commitment.PROVISIONAL
    assert numbers(t.state) == ["1100", "2200"]  # 07:42 >= 07:30 + 10
    assert t.state.errand_deadline == at("08:15")
    t = locate(s, "07:34", event("home", "leave", "07:33"), EARLY, USUAL)
    assert numbers(t.state) == ["1234", "5678"]  # 07:42 < 07:33 + 10


def test_missed_first_train_vs_missed_transfer_are_separate_events():  # spec fixture
    first = locate(occ(journey([L1, L2])), "08:14", event("AAA", "report", "08:13"), USUAL, LATER)
    assert kinds(first) == [(EventKind.MISSED_FIRST_TRAIN, Severity.LOUD)]
    assert numbers(first.state) == ["1300", "6666"]
    assert first.state.lock is not None and first.state.lock.source is LockSource.MISSED

    s = occ(journey([L1, L2], RIDE))
    transfer = locate(s, "08:52", event("UT", "report", "08:51"), USUAL, conn(L1, R2))
    assert kinds(transfer) == [(EventKind.MISSED_TRANSFER, Severity.LOUD)]
    assert numbers(transfer.state) == ["1234", "6666"]  # the ridden leg is kept
    assert first.events[0].text != transfer.events[0].text
    assert "change at UT" in transfer.events[0].text


def test_station_report_while_train_dwells_keeps_train_retained():  # safety fixture, 10.1
    # the spec's fourth context, across a restart, needs the store (Task 18)
    def retained_trains(s: OccurrenceState) -> list[list[str]]:
        return [[x.train.train_number for x in r.legs] for r in s.retained]

    # 1. missed first train: the 08:12 may still be at the platform with the user aboard
    t = locate(occ(journey([L1, L2])), "08:14", event("AAA", "report", "08:13"), USUAL, LATER)
    assert retained_trains(t.state) == [["1234", "5678"]]
    assert t.state.retained[0].legs[0].state is LegState.MISSED
    # and it keeps alerting: its original onward leg is cancelled while the new lock is healthy
    a = poll(t.state, ctx(at("08:16")), ans("AAA", conn(L1, L2X), LATER, when="08:16"))
    assert any("if you are on the 08:12" in e.text for e in a.events)
    # 2. missed transfer: the onward train and its legs stay
    s = occ(journey([L1, L2], RIDE))
    t = locate(s, "08:52", event("UT", "report", "08:51"), USUAL, conn(L1, R2))
    assert retained_trains(t.state) == [["5678"]]
    # 3. errand timeout after the provisional train departed
    s = locate(occ(None), "07:21", event("home", "leave", "07:20"), EARLY, USUAL).state
    t = on_tick(s, ctx(at("08:06")))
    assert t.state.lock is None and retained_trains(t.state) == [["1100", "2200"]]
    # 4. the train is delayed: a report after its planned but before its expected time is no miss
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=5)
    s = occ(journey([L1, L2]))
    t = locate(s, "08:15", event("AAA", "report", "08:14"), conn(late, L2), LATER)
    assert numbers(t.state) == ["1234", "5678"] and t.state.retained == ()
    assert not any(e.kind is EventKind.MISSED_FIRST_TRAIN for e in t.events)


def test_errand_timeout_falls_back_to_auto_lock():
    s = locate(occ(None), "07:21", event("home", "leave", "07:20"), EARLY, USUAL).state
    assert numbers(s) == ["1100", "2200"] and s.errand_deadline == at("08:05")
    assert on_tick(s, ctx(at("08:04"))).state == s  # not yet
    t = on_tick(s, ctx(at("08:06")), ans("AAA", USUAL, when="08:12"))
    assert t.state.lock is not None and t.state.lock.source is LockSource.AUTO
    assert numbers(t.state) == ["1234", "5678"]
    assert not t.state.travelling and t.state.errand_deadline is None
    assert [r.legs[0].train.train_number for r in t.state.retained] == ["1100"]
    assert not t.state.not_travelling  # the timeout never expires the occurrence


def test_station_report_waives_errand_timeout():
    s = locate(occ(None), "07:21", event("home", "leave", "07:20"), EARLY, USUAL).state
    s = locate(s, "07:35", event("AAA", "enter", "07:35"), EARLY, USUAL).state
    assert s.errand_deadline is None and s.lock is not None
    assert s.lock.commitment is Commitment.COMMITTED
    assert on_tick(s, ctx(at("08:06"))).state.lock == s.lock


def test_location_configured_but_silent_still_auto_locks():
    from train_commute_companion.model import LocationSettings

    c = ctx(at("07:47"))
    loc = LocationSettings("ha", "person.someone", {"home": "home", "work": "work"})
    c = replace(c, profile=replace(c.profile, location=loc))
    t = on_tick(occ(None), c, ans("AAA", USUAL, when="08:12"))
    assert t.state.lock is not None and t.state.lock.source is LockSource.AUTO
    assert numbers(t.state) == ["1234", "5678"]


def test_repeat_origin_reports_keep_poll_in_flight_and_lock():  # rule 21
    s = locate(occ(journey([L1, L2])), "08:00", event("AAA", "enter", "08:00"), USUAL, LATER).state
    assert s.lock is not None and s.lock.source is LockSource.STATION
    gen, lock = s.generation, s.lock
    b = board(s, ans("AAA", conn(L1X, L2), LATER, when="08:01"))  # a poll starts at 08:01
    for when in ("08:01", "08:02"):  # periodic platform reports while waiting
        s = locate(s, when, event("AAA", "report", when), LATER, USUAL).state
        assert s.generation == gen and s.lock == lock
    t = on_poll(s, ctx(at("08:02")), b)  # the poll lands and is not discarded
    assert (EventKind.CANCELLED, Severity.LOUD) in kinds(t)


def test_station_commits_same_auto_train_with_one_generation_step():
    s = occ(journey([L1, L2]))
    t = locate(s, "08:00", event("AAA", "enter", "08:00"), USUAL)
    assert t.state.generation == s.generation + 1 and numbers(t.state) == ["1234", "5678"]
    assert t.state.lock is not None and t.state.lock.source is LockSource.STATION
