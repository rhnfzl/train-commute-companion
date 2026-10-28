from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime

from builders import AMS, _on_track, make_connection, make_leg, planned_at_alight

from train_commute_companion.health import PollRecord
from train_commute_companion.lifecycle import (
    Board,
    Commitment,
    Ctx,
    DirectionState,
    Journey,
    LegProgress,
    LegState,
    LockSource,
    OccurrenceState,
    Transition,
    _stated,
    journey_of,
    on_poll,
    progress_of,
    queries_needed,
)
from train_commute_companion.model import (
    AlertEvent,
    Connection,
    Direction,
    EventKind,
    Leg,
    Severity,
)
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.serde import dumps, loads
from train_commute_companion.unclear import train_key

PROFILES = """
profiles:
  - id: "p1"
    name: "office_a"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "RTD"
    transfer_min: {UT: UT_MIN}
    outbound: {first_train: "08:12", buffer_min: 10, transfers: TRANSFERS}
    return: {first_train: "17:52", buffer_min: 5}
"""
DAY = date(2026, 10, 27)


def at(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-27 {hhmm}").replace(tzinfo=AMS)


def ctx(now: datetime, transfers: str = "[UT]", ut_min: int = 4) -> Ctx:
    text = PROFILES.replace("TRANSFERS", transfers).replace("UT_MIN", str(ut_min))
    ps, errs = parse_profiles(text, None)
    assert errs == []
    p = ps[0]
    return Ctx(p, Direction.OUTBOUND, dated_schedule(p, Direction.OUTBOUND, DAY, None), None, now)


DIRECT = "[]"


def conn(*legs: Leg) -> Connection:
    return make_connection(legs)


def ans(origin: str, *conns: Connection, when: str = "08:00") -> ProviderAnswer:
    q = Query(origin, "RTD", at(when))
    return ProviderAnswer(q, conns, q.at)


def board(s: OccurrenceState, *answers: ProviderAnswer) -> Board:
    return Board(answers, (PollRecord(at("08:00"), True, None),), s.generation)


def poll(s: OccurrenceState, c: Ctx, *answers: ProviderAnswer) -> Transition:
    return on_poll(s, c, board(s, *answers))


def journey(
    legs: Sequence[Leg], states: Sequence[LegState] = (), source: LockSource = LockSource.AUTO
) -> Journey:
    j = journey_of(conn(*legs), source, Commitment.INTENDED)
    xs = [replace(x, state=st) for x, st in zip(j.legs, states, strict=False)]
    return replace(j, legs=(*xs, *j.legs[len(xs) :]))


def occ(lock: Journey | None, **kw: object) -> OccurrenceState:
    base = OccurrenceState(
        key=ctx(at("07:12")).schedule.key,
        generation=1,
        route_revision=1,
        settings_revision=1,
        baseline_version=None,
        direction_state=DirectionState.LOCKED,
        lock=lock,
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
    s = replace(base, **kw)  # type: ignore[arg-type]
    departed = s.retained or (lock and any(x.state in RIDDEN for x in lock.legs))
    if departed and "direction_state" not in kw:  # a consistent state: riding once leg 1 left
        s = replace(s, direction_state=DirectionState.RIDING)
    return s


RIDDEN = (LegState.RIDING, LegState.DONE)


def numbers(s: OccurrenceState) -> list[str]:
    assert s.lock is not None
    return [x.train.train_number for x in s.lock.legs]


def kinds(t: Transition) -> list[tuple[EventKind, Severity]]:
    return [(e.kind, e.severity) for e in t.events]


# two-leg usual journey AAA -> UT -> RTD
L1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234")
L2 = make_leg("UT", "RTD", "08:50", "09:24", number="5678")
L1X = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True)
L2X = make_leg("UT", "RTD", "08:50", "09:24", number="5678", cancelled=True)
R2 = make_leg("UT", "RTD", "09:05", "09:39", number="6666")
LATER = conn(make_leg("AAA", "UT", "08:25", "08:53", number="1300"), R2)
RIDE = (LegState.RIDING,)


# direct usual journey AAA -> RTD calling at BBB and UT
def direct_1234(cut_after: str | None = None) -> Leg:
    return make_leg(
        "AAA", "RTD", "08:12", "09:24", number="1234", stops=["BBB", "UT"], cut_after=cut_after
    )


T_FULL, T_UT, T_BBB = direct_1234(), direct_1234("UT"), direct_1234("BBB")
FROM_UT = make_leg("UT", "RTD", "08:30", "09:00", number="8888")
FROM_BBB = make_leg("BBB", "RTD", "08:35", "09:10", number="9999")


def usual_locked() -> OccurrenceState:
    return occ(journey([L1, L2]))


def test_cancelled_first_leg_relocks_whole_journey():
    s = usual_locked()
    t = poll(s, ctx(at("07:50")), ans("AAA", conn(L1X, L2), LATER))
    assert numbers(t.state) == ["1300", "6666"]
    assert t.state.lock is not None and t.state.lock.source is LockSource.RELOCK
    assert kinds(t) == [(EventKind.CANCELLED, Severity.LOUD)]
    assert "cancelled" in t.events[0].text and "08:25" in t.events[0].text
    assert [h.train for h in t.state.cancellation_history] == [L1.train]
    assert t.state.generation == s.generation + 1


def test_cancelled_second_leg_relocks_from_transfer():  # owner fixture
    s = occ(journey([L1, L2], RIDE))
    t = poll(s, ctx(at("08:20")), ans("AAA", conn(L1, L2X)), ans("UT", conn(L2X), conn(R2)))
    assert numbers(t.state) == ["1234", "6666"]
    assert t.state.lock is not None and t.state.lock.legs[0].state is LegState.RIDING
    assert kinds(t) == [(EventKind.CANCELLED, Severity.LOUD)] and "09:05" in t.events[0].text
    assert [h.train for h in t.state.cancellation_history] == [L2.train]
    assert t.state.retained == ()


def test_broken_transfer_by_delay_relocks_suffix():  # owner fixture
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=12)  # 08:52 at UT
    s = occ(journey([L1, L2], RIDE))
    t = poll(s, ctx(at("08:20")), ans("AAA", conn(late, L2)), ans("UT", conn(L2), conn(R2)))
    assert numbers(t.state) == ["1234", "6666"]
    assert kinds(t) == [(EventKind.TRANSFER_BROKEN, Severity.LOUD)]
    assert "UT" in t.events[0].text and "09:05" in t.events[0].text
    assert t.state.cancellation_history == ()  # a missed change is not a reinstatable train


