"""Other boarding station and window mode: the cases the 0.3.0 second-eye audit found. Each one
failed before its fix."""

from datetime import time

from builders import make_connection, make_leg
from runtime_rig import at
from test_never_boarded import KEY, replay_to_departure
from test_other_station import AAA, NEAR_VL, ROAD, RTD, VL, Phone, loud_after, of
from test_other_station import rig_for as station_rig
from test_window_mode import (
    FIXED_OUT,
    OUT_KEY,
    RET_KEY,
    STAY_RET,
    commute_day,
    evenings,
    first_train,
    report,
    ride_to_work,
    stay_start,
    told,
)
from test_window_mode import rig_for as window_rig

from train_commute_companion.lifecycle import is_resolved
from train_commute_companion.location import LocationEvent
from train_commute_companion.model import Direction, EventKind, Severity


async def known_vl_far_fix(tmp_path):
    """A known VL noticed by one fix 400 m out: TCC follows the 20:06, provisionally."""
    rig = await station_rig(tmp_path, other="other_stations: [VL]")
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    s = rig.occ(KEY)
    assert s.boarding_station == "VL" and s.lock.legs[0].train.train_number == "6066"
    assert s.lock.commitment.value != "committed"
    return rig, phone


async def test_a_vouch_for_a_wide_circle_lock_proves_no_miss(tmp_path):
    """The HA shell's track still says VL (no newer fix), so it vouches 12 min after the 20:06
    left: the wide circle never proved the user waited there, so nothing is missed or kept."""
    rig, _ = await known_vl_far_fix(tmp_path)
    rig.clock.set("20:18")
    await rig.rt.report_location("p1", LocationEvent("VL", "still", at("20:18")))
    assert [e for e in of(rig, EventKind.MISSED_FIRST_TRAIN) if e.created_at > at("19:49")] == []
    s = rig.occ(KEY)
    assert s is None or s.lock is None or s.lock.commitment.value != "committed"
    await rig.run_until("21:45")
    assert loud_after(rig, "19:49") == []


async def test_a_vouch_before_departure_does_not_commit_a_wide_circle_lock(tmp_path):
    rig, _ = await known_vl_far_fix(tmp_path)
    rig.clock.set("20:01")
    await rig.rt.report_location("p1", LocationEvent("VL", "still", at("20:01")))
    assert rig.occ(KEY).lock.commitment.value != "committed"


async def test_a_cancelled_guess_at_an_other_station_is_told_quietly(tmp_path):
    rig, _ = await known_vl_far_fix(tmp_path)
    gone = make_connection([make_leg("VL", "AAA", "20:06", "21:06", number="6066", cancelled=True)])
    rig.provider.world = [
        gone if c.legs[0].train.train_number == "6066" else c for c in rig.provider.world
    ]
    await rig.run_until("19:56")
    (cancel,) = [e for e in of(rig, EventKind.CANCELLED) if e.created_at > at("19:49")]
    assert cancel.severity is Severity.SILENT
    assert rig.occ(KEY).lock.legs[0].train.train_number == "6096"  # the relock still moves
    assert loud_after(rig, "19:49") == []


async def test_a_window_replacement_takes_the_earliest_good_train(tmp_path):
    """Window days have no usual route: a cancellation moves to the next good train, not to the
    one that matches the transfer stations."""
    out = '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, transfers: [UT]}'
    direct = make_leg("AAA", "RTD", "07:00", "07:40", number="700")
    later = make_connection([make_leg("AAA", "RTD", "07:10", "07:50", number="710")])
    first = make_leg("AAA", "UT", "07:30", "07:50", number="730")
    second = make_leg("UT", "RTD", "08:00", "08:20", number="800")
    world = [make_connection([direct]), later, make_connection([first, second])]
    rig = await window_rig(tmp_path, out=out, extra="    transfer_min: {UT: 4}\n", world=world)
    await rig.run_until("06:37")
    assert first_train(rig) == "07:00"
    gone = make_leg("AAA", "RTD", "07:00", "07:40", number="700", cancelled=True)
    rig.provider.world = [make_connection([gone]), later, make_connection([first, second])]
    await rig.run_until("06:45")
    assert first_train(rig) == "07:10"
    (cancel,) = told(rig, EventKind.CANCELLED)
    assert "new route" not in cancel[0] and "Next good connection: 07:10" in cancel[0]


