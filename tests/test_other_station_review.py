"""Other boarding station and window mode: the cases the 0.3.0 review found. Each one failed
before its fix. Loud messages must never come for a train the user was never near, and an
unanswered question must change nothing."""

from dataclasses import replace
from datetime import timedelta

import runtime_seam as seam
from builders import make_connection, make_leg
from lifecycle_fixtures import at as fixture_at
from lifecycle_fixtures import ctx as fixture_ctx
from lifecycle_fixtures import usual_locked
from runtime_rig import at
from test_never_boarded import KEY, replay_to_departure
from test_other_station import HOME, NEAR_VL, PLACES, ROAD, RTD, VL, Phone, at_venlo, loud_after, of
from test_other_station import rig_for as station_rig
from test_window_mode import FIXED_OUT, STAY_RET, commute_day, first_train, stay_start
from test_window_mode import rig_for as window_rig

from train_commute_companion.disruptions import update_disruptions
from train_commute_companion.location import Evidence, Fix, LocationEvent
from train_commute_companion.model import Direction, DirectionState, EventKind
from train_commute_companion.providers.base import (
    ActiveDisruption,
    ActiveDisruptions,
    ProviderError,
)
from train_commute_companion.view import track_fix


def down(q):
    return ProviderError("down")


# --- an unanswered or failed question changes nothing ---


async def test_other_train_with_no_pick_stays_quiet(tmp_path):
    rig, phone = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "other") is None
    s = rig.occ(KEY)
    assert s.boarding_station is None and s.prompt.stage == "pick"
    assert await phone.fix(VL, "19:58") == [("VL", "report")]  # at the station, still no pick
    assert rig.occ(KEY).lock is None
    await rig.run_until("21:35")
    assert loud_after(rig, "19:49") == []