def test_broken_transfer_searches_from_expected_arrival_plus_minimum():
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=12)  # 08:52 at UT
    s = occ(journey([L1, L2], RIDE))
    c = ctx(at("08:20"))
    # the answer asked at the planned 08:50 lists nothing later: not yet proof of no connection
    t1 = poll(s, c, ans("AAA", conn(late, L2)), ans("UT", conn(L2), when="08:50"))
    assert kinds(t1) == [(EventKind.TRANSFER_BROKEN, Severity.LOUD)]
    assert numbers(t1.state) == ["1234", "5678"] and t1.state.generation == s.generation
    assert Query("UT", "RTD", at("08:56")) in queries_needed(t1.state, c)  # 08:52 + 4
    # the next poll asks from 08:56 and finds the later train
    t2 = poll(t1.state, c, ans("AAA", conn(late, L2)), ans("UT", conn(R2), when="08:56"))
    assert numbers(t2.state) == ["1234", "6666"]
    # asked from 08:56 with nothing there: now it is loud
    t3 = poll(t1.state, c, ans("AAA", conn(late, L2)), ans("UT", when="08:56"))
    assert kinds(t3)[-1] == (EventKind.NO_CONNECTION, Severity.LOUD)


B1 = make_leg("BBB", "UT", "08:30", "08:45", number="2001")
L1_CUT = make_leg("AAA", "UT", "08:12", "08:40", number="1234", stops=["BBB"], cut_after="BBB")


def cut_at_bbb() -> Transition:
    s = occ(journey([L1, L2], RIDE))
    return poll(s, ctx(at("08:15")), ans("AAA", conn(L1_CUT, L2)), ans("BBB", conn(B1, R2)))


