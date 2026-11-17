from dataclasses import replace

import pytest
from builders import make_leg, on_track
from lifecycle_fixtures import (
    DAY,
    L1,
    L1X,
    L2,
    L2X,
    LATER,
    R2,
    RIDE,
    ans,
    at,
    conn,
    ctx,
    journey,
    kinds,
    numbers,
    occ,
    poll,
    poll_answers,
)
from location_fixtures import USUAL, event, locate

from train_commute_companion.lifecycle import Transition, on_location, on_poll
from train_commute_companion.lifecycle_end import may_archive, on_tick
from train_commute_companion.location import (
    Zone,
    ZoneTrack,
    next_zone,
    number,
    resolve_zone,
    seed_zone,
    send_zone_events,
    without_current_report,
)
from train_commute_companion.model import (
    Ctx,
    Direction,
    EventKind,
    LegState,
    OccurrenceKey,
    Severity,
)
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.state import Commitment, LockSource, OccurrenceState

E1 = make_leg("AAA", "UT", "07:42", "08:10", number="1100")
E2 = make_leg("UT", "RTD", "08:20", "08:54", number="2200")
EARLY = conn(E1, E2)


def test_number_is_a_finite_json_number():
    assert number(52) == 52.0
    assert number(5.5) == 5.5
    assert number(10**400) is None  # too large for a float
    for bad in (True, "52", None, float("nan"), float("inf")):
        assert number(bad) is None


def test_next_zone_gives_leave_enter_and_a_throttled_report():
    track, events = next_zone(ZoneTrack(), "UT", at("08:30"))  # no seed: nothing to leave
    assert events == [("UT", "enter")]
    track, events = next_zone(track, "UT", at("08:31"))
    assert events == []  # the enter started the timer
    track, events = next_zone(track, "UT", at("08:32"))
    assert events == [("UT", "report")]
    track, events = next_zone(track, "UT", at("08:33"))
    assert events == []
    track, events = next_zone(track, "AAA", at("08:33"))
    assert events == [("UT", "leave"), ("AAA", "enter")]
    track, events = next_zone(track, None, at("08:34"))
    assert events == [("AAA", "leave")]
    assert next_zone(track, None, at("08:35"))[1] == []
    # a return within 2 min of the last report enters and restarts that zone's timer
    track, events = next_zone(track, "AAA", at("08:35"))
    assert events == [("AAA", "enter")]
    assert next_zone(track, "AAA", at("08:36"))[1] == []


def test_seed_sends_nothing_and_starts_the_report_timer():
    track = seed_zone("UT", at("08:30"))
    assert next_zone(track, "UT", at("08:31"))[1] == []
    assert next_zone(track, "UT", at("08:32"))[1] == [("UT", "report")]
    assert next_zone(seed_zone(None, at("08:30")), "UT", at("08:31"))[1] == [("UT", "enter")]


def test_undelivered_lifts_only_the_current_zones_timer():
    track, _ = next_zone(seed_zone("AAA", at("08:30")), "UT", at("08:30"))
    track = without_current_report(track)
    assert next_zone(track, "UT", at("08:31"))[1] == [("UT", "report")]
    assert track.reported == {"AAA": at("08:30")}
    assert without_current_report(ZoneTrack()) == ZoneTrack()


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
    assert not may_archive(s, at("08:01"), 0)
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
    first = locate(occ(journey([L1, L2])), "08:16", event("AAA", "report", "08:15"), USUAL, LATER)
    assert kinds(first) == [(EventKind.MISSED_FIRST_TRAIN, Severity.LOUD)]
    assert numbers(first.state) == ["1300", "6666"]
    assert first.state.lock is not None and first.state.lock.source is LockSource.MISSED

    s = occ(journey([L1, L2], RIDE))
    transfer = locate(s, "08:54", event("UT", "report", "08:53"), USUAL, conn(L1, R2))
    assert kinds(transfer) == [(EventKind.MISSED_TRANSFER, Severity.LOUD)]
    assert numbers(transfer.state) == ["1234", "6666"]  # the ridden leg is kept
    assert first.events[0].text != transfer.events[0].text
    assert "change at UT" in transfer.events[0].text