async def stay_day_at_venlo(tmp_path):
    """Return window 15:00-20:00 with an 8 h stay from 09:05: watched from 16:45. At 15:30
    the phone is at VL, an unknown station."""
    rig = await window_rig(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    rig.provider.positions = {"RTD": RTD, "VL": VL, "AAA": AAA}
    rig.provider.world += [
        make_connection([make_leg("VL", "AAA", dep, arr, number=n)])
        for dep, arr, n in (("15:36", "16:36", "6036"), ("16:06", "17:06", "6066"))
    ]
    await ride_to_work(rig, phone=True)
    await rig.run_until("15:30")
    assert stay_start(rig) == "17:05"
    await rig.rt.report_location("p1", LocationEvent("VL", "enter", at("15:30")))
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert "15:36 from VL" in ask.text
    return rig, ask


async def test_yes_to_an_early_train_inside_the_stay_follows_it(tmp_path):
    rig, ask = await stay_day_at_venlo(tmp_path)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    assert first_train(rig, RET_KEY) == "15:36"


async def test_a_pick_of_an_early_train_inside_the_stay_follows_it(tmp_path):
    rig, ask = await stay_day_at_venlo(tmp_path)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "other") is None
    assert await rig.rt.answer_prompt(rig.occ(RET_KEY).prompt.id, "t1606") is None
    assert first_train(rig, RET_KEY) == "16:06"


async def test_a_choice_after_the_latest_time_is_still_refused(tmp_path):
    rig = await window_rig(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    await ride_to_work(rig, phone=True)
    await rig.run_until("15:20")
    late = await rig.rt.choose_at("p1", time(20, 30), direction=Direction.RETURN)
    assert late is not None and "outside the return window" in late.text


async def test_a_late_phone_arrival_moves_the_minimum_stay(tmp_path):
    """The 08:12 arrives 08:50; the phone first reaches work at 09:35, after following ended."""
    rig = await window_rig(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    await ride_to_work(rig, phone=False)
    await rig.run_until("09:35")
    s = rig.occ(OUT_KEY)
    assert s is None or is_resolved(s)
    await report(rig, "enter", "09:35", zone="work")
    await report(rig, "report", "10:00", zone="work")  # only the first presence counts
    assert rig.rt.state.journey_records[OUT_KEY].seen_at == at("09:35")
    await rig.run_until("14:50")
    assert stay_start(rig) == "17:35"


async def test_a_phone_at_work_the_next_morning_is_no_arrival(tmp_path):
    rig = await window_rig(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    await ride_to_work(rig, phone=False)
    await rig.run_until("12:00")
    await report(rig, "enter", "12:00", zone="work")  # over 150 min after the train arrived
    assert rig.rt.state.journey_records[OUT_KEY].seen_at is None


async def test_a_stay_ending_at_the_latest_time_keeps_the_latest_bound(tmp_path):
    """12:00 + 8 h ends at 20:00 exactly: not past the latest time, so no train after it."""
    out = '{first_train: "11:22", buffer_min: 10}'
    late = make_connection([make_leg("AAA", "RTD", "11:22", "12:00", number="1122")])
    trains = [c for c in evenings() if f"{c.legs[0].train.planned_departure:%H:%M}" != "20:00"]
    rig = await window_rig(tmp_path, out, STAY_RET, now="10:50", world=[late, *trains])
    await rig.run_until("11:05")
    await report(rig, "enter", "11:05", zone="AAA")
    await rig.run_until("14:50")
    assert stay_start(rig) == "20:00"
    s = rig.rt.state.occurrences[RET_KEY]
    assert rig.rt._ctx(s, rig.clock.now()).schedule.pick == "earliest"
    await rig.run_until("19:42")
    assert first_train(rig, RET_KEY) is None
    assert len(told(rig, EventKind.NO_CONNECTION)) == 1