def test_cut_short_first_leg_before_transfer_station():  # spec fixture
    t = cut_at_bbb()
    assert numbers(t.state) == ["1234", "2001", "6666"]  # usual remaining pattern via UT kept
    lock = t.state.lock
    assert lock is not None
    cut = lock.legs[0]
    assert (cut.intended_alight, cut.served_endpoint, cut.alight_stop) == ("UT", "BBB", "BBB")
    assert not cut.alight_frozen and cut.state is LegState.RIDING
    assert kinds(t) == [(EventKind.CUT_SHORT, Severity.LOUD)]
    assert "runs only to BBB" in t.events[0].text and "cancelled" not in t.events[0].text
    # the original onward train can come back by the restore rule
    assert [h.train for h in t.state.cancellation_history] == [L2.train]


def direct_riding() -> OccurrenceState:
    return occ(journey([T_FULL], RIDE))


def test_cut_short_alighting_stop_follows_endpoint_until_recovery_boarded():
    c = ctx(at("08:14"), DIRECT)
    t1 = poll(direct_riding(), c, ans("AAA", conn(T_BBB)), ans("BBB", conn(FROM_BBB)))
    assert numbers(t1.state) == ["1234", "9999"]
    assert t1.state.lock is not None and t1.state.lock.legs[0].alight_stop == "BBB"
    # not yet boarded: the stop follows the endpoint, the recovery moves with it
    t2 = poll(
        t1.state, c, ans("AAA", conn(T_UT)), ans("UT", conn(FROM_UT)), ans("BBB", conn(FROM_BBB))
    )
    assert numbers(t2.state) == ["1234", "8888"]
    lock = t2.state.lock
    assert lock is not None and lock.legs[0].alight_stop == "UT"
    assert kinds(t2) == [(EventKind.CUT_SHORT, Severity.LOUD)]
    # recovery boarded: the stop freezes, a later endpoint change moves nothing
    boarded = replace(lock, legs=(lock.legs[0], replace(lock.legs[1], state=LegState.RIDING)))
    s3 = replace(t2.state, lock=boarded)
    answers = (ans("AAA", conn(T_BBB)), ans("BBB", conn(FROM_BBB)), ans("UT", conn(FROM_UT)))
    t3 = poll(s3, ctx(at("08:31"), DIRECT), *answers)
    assert numbers(t3.state) == ["1234", "8888"]
    assert t3.state.lock is not None
    x = t3.state.lock.legs[0]
    assert (x.alight_stop, x.served_endpoint, x.alight_frozen) == ("UT", "BBB", True)
    # shown, never moved: the boarded recovery is kept (7.4, 7.12 rule 7)
    assert kinds(t3)[0] == (EventKind.TRANSFER_BROKEN, Severity.LOUD)


def cut_at_ut(recovery: LegState) -> OccurrenceState:
    j = journey([T_FULL, FROM_UT], (LegState.RIDING, recovery))
    cut = replace(j.legs[0], served_endpoint="UT", alight_stop="UT")
    return occ(replace(j, legs=(cut, j.legs[1])))


def test_cut_short_endpoint_extended_after_recovery_boarded_keeps_frozen_stop():
    s = cut_at_ut(LegState.RIDING)
    t = poll(s, ctx(at("08:31"), DIRECT), ans("AAA", conn(T_FULL)), ans("UT", conn(FROM_UT)))
    assert numbers(t.state) == ["1234", "8888"]
    assert t.state.lock is not None
    x = t.state.lock.legs[0]
    assert (x.alight_stop, x.served_endpoint, x.alight_frozen) == ("UT", "RTD", True)
    assert t.events == ()
    # before the recovery is boarded, an extension back to the destination drops the recovery
    s = cut_at_ut(LegState.PENDING)
    t = poll(s, ctx(at("08:20"), DIRECT), ans("AAA", conn(T_FULL)), ans("UT", conn(FROM_UT)))
    assert numbers(t.state) == ["1234"]
    assert t.state.lock is not None and t.state.lock.legs[0].alight_stop == "RTD"
    assert kinds(t) == [(EventKind.RESTORED, Severity.SILENT)]