def test_station_report_inside_grace_moves_nothing():  # safety fixture, Q4
    # the train may be pulling out with the user aboard: no miss, no relock, nothing retained
    def retained_trains(s: OccurrenceState) -> list[list[str]]:
        return [[x.train.train_number for x in r.legs] for r in s.retained]

    # 1. a report just after the 08:12 left
    t = locate(occ(journey([L1, L2])), "08:14", event("AAA", "report", "08:13"), USUAL, LATER)
    assert t.events == () and numbers(t.state) == ["1234", "5678"] and t.state.retained == ()
    assert t.state.lock is not None and t.state.lock.legs[0].state is not LegState.MISSED
    assert t.state.at_station
    # 2. a report from before the departure processed after it: no silent relock to the 08:25
    t = locate(occ(journey([L1, L2])), "08:13", event("AAA", "report", "08:11"), USUAL, LATER)
    assert t.events == () and numbers(t.state) == ["1234", "5678"] and t.state.retained == ()
    # 2b. the transfer: a report at UT just after the 08:50 left
    s = occ(journey([L1, L2], RIDE))
    t = locate(s, "08:53", event("UT", "report", "08:52"), USUAL, conn(L1, R2))
    assert t.events == () and numbers(t.state) == ["1234", "5678"] and t.state.retained == ()
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


def test_location_confirmed_missed_train_is_dropped():  # Q2
    t = locate(occ(journey([L1, L2])), "08:16", event("AAA", "report", "08:15"), USUAL, LATER)
    assert kinds(t) == [(EventKind.MISSED_FIRST_TRAIN, Severity.LOUD)] and t.state.retained == ()
    # its onward leg is cancelled later: nothing more about the missed train
    a = poll(t.state, ctx(at("08:17")), ans("AAA", conn(L1, L2X), LATER, when="08:17"))
    assert not any(e.train is not None and e.train.train_number == "1234" for e in a.events)
    assert not any("if you are on" in e.text for e in a.events)
    # a missed transfer is dropped the same way
    s = occ(journey([L1, L2], RIDE))
    t = locate(s, "08:54", event("UT", "report", "08:53"), USUAL, conn(L1, R2))
    assert kinds(t) == [(EventKind.MISSED_TRANSFER, Severity.LOUD)] and t.state.retained == ()
    a = poll(t.state, ctx(at("08:55")), ans("AAA", conn(L1, L2X), conn(L1, R2), when="08:55"))
    assert not any(e.train is not None and e.train.train_number == "5678" for e in a.events)


def test_missed_needs_three_minutes_after_expected_departure():  # Q4 boundary
    def missed(c: object, report: str, now: str) -> bool:
        t = locate(occ(journey([L1, L2])), now, event("AAA", "report", report), c, LATER)
        return any(e.kind is EventKind.MISSED_FIRST_TRAIN for e in t.events)

    assert not missed(USUAL, "08:14:59", "08:15:30")
    assert missed(USUAL, "08:15", "08:15:30")
    late = conn(make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=5), L2)
    assert not missed(late, "08:19:59", "08:20:30")  # expected 08:17
    assert missed(late, "08:20", "08:20:30")