async def test_yes_with_ns_failing_can_be_tried_again(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    rig.provider.error = down
    refused = await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    assert refused is not None and "try again" in refused.text
    s = rig.occ(KEY)
    assert s.prompt is not None and s.boarding_station is None and s.lock is None
    rig.provider.error = lambda q: None
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    s = rig.occ(KEY)
    assert s.boarding_station == "VL" and s.lock.legs[0].train.train_number == "6066"
    assert len(of(rig, EventKind.STATION_REMEMBER)) == 1


async def test_a_stale_yes_never_replaces_a_train_from_the_usual_station(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    rig.clock.set("19:55")  # the user went to the usual station after all
    await rig.rt.report_location("p1", LocationEvent("RTD", "enter", at("19:55")))
    s = rig.occ(KEY)
    assert s.prompt is None and s.lock.legs[0].board_station == "RTD"
    refused = await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    assert refused is not None and refused.text == "this question has expired"
    assert rig.occ(KEY).lock.legs[0].board_station == "RTD"


async def test_an_open_pick_keeps_the_usual_auto_lock(tmp_path):
    """A question asked while TCC holds its own guess: Other train and no pick leave the guess
    in place, and it is let go quietly when the user never boards it."""
    rig = await station_rig(tmp_path)
    await rig.run_until("16:20")
    phone = Phone(rig)
    await phone.fix(ROAD, "16:20")
    await phone.fix(NEAR_VL, "16:22")
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "other") is None
    s = rig.occ(KEY)
    assert s.lock.source.value == "auto" and s.lock.legs[0].board_station == "RTD"
    await rig.run_until("21:35")
    assert loud_after(rig, "16:21") == []


async def test_remember_can_be_answered_after_the_trip_ended(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    (remember,) = of(rig, EventKind.STATION_REMEMBER)
    s = rig.occ(KEY)
    rig.occ_set(replace(s, direction_state=DirectionState.DONE), KEY)  # arrived home
    assert await rig.rt.answer_prompt(remember.facts.prompt_id, "remember") is None
    assert rig.rt.state.remembered_stations == {"p1|return": ("VL",)}


# --- a known station followed by itself, never loud for a train the user was not near ---


async def test_venue_near_a_remembered_station_sends_nothing_loud(tmp_path):
    rig = await station_rig(tmp_path)
    st = rig.rt.state
    seam.seed_state(rig.rt, remembered_stations={**st.remembered_stations, "p1|return": ("VL",)})
    await replay_to_departure(rig)  # left by car at 17:40
    await rig.run_until("18:00")
    phone = Phone(rig)
    await phone.fix(ROAD, "18:00", ev=Evidence(speed=15.0))
    walk = Evidence(activity="Walking", activity_at=at("18:10"))
    await phone.fix(NEAR_VL, "18:10", ev=walk)  # parked at a venue 400 m from VL
    for t in ("18:30", "18:50", "19:10", "19:30", "19:45", "20:00", "20:15", "20:30"):
        await rig.run_until(t)
        await phone.fix(NEAR_VL, t, ev=Evidence(activity="Stationary", activity_at=at(t)))
    await rig.run_until("20:31")
    assert loud_after(rig, "18:09") == []


async def test_walk_past_a_known_station_is_let_go_quietly(tmp_path):
    rig = await station_rig(tmp_path, other="other_stations: [VL]")
    await replay_to_departure(rig)
    await rig.run_until("19:30")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:30")
    walk = Evidence(activity="Walking", activity_at=at("19:32"))
    await phone.fix(NEAR_VL, "19:32", ev=walk)
    assert rig.occ(KEY).lock is not None
    await phone.fix(ROAD, "19:34", ev=walk)
    await rig.run_until("21:45")
    assert loud_after(rig, "19:31") == []


async def test_known_station_fix_inside_the_circle_commits_and_q12_applies(tmp_path):
    rig = await station_rig(tmp_path, other="other_stations: [VL]")
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    assert await phone.fix(VL, "19:55") == [("VL", "report")]  # inside the usual radius
    s = rig.occ(KEY)
    assert s.lock.commitment.value == "committed" and s.lock.legs[0].train.train_number == "6066"
    await rig.run_until("20:18")
    still = Evidence(activity="Stationary", activity_at=at("20:18"))
    await phone.fix(VL, "20:18", ev=still)  # still at Venlo 12 min after the 20:06 left
    (miss,) = [e for e in of(rig, EventKind.MISSED_FIRST_TRAIN) if e.created_at > at("19:49")]
    assert "you missed the 20:06" in miss.text


async def test_riding_an_untracked_train_through_stations_asks_once(tmp_path):
    rig = await station_rig(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    stops = {"S1": (51.45, 6.05), "S2": (51.55, 5.95), "S3": (51.62, 5.70)}
    rig.provider.positions = {**rig.provider.positions, **stops}
    for code in stops:
        for dep, n in (("20:20", "1"), ("20:40", "2"), ("21:00", "3")):
            leg = make_leg(code, "AAA", dep, "21:30", number=code + n)
            rig.provider.world.append(make_connection([leg]))
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    for code, t in (("S1", "20:15"), ("S2", "20:30"), ("S3", "20:45")):
        await rig.run_until(t)
        lat, lon = stops[code]
        await phone.fix((lat + 0.001, lon), t, acc=20)  # speed 0 at each stop
    assert len(of(rig, EventKind.STATION_ASK)) == 1


async def test_riding_through_a_known_station_sends_nothing_loud(tmp_path):
    rig = await station_rig(tmp_path, other="other_stations: [S2]")
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    s2 = (51.55, 5.95)
    rig.provider.positions = {**rig.provider.positions, "S2": s2}
    for dep, n in (("20:31", "1"), ("21:01", "2")):
        leg = make_leg("S2", "AAA", dep, "21:40", number="S2" + n)
        rig.provider.world.append(make_connection([leg]))
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await rig.run_until("20:30")
    await phone.fix((s2[0] + 0.001, s2[1]), "20:30", acc=20)  # the train stopped at S2
    await rig.run_until("20:33")
    await phone.fix((51.60, 5.80), "20:33", acc=20, ev=Evidence(speed=25.0))
    await rig.run_until("21:50")
    assert loud_after(rig, "20:29") == []


# --- what a fix must be to notice a station ---


async def test_unknown_accuracy_notices_no_station(tmp_path):
    rig = await station_rig(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    view = rig.rt.view()
    p = next(p for p in view.profiles if p.id == "p1")
    epoch = view.location_epochs.get("p1", 0)
    seeded = track_fix(view, p, epoch, Fix(*ROAD, at("19:48"), 30), None, PLACES)
    assert seeded is not None
    got = track_fix(view, p, epoch, Fix(*NEAR_VL, at("19:50"), None), seeded[0], PLACES)
    assert got is None or ("VL", "enter") not in got[1]


async def test_nearest_station_decides_before_the_trip_ends_are_left_out(tmp_path):
    """300 m from the usual station (outside its circle) and 450 m from another: the nearest is
    where the trip starts, so no other station is noticed."""
    rig = await station_rig(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    rig.provider.positions = {**rig.provider.positions, "OT": (RTD[0] - 0.0068, RTD[1])}
    view = rig.rt.view()
    p = next(p for p in view.profiles if p.id == "p1")
    epoch = view.location_epochs.get("p1", 0)
    seeded = track_fix(view, p, epoch, Fix(*ROAD, at("19:48"), 30), None, PLACES)
    assert seeded is not None
    near_rtd = (RTD[0] - 0.0027, RTD[1])
    got = track_fix(view, p, epoch, Fix(*near_rtd, at("19:50"), 20), seeded[0], PLACES)
    assert got is None or ("OT", "enter") not in got[1]


# --- Q11 and Q12 at an other station ---


async def test_origin_place_cannot_prove_a_miss_at_an_other_station(tmp_path):
    loc = "{source: ha, entity: person.me, wifi: {work: work_wifi}}"
    rig, _ = await at_venlo(tmp_path, location=loc)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    await rig.run_until("20:19")
    rig.clock.set("20:19")  # the work place, 13 min after the 20:06 left Venlo
    ev = Evidence(wifi=True, activity="Stationary", activity_at=at("20:19"))
    await rig.rt.report_location("p1", LocationEvent("work", "still", at("20:19"), ev))
    assert [e for e in of(rig, EventKind.MISSED_FIRST_TRAIN) if e.created_at > at("19:49")] == []
    assert rig.occ(KEY).lock.legs[0].train.train_number == "6066"


def test_line_problems_at_the_confirmed_boarding_station_with_no_train_yet():
    context = fixture_ctx(fixture_at("07:50"))
    feed = ActiveDisruptions(
        (ActiveDisruption("i1", "Signal fault", frozenset({"VL"}), None),), "complete"
    )
    s = replace(usual_locked(), lock=None)
    assert not update_disruptions(s, context, feed).events  # VL is no station of this trip
    t = update_disruptions(replace(s, boarding_station="VL"), context, feed)
    assert [e.kind for e in t.events] == [EventKind.DISRUPTION]


async def test_questions_are_sent_during_quiet_hours(tmp_path):
    rig = await station_rig(tmp_path, extra='    alerts: {quiet_hours: ["19:00", "23:00"]}\n')
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    await rig.run_until("19:52")
    assert [e.kind for e in rig.notifier.attempts if e.kind is EventKind.STATION_ASK] == [
        EventKind.STATION_ASK
    ]


async def test_on_leave_still_tells_line_problems_before_leaving(tmp_path):
    on_leave = '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, pick: on_leave}'
    rig = await window_rig(tmp_path, out=on_leave)
    rig.provider.disruptions = ActiveDisruptions(
        (ActiveDisruption("i1", "Signal fault", frozenset({"AAA"}), None),), "complete"
    )
    await rig.run_until("07:30")
    assert rig.occ("p1|outbound|2026-10-27").lock is None
    assert any(e.kind is EventKind.DISRUPTION for e in rig.rt.events_log)


# --- window mode ---


async def test_window_earliest_takes_the_earliest_train_not_the_usual_route(tmp_path):
    out = '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, transfers: [UT]}'
    direct = make_connection([make_leg("AAA", "RTD", "07:00", "07:40", number="700")])
    first = make_leg("AAA", "UT", "07:30", "07:50", number="730")
    second = make_leg("UT", "RTD", "08:00", "08:20", number="800")
    world = [direct, make_connection([first, second])]
    rig = await window_rig(tmp_path, out=out, extra="    transfer_min: {UT: 4}\n", world=world)
    await rig.run_until("06:37")
    assert first_train(rig) == "07:00"


async def test_a_day_can_turn_off_the_minimum_stay(tmp_path):
    ret = STAY_RET.replace(
        "min_stay_min: 480", "min_stay_min: 480, by_weekday: {tue: {min_stay_min: 0}}"
    )
    rig = await window_rig(tmp_path, FIXED_OUT, ret, now="07:50", world=commute_day())
    await rig.run_until("07:58")
    await rig.rt.report_location("p1", LocationEvent("home", "leave", at("07:58")))
    await rig.run_until("14:50")
    assert stay_start(rig) == "15:00"  # 2026-10-27 is a Tuesday: no stay that day


async def test_a_stay_past_the_latest_time_makes_misses_silent(tmp_path):
    out = '{first_train: "12:22", buffer_min: 10}'
    noon = make_connection([make_leg("AAA", "RTD", "12:22", "13:00", number="1222")])
    rig = await window_rig(tmp_path, out, STAY_RET, now="11:50", world=[noon])
    await rig.run_until("12:05")
    rig.clock.set("12:05")
    await rig.rt.report_location("p1", LocationEvent("AAA", "enter", at("12:05")))
    await rig.run_until("14:50")
    s = rig.rt.state.occurrences["p1|return|2026-10-27"]
    assert rig.rt._ctx(s, rig.clock.now()).schedule.pick == "earliest_silent"


async def test_a_revised_train_arrival_moves_the_minimum_stay(tmp_path):
    rig = await window_rig(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    await rig.run_until("07:58")
    rig.clock.set("07:58")
    await rig.rt.report_location("p1", LocationEvent("home", "leave", at("07:58")))
    await rig.run_until("08:05")
    rig.clock.set("08:05")
    await rig.rt.report_location("p1", LocationEvent("AAA", "enter", at("08:05")))
    await rig.run_until("09:10")
    record = rig.rt.state.journey_records["p1|outbound|2026-10-27"]
    assert record.train_arrival is not None
    first = record.train_arrival
    later = make_connection([make_leg("AAA", "RTD", "08:12", "08:50", number="812", delay_arr=20)])
    rig.provider.world = [later, *rig.provider.world[1:]]
    await rig.run_until("09:40")
    record = rig.rt.state.journey_records["p1|outbound|2026-10-27"]
    assert record.train_arrival is not None and record.train_arrival > first
    assert record.train_arrival - first == timedelta(minutes=20)


async def test_asked_once_per_station_also_after_no(tmp_path):
    rig, phone = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "no") is None
    await phone.fix(ROAD, "19:53")
    assert await phone.fix(NEAR_VL, "19:55") == [("VL", "enter")]
    assert len(of(rig, EventKind.STATION_ASK)) == 1


async def test_a_committed_lock_is_followed_for_each_occurrence(tmp_path):
    """The view shows the zone when any trip of the profile is open; each occurrence still
    judges its own lock, so a committed one never asks."""
    from train_commute_companion.lifecycle import other_station_candidate

    rig = await station_rig(tmp_path)
    await rig.run_until("16:20")
    s = rig.occ(KEY)
    ctx = rig.rt._ctx(s, rig.clock.now())
    event = LocationEvent("VL", "enter", at("16:20"))
    assert other_station_candidate(s, ctx, event)
    committed = replace(s, lock=replace(s.lock, commitment=s.lock.commitment.COMMITTED))
    assert not other_station_candidate(committed, ctx, event)


async def test_yes_asks_ns_once_and_keeps_the_question_on_failure(tmp_path):
    """The answer that checked the train is the one the choice is made from: no second request
    can fail after the station was confirmed."""
    rig = await station_rig(tmp_path)
    await rig.run_until("16:20")
    phone = Phone(rig)
    await phone.fix(ROAD, "16:20")
    await phone.fix(NEAR_VL, "16:22")
    (ask,) = of(rig, EventKind.STATION_ASK)
    calls = len(rig.provider.calls)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    assert [q.origin for q in rig.provider.calls[calls:]] == ["VL"]
    s = rig.occ(KEY)
    assert s.boarding_station == "VL" and s.lock.legs[0].train.train_number == "6036"


async def test_real_boarding_at_a_known_station_with_one_far_fix(tmp_path):
    """The 11-18 evening with VL known: one fix 400 m out, then fixes on the train. The train is
    followed, nothing is loud, and the phone at home is the arrival."""
    rig = await station_rig(tmp_path, other="other_stations: [VL]")
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    assert rig.occ(KEY).lock.legs[0].train.train_number == "6066"
    await rig.run_until("20:10")
    await phone.fix((51.45, 5.95), "20:10", ev=Evidence(speed=30.0))  # on the train
    await rig.run_until("21:20")
    await phone.fix(HOME, "21:20")
    await rig.run_until("22:00")
    assert loud_after(rig, "19:49") == []
    told = [e.text for e in rig.rt.events_log if e.created_at > at("19:49")]
    assert any("following your 20:06" in t for t in told)
    record = rig.rt.state.journey_records["p1|return|2026-10-27"]
    assert record.seen_at == at("21:20")


async def _one_precise_fix_at_venlo(tmp_path, other: str = "", remembered: bool = False):
    """One 30 m fix at the Venlo station point and then a phone at rest: the next train leaves
    after the 45 min errand timeout would have let a provisional lock go."""
    rig = await station_rig(tmp_path, other=other)
    if remembered:
        st = rig.rt.state
        seam.seed_state(
            rig.rt, remembered_stations={**st.remembered_stations, "p1|return": ("VL",)}
        )
    late = make_connection([make_leg("VL", "AAA", "20:36", "21:36", number="6096")])
    rig.provider.world = [*(c for c in rig.provider.world if c.origin != "VL"), late]
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(VL, "19:50")
    s = rig.occ(KEY)
    assert s.boarding_station == "VL" and s.at_station
    assert s.lock.commitment.value == "committed" and s.lock.legs[0].train.train_number == "6096"
    await rig.run_until("20:40")
    s = rig.occ(KEY)
    assert s.boarding_station == "VL" and s.lock.legs[0].train.train_number == "6096"
    assert loud_after(rig, "19:49") == []


async def test_one_precise_fix_at_a_configured_station_commits(tmp_path):
    await _one_precise_fix_at_venlo(tmp_path, other="other_stations: [VL]")


async def test_one_precise_fix_at_a_remembered_station_commits(tmp_path):
    await _one_precise_fix_at_venlo(tmp_path, remembered=True)


async def test_a_refused_pick_changes_nothing(tmp_path):
    """The picked train is gone from NS data by the time it is tapped: the refusal leaves the
    station, travel facts and question as they were, and no poll follows another train."""
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "other") is None
    before = rig.occ(KEY)
    assert before.prompt.stage == "pick" and ("t2006", "20:06") in before.prompt.choices
    rig.provider.world = [c for c in rig.provider.world if c.legs[0].train.train_number != "6066"]
    refused = await rig.rt.answer_prompt(before.prompt.id, "t2006")
    assert refused is not None
    s = rig.occ(KEY)
    assert (s.lock, s.boarding_station, s.travelling, s.at_station, s.prompt) == (
        before.lock,
        before.boarding_station,
        before.travelling,
        before.at_station,
        before.prompt,
    )
    await rig.run_until("20:10")
    s = rig.occ(KEY)
    assert s.boarding_station is None
    assert s.lock is None or s.lock.legs[0].board_station != "VL"


async def test_a_leave_while_ns_answers_voids_the_yes(tmp_path):
    """I'm leaving commits the usual station's train while NS answers the Yes: the answer that
    comes back must not replace it."""
    rig = await station_rig(tmp_path)
    await rig.run_until("16:20")
    phone = Phone(rig)
    await phone.fix(ROAD, "16:20")
    await phone.fix(NEAR_VL, "16:22")
    (ask,) = of(rig, EventKind.STATION_ASK)
    usual = rig.occ(KEY).lock.legs[0].train

    async def leave_meanwhile(q) -> None:
        rig.provider.hook = None
        assert await rig.rt.leaving("p1", direction=Direction.RETURN) is None

    rig.provider.hook = leave_meanwhile
    refused = await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    assert refused is not None
    s = rig.occ(KEY)
    assert s.lock.legs[0].train == usual and s.lock.commitment.value == "committed"
    assert s.boarding_station is None