def test_cut_short_cut_one_stop_earlier_reopens_and_moves_recovery():
    s = cut_at_ut(LegState.PENDING)
    answers = (ans("AAA", conn(T_BBB)), ans("BBB", conn(FROM_BBB)), ans("UT", conn(FROM_UT)))
    t = poll(s, ctx(at("08:14"), DIRECT), *answers)
    assert numbers(t.state) == ["1234", "9999"]
    assert t.state.lock is not None
    x = t.state.lock.legs[0]
    assert (x.alight_stop, x.served_endpoint, x.alight_frozen) == ("BBB", "BBB", False)
    assert kinds(t) == [(EventKind.CUT_SHORT, Severity.LOUD)]


def test_reinstated_original_before_replacement_is_restored():
    relocked = poll(usual_locked(), ctx(at("07:50")), ans("AAA", conn(L1X, L2), LATER)).state
    t = poll(relocked, ctx(at("07:55")), ans("AAA", conn(L1, L2), LATER))
    assert numbers(t.state) == ["1234", "5678"]
    assert t.state.lock is not None and t.state.lock.source is LockSource.RESTORE
    assert kinds(t) == [(EventKind.RESTORED, Severity.SILENT)]
    assert "back to normal" in t.events[0].text
    assert t.state.cancellation_history == ()


def test_restore_never_replaces_suffix_being_ridden():
    s = cut_at_bbb().state
    assert s.lock is not None
    answers = (ans("AAA", conn(L1, L2)), ans("UT", conn(L2)), ans("BBB", conn(B1, R2)))
    # the recovery from BBB is being ridden: the reinstated original is not taken
    legs = s.lock.legs
    ridden = replace(
        s, lock=replace(s.lock, legs=(legs[0], replace(legs[1], state=LegState.RIDING), legs[2]))
    )
    t = poll(ridden, ctx(at("08:31")), *answers)
    assert numbers(t.state) == ["1234", "2001", "6666"]
    assert EventKind.RESTORED not in [e.kind for e in t.events]
    assert t.state.lock is not None and t.state.lock.legs[0].alight_stop == "BBB"
    # not yet boarded: the original onward train comes back
    t = poll(s, ctx(at("08:20")), *answers)
    assert numbers(t.state) == ["1234", "5678"]
    assert t.state.lock is not None and t.state.lock.legs[0].alight_stop == "UT"
    assert kinds(t) == [(EventKind.RESTORED, Severity.SILENT)]


def test_locked_leg_absent_from_empty_answer_is_missing():  # Review Focus 1
    s = usual_locked()
    t = poll(s, ctx(at("07:50")), ans("AAA"))
    assert kinds(t) == [(EventKind.MISSING, Severity.LOUD)] * 2
    assert [e.train for e in t.events] == [L1.train, L2.train]
    assert t.state.lock == s.lock and t.state.generation == s.generation


def test_retained_leg_missing_while_new_lock_healthy_is_missing():
    s = occ(journey(LATER.legs), retained=(journey([L1, L2], RIDE),))
    t = poll(s, ctx(at("08:20")), ans("AAA", LATER))
    assert kinds(t) == [(EventKind.MISSING, Severity.LOUD)] * 2
    assert [e.train for e in t.events] == [L1.train, L2.train]
    assert all("if you are on the 08:12" in e.text for e in t.events)
    assert numbers(t.state) == ["1300", "6666"]


def test_retained_onward_leg_cancelled_alerts_without_relocking():  # spec 10.1 safety fixture
    s = occ(journey(LATER.legs), retained=(journey([L1, L2], RIDE),))
    t = poll(s, ctx(at("08:20")), ans("AAA", conn(L1, L2X), LATER), ans("UT", conn(R2)))
    assert kinds(t) == [(EventKind.CANCELLED, Severity.LOUD)]
    e = t.events[0]
    assert e.train == L2.train and "if you are on the 08:12: your 08:50" in e.text
    assert "Next good connection: 09:05" in e.text and ":retained:1234@" in e.key
    trains = [[(x.train, x.state) for x in r.legs] for r in t.state.retained]
    assert trains == [[(x.train, x.state) for x in r.legs] for r in s.retained]
    assert numbers(t.state) == ["1300", "6666"]


def test_stale_generation_poll_is_discarded():  # spec safety fixture
    s = usual_locked()
    stale = Board((ans("AAA", conn(L1X, L2), LATER),), (), s.generation - 1)
    t = on_poll(s, ctx(at("07:50")), stale)
    assert t.state is s and t.events == ()