def test_no_left_note_after_waiting_at_the_boarding_station():  # Q4
    def left_notes(s: OccurrenceState, now: str, *conns: object) -> list[str]:
        t = poll(s, ctx(at(now)), ans("AAA", *conns, when=now))  # type: ignore[arg-type]
        assert t.state.lock is not None and LegState.RIDING in [x.state for x in t.state.lock.legs]
        return [e.text for e in t.events if e.kind is EventKind.NEXT_CONNECTION]

    waited = locate(occ(journey([L1, L2])), "08:05", event("AAA", "enter", "08:05"), USUAL).state
    assert left_notes(waited, "08:13", USUAL, LATER) == []
    # a pass through the station more than BOARDING_LEAD before does not count
    early = locate(occ(journey([L1, L2])), "07:40", event("AAA", "enter", "07:40"), USUAL).state
    assert ["has left" in x for x in left_notes(early, "08:13", USUAL, LATER)] == [True]
    # waited, then a fix inside the grace as the train pulls out: still waited, no note
    t = locate(waited, "08:13", event("AAA", "report", "08:13"), USUAL, LATER)
    assert t.state.zone_reports["AAA"] == at("08:05")
    assert left_notes(t.state, "08:14", USUAL, LATER) == []
    # a first report only inside the grace is no wait: the hedge stays (reached it just late)
    t = locate(occ(journey([L1, L2])), "08:13", event("AAA", "enter", "08:13"), USUAL, LATER)
    assert ["has left" in x for x in left_notes(t.state, "08:14", USUAL, LATER)] == [True]
    # walked out again before the train: the wait ended, the hedge stays
    out = locate(waited, "08:08", event("AAA", "leave", "08:08"), USUAL).state
    assert "AAA" not in out.zone_reports
    assert ["has left" in x for x in left_notes(out, "08:13", USUAL, LATER)] == [True]
    # a leave as the train pulls out (the user aboard) keeps the wait
    aboard = locate(waited, "08:13", event("AAA", "leave", "08:13"), USUAL).state
    assert left_notes(aboard, "08:14", USUAL, LATER) == []
    # the transfer station works the same way
    s = occ(journey([L1, L2], RIDE), zone_reports={"UT": at("08:45")})
    assert left_notes(s, "08:51", USUAL, conn(L1, R2)) == []
    # no report at all keeps the hedge
    s = occ(journey([L1, L2], RIDE))
    assert ["has left" in x for x in left_notes(s, "08:51", USUAL, conn(L1, R2))] == [True]


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
    b = poll_answers(s, ans("AAA", conn(L1X, L2), LATER, when="08:01"))  # a poll starts at 08:01
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


def test_station_lock_reads_the_watched_answers_too():  # 7.12 rule 1
    # the newer answer from UT shows 6666 cancelled: entering the origin never locks it
    s = occ(journey([L1, L2]))
    r2x = make_leg("UT", "RTD", "09:05", "09:39", number="6666", cancelled=True)
    origin = ans("AAA", conn(L1X, L2), LATER, when="08:00")
    watched = (ans("UT", conn(r2x), when="08:01"),)
    t = on_location(s, ctx(at("08:01")), event("AAA", "enter", "08:01"), origin, watched)
    assert t.state.lock is not None and numbers(t.state) != ["1300", "6666"]


def test_late_leave_after_the_auto_train_left_keeps_the_lock():
    # the phone says "left home" only after the usual train left: a late fix, so the lock stays
    s = occ(journey([L1, L2], RIDE))
    t = locate(s, "08:14", event("home", "leave", "08:13"), USUAL, LATER)
    assert t.state == s and t.events == ()
    # a really late user is still caught at the station: there the next good train takes over
    t = locate(t.state, "08:16", event("AAA", "enter", "08:16"), LATER)
    assert t.state.lock is not None and t.state.lock.source is LockSource.STATION
    assert numbers(t.state) == ["1300", "6666"]


def at_station(t: Transition) -> list[str]:
    return [e.text for e in t.events if e.kind is EventKind.AT_STATION]


def test_station_enter_sends_one_silent_status():  # Q1
    t = locate(occ(journey([L1, L2])), "08:00", event("AAA", "enter", "08:00"), USUAL)
    assert kinds(t) == [(EventKind.AT_STATION, Severity.SILENT)]
    assert t.events[0].text == (
        "Office A: at the station: your 08:12 to RTD, change at UT, on time per NS, arrives 09:24."
    )
    assert t.state.all_good_evaluated  # the check-in would only repeat it