def test_relock_keeps_remaining_pattern():
    earlier_direct = conn(make_leg("AAA", "RTD", "08:20", "09:10", number="7000"))
    t = poll(usual_locked(), ctx(at("07:50")), ans("AAA", conn(L1X, L2), earlier_direct, LATER))
    assert numbers(t.state) == ["1300", "6666"]
    assert "new route" not in t.events[0].text


def test_relock_must_use_different_pattern():
    via_bbb = conn(
        make_leg("AAA", "BBB", "08:20", "08:30", number="7100"),
        make_leg("BBB", "RTD", "08:40", "09:15", number="7200"),
    )
    t = poll(usual_locked(), ctx(at("07:50")), ans("AAA", conn(L1X, L2), via_bbb))
    assert numbers(t.state) == ["7100", "7200"]
    assert "new route via BBB" in t.events[0].text


def test_no_connection_found_is_loud():
    s = usual_locked()
    t = poll(s, ctx(at("07:50")), ans("AAA", conn(L1X, L2)))
    assert kinds(t) == [
        (EventKind.CANCELLED, Severity.LOUD),
        (EventKind.NO_CONNECTION, Severity.LOUD),
    ]
    assert "check the NS app" in t.events[1].text
    assert t.state.lock == s.lock  # never silently kept: the user was told loudly


def test_boarded_train_rerouted_over_cap_followed_with_loud_note():
    rerouted = conn(
        make_leg("AAA", "BBB", "08:12", "08:30", number="1234"),
        make_leg("BBB", "UT", "08:40", "08:55", number="2222"),
        make_leg("UT", "RTD", "09:05", "09:31", number="3333"),
    )
    t = poll(direct_riding(), ctx(at("08:20"), DIRECT), ans("AAA", rerouted))
    assert numbers(t.state) == ["1234", "2222", "3333"]  # over the cap of usual + 1
    lock = t.state.lock
    assert lock is not None and lock.legs[0].state is LegState.RIDING  # never dropped
    assert lock.legs[0].alight_stop == "BBB"
    notes = [e for e in t.events if e.kind is EventKind.ROUTE_CHANGED]
    assert len(notes) == 1 and notes[0].severity is Severity.LOUD
    assert "beyond the sensors" in notes[0].text


def test_route_changed_before_boarding_follows_within_cap():
    onward = (
        make_leg("UT", "BBB", "08:52", "09:00", number="3100"),
        make_leg("BBB", "RTD", "09:10", "09:40", number="3200"),
    )
    t = poll(usual_locked(), ctx(at("07:50")), ans("AAA", conn(L1, *onward)))
    assert numbers(t.state) == ["1234", "3100", "3200"]  # 2 changes: within usual + 1
    notes = [e for e in t.events if e.kind is EventKind.ROUTE_CHANGED]
    assert len(notes) == 1 and "via UT, BBB, following it" in notes[0].text
    assert (EventKind.MISSING, Severity.LOUD) in kinds(t)  # the 08:50 is gone from the answer


def test_raising_transfer_min_turns_transfer_at_risk_next_poll():
    s = occ(journey([L1, L2], RIDE))  # 10 min at UT
    answers = (ans("AAA", conn(L1, L2)),)
    assert poll(s, ctx(at("08:20")), *answers).events == ()
    in_flight = board(s, *answers)
    edited = replace(s, generation=s.generation + 1, settings_revision=2)  # settings edit
    c = ctx(at("08:20"), ut_min=9)
    assert on_poll(edited, c, in_flight).state is edited  # computed under the old settings
    t = poll(edited, c, *answers)
    assert kinds(t) == [(EventKind.TRANSFER_AT_RISK, Severity.LOUD)]  # within 30 min
    assert numbers(t.state) == ["1234", "5678"]


def test_queries_cover_retained_and_restorable():
    c = ctx(at("08:20"))
    assert queries_needed(occ(None), c) == [Query("AAA", "RTD", at("08:12"))]
    lock = journey([L1, R2], RIDE)
    retained = journey(
        [
            make_leg("AAA", "UT", "08:05", "08:33", number="1111"),
            make_leg("UT", "RTD", "08:55", "09:31", number="5555"),
        ],
        RIDE,
    )
    gone = make_leg("UT", "RTD", "08:15", "08:50", number="4444")  # departed: not restorable
    history: tuple[LegProgress, ...] = (progress_of(L2), progress_of(gone))
    s = occ(lock, retained=(retained,), cancellation_history=history)
    assert queries_needed(s, c) == [
        Query("AAA", "RTD", at("08:12")),
        Query("UT", "RTD", at("09:05")),
        Query("AAA", "RTD", at("08:05")),
        Query("UT", "RTD", at("08:55")),
        Query("UT", "RTD", at("08:50")),
    ]
    # a done leg stays watched, and a served endpoint is asked for the recovery
    done = replace(lock.legs[0], state=LegState.DONE, served_endpoint="BBB", alight_stop="BBB")
    s = occ(replace(lock, legs=(done, lock.legs[1])))
    assert queries_needed(s, c) == [
        Query("UT", "RTD", at("09:05")),
        Query("AAA", "RTD", at("08:12")),
        Query("BBB", "RTD", at("08:12")),
    ]


def _late(delay: int) -> Connection:
    return conn(
        make_leg("AAA", "RTD", "08:12", "09:24", number="1234", delay_dep=delay, delay_arr=delay)
    )


def _direct_lock() -> OccurrenceState:
    return occ(journey([make_leg("AAA", "RTD", "08:12", "09:24", number="1234")]))


def test_plain_delay_is_silent_and_moves_only_by_3_min_steps():  # 7.9, 7.5
    s = _direct_lock()
    t = poll(s, ctx(at("07:50")), ans("AAA", _late(7)))
    assert kinds(t) == [(EventKind.DELAYED, Severity.SILENT)]
    t2 = poll(t.state, ctx(at("07:52")), ans("AAA", _late(8)))
    t3 = poll(t.state, ctx(at("07:52")), ans("AAA", _late(10)))
    assert t2.events[0].key == t.events[0].key  # a wobble is not news
    assert t3.events[0].key != t.events[0].key  # a 3-min step is
    assert poll(s, ctx(at("07:50")), ans("AAA", _late(2))).events == ()


def test_delay_past_latest_acceptable_arrival_is_loud():  # 7.9
    text = (
        PROFILES.replace("TRANSFERS", "[]")
        .replace("UT_MIN", "4")
        .replace("outbound: {", 'alerts: {latest_acceptable_arrival: "09:30"}\n    outbound: {')
    )
    ps, errs = parse_profiles(text, None)
    assert errs == []
    c = Ctx(
        ps[0],
        Direction.OUTBOUND,
        dated_schedule(ps[0], Direction.OUTBOUND, DAY, None),
        None,
        at("07:50"),
    )
    assert kinds(poll(_direct_lock(), c, ans("AAA", _late(7)))) == [
        (EventKind.DELAYED, Severity.LOUD)
    ]
    assert kinds(poll(_direct_lock(), c, ans("AAA", _late(4)))) == [
        (EventKind.DELAYED, Severity.SILENT)
    ]


# --- live news per leg: platform, delay recovery, both delays, arrival limit (7.5, 7.9) ---


def with_track(leg: Leg, planned: str, actual: str) -> Leg:
    b = replace(leg.stops[0], planned_track=planned, actual_track=actual)
    return replace(leg, stops=(b, *leg.stops[1:]))


def test_platform_change_is_news_silent_then_loud_in_the_15_min_zone():
    s = occ(journey([L1, L2]))
    t = poll(s, ctx(at("07:40")), ans("AAA", conn(with_track(L1, "1", "5"), L2)))
    pc = [e for e in t.events if e.kind is EventKind.PLATFORM_CHANGE]
    assert len(pc) == 1 and pc[0].severity is Severity.SILENT
    assert "platform 5 (was 1)" in pc[0].text
    t2 = poll(t.state, ctx(at("07:58")), ans("AAA", conn(with_track(L1, "1", "5"), L2)))
    pc2 = [e for e in t2.events if e.kind is EventKind.PLATFORM_CHANGE]
    assert pc2 and pc2[0].severity is Severity.LOUD and pc2[0].key == pc[0].key