def test_station_status_not_repeated_on_bounce_or_reports():  # Q1
    s = locate(occ(journey([L1, L2])), "08:00", event("AAA", "enter", "08:00"), USUAL).state
    for kind, when in (("leave", "08:01"), ("enter", "08:02"), ("report", "08:04")):
        t = locate(s, when, event("AAA", kind, when), USUAL)
        assert t.events == (), kind
        s = t.state


def test_station_status_waits_for_a_fresh_answer():  # Q1
    t = locate(occ(journey([L1, L2])), "08:00", event("AAA", "enter", "08:00"))
    assert t.events == () and t.state.at_station and "at_station#" not in t.state.alert_keys
    t = locate(t.state, "08:02", event("AAA", "report", "08:02"), USUAL)
    assert kinds(t) == [(EventKind.AT_STATION, Severity.SILENT)]


def test_station_status_uses_the_newest_platform_evidence():
    older = ans("AAA", conn(on_track(L1, "1"), L2), when="08:00")
    newer = ans("AAA", conn(on_track(L1, "7"), L2), when="08:01")
    t = on_location(
        occ(journey([L1, L2])), ctx(at("08:01")), event("AAA", "enter", "08:00"), older, (newer,)
    )
    assert "platform 7" in t.events[0].text


def test_fresh_station_reports_follow_a_delayed_departure():
    delayed = conn(
        make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=35, delay_arr=35),
        make_leg("UT", "RTD", "08:50", "09:24", number="5678", delay_dep=35, delay_arr=35),
    )
    s = locate(occ(journey([L1, L2])), "08:05", event("AAA", "enter", "08:05"), delayed).state
    s = locate(s, "08:40", event("AAA", "report", "08:40"), delayed).state
    assert s.zone_reports["AAA"] == at("08:40")
    t = poll(s, ctx(at("08:48")), ans("AAA", delayed, when="08:48"))
    assert not any(e.kind is EventKind.NEXT_CONNECTION for e in t.events)
    left = locate(s, "08:41", event("AAA", "leave", "08:41"), delayed).state
    assert "AAA" not in left.zone_reports


def test_leaving_home_removes_only_older_presence():
    s = replace(occ(journey([L1, L2])), zone_reports={"home": at("08:10")})
    left = locate(s, "08:13", event("home", "leave", "08:13"), USUAL).state
    assert "home" not in left.zone_reports
    stale = locate(s, "08:13", event("home", "leave", "08:09"), USUAL).state
    assert stale.zone_reports["home"] == at("08:10")


def test_station_status_names_the_station_relock():  # Q1
    t = locate(occ(journey([L1, L2], RIDE)), "08:16", event("AAA", "enter", "08:16"), LATER)
    assert numbers(t.state) == ["1300", "6666"]
    told = at_station(t)
    assert len(told) == 1 and told[0].startswith("Office A: at the station: your 08:25 to RTD")


def test_missed_at_station_is_one_message_with_next_status():  # Q1, Q2
    t = locate(occ(journey([L1, L2])), "08:16", event("AAA", "report", "08:15"), USUAL, LATER)
    assert kinds(t) == [(EventKind.MISSED_FIRST_TRAIN, Severity.LOUD)]
    assert t.events[0].text == (
        "Office A: you missed the 08:12. Next: 08:25 to RTD, change at UT, on time per NS,"
        " arrives 09:39."
    )
    later = locate(t.state, "08:18", event("AAA", "report", "08:18"), LATER)
    assert at_station(later) == []


def test_status_only_after_a_miss_told_from_home():  # Q1, Q3
    s = occ(journey(LATER.legs, source=LockSource.MISSED))
    t = locate(s, "08:20", event("AAA", "enter", "08:20"), LATER)
    assert kinds(t) == [(EventKind.AT_STATION, Severity.SILENT)]
    assert "your 08:25 to RTD" in t.events[0].text


def test_no_status_for_a_cancelled_first_train():  # Q1, 6.3
    s = occ(journey([L1, L2], source=LockSource.CHOOSE))
    t = locate(s, "08:00", event("AAA", "enter", "08:00"), conn(L1X, L2))
    assert at_station(t) == [] and "at_station#" not in t.state.alert_keys


def test_return_direction_status_at_work_station():  # Q1
    p = ctx(at("17:40")).profile
    c = Ctx(p, Direction.RETURN, dated_schedule(p, Direction.RETURN, DAY, None), None, at("17:40"))
    s = occ(None, key=OccurrenceKey("p1", Direction.RETURN, DAY))
    home = make_leg("RTD", "AAA", "17:52", "18:50", number="9999")
    t = on_location(s, c, event("RTD", "enter", "17:40"), ans("RTD", conn(home), when="17:40"))
    assert t.state.lock is not None and t.state.lock.source is LockSource.STATION
    assert kinds(t) == [(EventKind.AT_STATION, Severity.SILENT)]
    assert "your 17:52 to AAA" in t.events[0].text


def home_at(when: str, kind: str = "report", s: OccurrenceState | None = None) -> OccurrenceState:
    """The usual journey with the user seen at home before the 08:12 leaves (Q3)."""
    t = locate(s or occ(journey([L1, L2])), when, event("home", kind, when), USUAL, LATER)
    assert t.events == ()  # not departed yet: nothing to say
    return t.state


def missed_home(t: Transition) -> list[str]:
    return [e.text for e in t.events if e.kind is EventKind.MISSED_FIRST_TRAIN]


def test_home_report_when_first_train_departs_relocks_with_leave_by():  # Q3
    t = poll(home_at("08:07"), ctx(at("08:13")), ans("AAA", USUAL, LATER, when="08:13"))
    assert kinds(t) == [(EventKind.MISSED_FIRST_TRAIN, Severity.LOUD)]  # no "has left" note
    assert t.events[0].text == (
        "Office A: you missed the 08:12. Next you can catch: 08:25. Leave by 08:15."
    )
    assert numbers(t.state) == ["1300", "6666"]
    assert t.state.lock is not None and t.state.lock.source is LockSource.MISSED
    assert t.state.retained == () and not t.state.travelling


def test_home_report_after_departure_is_missed_on_the_event():  # Q3
    # the departure poll had no fresh presence; the 08:12 is already followed as left
    s = occ(journey([L1, L2], RIDE))
    t = locate(s, "08:13", event("home", "report", "08:13"), USUAL, LATER)
    assert missed_home(t) == [
        "Office A: you missed the 08:12. Next you can catch: 08:25. Leave by 08:15."
    ]
    assert numbers(t.state) == ["1300", "6666"] and t.state.retained == ()


def test_still_event_counts_as_presence():  # Q3, Q10
    s = home_at("08:08", kind="still")
    assert s.zone_reports["home"] == at("08:08")
    t = poll(s, ctx(at("08:13")), ans("AAA", USUAL, LATER, when="08:13"))
    assert kinds(t) == [(EventKind.MISSED_FIRST_TRAIN, Severity.LOUD)]


def test_still_inside_half_a_small_buffer_is_a_miss():  # Q3 review: the return buffer is 2
    def small(c: Ctx) -> Ctx:
        p = c.profile
        return replace(c, profile=replace(p, outbound=replace(p.outbound, buffer_min=2)))

    still = event("home", "still", "08:11")  # the shell's vouch at half the buffer
    s = on_location(
        occ(journey([L1, L2])),
        small(ctx(at("08:11"))),
        still,
        ans("AAA", USUAL, LATER, when="08:11"),
    ).state
    t = poll(s, small(ctx(at("08:13"))), ans("AAA", USUAL, LATER, when="08:13"))
    assert kinds(t) == [(EventKind.MISSED_FIRST_TRAIN, Severity.LOUD)]