def test_delay_back_to_normal_and_nearly_on_time_are_news():
    def leg(delay: int) -> Leg:
        return make_leg("AAA", "RTD", "08:12", "09:24", delay_dep=delay, delay_arr=delay)

    s = occ(journey([leg(0)]))
    c = ctx(at("07:40"), DIRECT)
    t = poll(s, c, ans("AAA", conn(leg(7))))
    assert [e.kind for e in t.events] == [EventKind.DELAYED]
    t = poll(t.state, c, ans("AAA", conn(leg(2))))
    assert [(e.kind, e.severity) for e in t.events] == [(EventKind.NEARLY_ON_TIME, Severity.SILENT)]
    t = poll(t.state, c, ans("AAA", conn(leg(0))))
    assert [(e.kind, e.severity) for e in t.events] == [(EventKind.BACK_TO_NORMAL, Severity.SILENT)]
    assert "back to normal" in t.events[0].text
    t = poll(t.state, c, ans("AAA", conn(leg(0))))
    assert t.events == ()  # on time and already told: nothing


def test_departure_delay_moves_are_news_while_arrival_is_unchanged():
    s = occ(journey([L1, L2]))
    c = ctx(at("07:40"))
    seven = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=7)
    ten = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=10)
    t1 = poll(s, c, ans("AAA", conn(seven, L2)))
    t2 = poll(t1.state, c, ans("AAA", conn(ten, L2)))
    d1 = [e for e in t1.events if e.kind is EventKind.DELAYED]
    d2 = [e for e in t2.events if e.kind is EventKind.DELAYED]
    assert len(d1) == 1 and "leaves 7 min late and arrives on time" in d1[0].text
    assert len(d2) == 1 and "leaves 10 min late" in d2[0].text and d2[0].key != d1[0].key
    assert "delayed 0 min" not in d1[0].text + d2[0].text


def test_arrival_limit_is_compared_across_midnight():
    from train_commute_companion.classify import classify_leg
    from train_commute_companion.lifecycle import _delayed

    text = (
        PROFILES.replace("TRANSFERS", "[]")
        .replace("UT_MIN", "4")
        .replace('"08:12", buffer_min: 10', '"23:20", buffer_min: 10')
        .replace("    return:", '    alerts: {latest_acceptable_arrival: "23:55"}\n    return:')
    )
    ps, errs = parse_profiles(text, None)
    assert errs == [], errs
    p = ps[0]
    c = Ctx(p, Direction.OUTBOUND, dated_schedule(p, Direction.OUTBOUND, DAY, None), None,
            at("23:30"))  # fmt: skip
    for delay, loud in ((4, False), (15, True)):  # 23:54 is in time, 00:05 the next day is not
        leg = make_leg("AAA", "RTD", "23:20", "23:50", number="9", delay_arr=delay)
        e = _delayed(c, progress_of(leg), classify_leg(leg, "RTD"), last=True)
        assert (e.severity is Severity.LOUD) is loud, (delay, e.text)


def test_frozen_stop_takes_the_latest_arrival_of_any_copy():  # 7.12: never a missed alarm
    ut = T_FULL.stops[2]
    late_ut = replace(ut, arrival=replace(ut.arrival, value=at("08:27")))
    late = replace(T_FULL, stops=(*T_FULL.stops[:2], late_ut, *T_FULL.stops[3:]))
    for pair in ((T_FULL, late), (late, T_FULL)):
        s = cut_at_ut(LegState.RIDING)
        t = poll(
            s, ctx(at("08:20"), DIRECT), ans("AAA", *map(conn, pair)), ans("UT", conn(FROM_UT))
        )
        assert (EventKind.TRANSFER_BROKEN, Severity.LOUD) in kinds(t)


# --- rule 23: copies of one followed train in one answer that disagree (data_unclear) ---

T_PLANNED = planned_at_alight(T_FULL)
SPLIT = (conn(T_FULL), conn(T_PLANNED))
UNCLEAR_TEXT = (
    "NS data unclear for IC 1234 at RTD. Copy 1: arrives 09:24 (planned only)."
    " Copy 2: arrives 09:24 (live). Check the NS app or the station boards."
)


def unclear(t: Transition) -> list[AlertEvent]:
    return [e for e in t.events if e.kind is EventKind.DATA_UNCLEAR]


def test_disagreeing_copies_of_a_followed_train_send_one_loud_data_unclear():
    t = poll(occ(journey([T_FULL])), ctx(at("07:52")), ans("AAA", *SPLIT))
    (e,) = unclear(t)
    assert e.severity is Severity.LOUD and e.train == T_FULL.train
    assert e.text == "Office A: " + UNCLEAR_TEXT


def test_data_unclear_is_sent_once_per_train_per_day():
    c = ctx(at("07:52"))
    s = poll(occ(journey([T_FULL])), c, ans("AAA", *SPLIT)).state
    for world in (SPLIT, (conn(T_FULL),), SPLIT):  # same, agreeing, same again
        t = poll(s, c, ans("AAA", *world))
        assert unclear(t) == []
        s = t.state


def test_data_unclear_is_sent_again_when_the_values_change():
    c = ctx(at("07:52"))
    s = poll(occ(journey([T_FULL])), c, ans("AAA", *SPLIT)).state
    (e,) = unclear(poll(s, c, ans("AAA", conn(T_FULL), conn(_on_track(T_FULL, "7")))))
    assert "platform 7" in e.text


def test_copies_in_different_answers_never_send_data_unclear():
    t = poll(occ(journey([T_FULL])), ctx(at("07:52")),
             ans("AAA", conn(T_PLANNED), when="07:50"), ans("AAA", conn(T_FULL), when="07:52"))  # fmt: skip
    assert unclear(t) == []


def test_a_retained_train_gets_the_if_you_are_on_prefix():
    s = occ(journey(LATER.legs), retained=(journey([T_FULL], RIDE),))
    (e,) = unclear(poll(s, ctx(at("08:30")), ans("AAA", *SPLIT, LATER)))
    assert e.text == "Office A: if you are on the 08:12: " + UNCLEAR_TEXT


def _cut(leg: LegProgress) -> LegProgress:  # recovery fixed the alighting stop at UT
    return replace(leg, served_endpoint="RTD", alight_stop="UT", alight_frozen=True)


def test_recovery_alighting_stop_bounds_data_unclear():
    s = cut_at_ut(LegState.RIDING)  # lock: alight_stop UT, intended RTD; SPLIT differs only at RTD
    t = poll(s, ctx(at("08:31"), DIRECT), ans("AAA", *SPLIT), ans("UT", conn(FROM_UT)))
    assert unclear(t) == []
    j = journey([T_FULL], RIDE)
    s = occ(journey(LATER.legs), retained=(replace(j, legs=(_cut(j.legs[0]),)),))
    assert unclear(poll(s, ctx(at("08:30")), ans("AAA", *SPLIT, LATER))) == []


def test_a_train_in_lock_and_retained_is_compared_once():
    j = journey([T_FULL], RIDE)  # lock rides to RTD, the retained copy of the train to UT
    s = occ(j, retained=(replace(j, legs=(_cut(j.legs[0]),)),))
    t = poll(s, ctx(at("08:30")), ans("AAA", *SPLIT))
    (e,) = unclear(t)
    assert e.text == "Office A: " + UNCLEAR_TEXT  # the lock's bound (RTD), no prefix
    s = t.state
    for _ in range(2):
        s = loads(OccurrenceState, dumps(s))  # what the store keeps across a restart
        t = poll(s, ctx(at("08:30")), ans("AAA", *SPLIT))
        assert unclear(t) == []
        s = t.state


def test_reserved_characters_in_a_train_number_stay_out_of_the_event_key():
    odd = replace(T_FULL, train=replace(T_FULL.train, train_number="12|a~7"))
    t = poll(
        occ(journey([odd])), ctx(at("07:52")), ans("AAA", conn(odd), conn(planned_at_alight(odd)))
    )
    (e,) = unclear(t)
    assert "~" not in e.key and "|" not in e.key and e.train == odd.train
    assert f"unclear#{train_key(odd.train)}" in t.state.alert_keys
    # on-time following writes its own delay slots too, so check `_stated` on this event alone
    assert (
        _stated(replace(t.state, alert_keys={}), (e,)).state.alert_keys == {}
    )  # no synthetic slot