def test_stale_home_report_at_departure_keeps_the_has_left_note():  # Q3
    t = poll(home_at("08:00"), ctx(at("08:13")), ans("AAA", USUAL, LATER, when="08:13"))
    assert missed_home(t) == [] and numbers(t.state) == ["1234", "5678"]
    assert [e.kind for e in t.events] == [EventKind.NEXT_CONNECTION]


def test_left_home_before_departure_is_not_missed_from_home():  # Q3
    s = locate(home_at("08:07"), "08:08", event("home", "leave", "08:08"), USUAL, LATER).state
    t = poll(s, ctx(at("08:13")), ans("AAA", USUAL, LATER, when="08:13"))
    assert missed_home(t) == []


def test_home_report_reachable_by_buffer_is_not_missed():  # Q3
    # seen at 08:03 with a 10-min buffer: the 08:12, 3 min late, was still within reach
    late = conn(make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=3), L2)
    t = poll(home_at("08:03"), ctx(at("08:16")), ans("AAA", late, LATER, when="08:16"))
    assert missed_home(t) == []


def test_delayed_train_still_at_platform_is_not_missed_from_home():  # Q3
    late = conn(make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=5), L2)
    t = poll(home_at("08:10"), ctx(at("08:13")), ans("AAA", late, LATER, when="08:13"))
    assert missed_home(t) == [] and numbers(t.state) == ["1234", "5678"]


def test_home_with_no_catchable_train_sends_one_message():  # Q3
    t = poll(home_at("08:07"), ctx(at("08:13")), ans("AAA", USUAL, when="08:13"))
    assert kinds(t) == [(EventKind.MISSED_FIRST_TRAIN, Severity.LOUD)]
    assert t.events[0].text.endswith(
        "you missed the 08:12. No train you can still catch was found, check the NS app."
    )
    assert t.state.lock is not None and t.state.lock.legs[0].state is LegState.MISSED
    again = poll(t.state, ctx(at("08:15")), ans("AAA", USUAL, when="08:15"))
    assert missed_home(again) == []


def test_return_direction_uses_the_work_zone():  # Q3
    p = ctx(at("17:40")).profile

    def c(now: str) -> Ctx:
        return Ctx(
            p, Direction.RETURN, dated_schedule(p, Direction.RETURN, DAY, None), None, at(now)
        )

    home = make_leg("RTD", "AAA", "17:52", "18:50", number="9999")
    later = make_leg("RTD", "AAA", "18:22", "19:20", number="9998")
    key = OccurrenceKey("p1", Direction.RETURN, DAY)
    s = occ(journey([home]), key=key)
    a = ans("RTD", conn(home), conn(later), when="17:50")
    s = on_location(s, c("17:50"), event("work", "report", "17:50"), a).state
    t = poll(s, c("17:53"), ans("RTD", conn(home), conn(later), when="17:53"))
    assert missed_home(t) == [
        "Office A: you missed the 17:52. Next you can catch: 18:22. Leave by 18:17."
    ]


async def test_send_zone_events_marks_undelivered_and_a_raise_ends_the_rest():
    sent, lifted = [], []

    async def send(e):
        sent.append(e.kind)
        if e.kind == "report":
            raise RuntimeError
        return e.kind != "leave"

    at_ = at("08:00")
    await send_zone_events([("a", "leave"), ("b", "enter")], at_, send, lambda: lifted.append(1))
    assert sent == ["leave", "enter"] and lifted == [1]
    with pytest.raises(RuntimeError):
        events = [("a", "report"), ("b", "enter")]
        await send_zone_events(events, at_, send, lambda: lifted.append(2))
    assert sent == ["leave", "enter", "report"] and lifted == [1, 2]
