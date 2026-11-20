"""Other boarding station and window mode: the cases the 0.3.0 second-eye audit found. Each one
failed before its fix."""

from builders import make_connection, make_leg
from runtime_rig import at
from test_never_boarded import KEY, replay_to_departure
from test_other_station import NEAR_VL, ROAD, Phone, loud_after, of
from test_other_station import rig_for as station_rig
from test_window_mode import first_train, told
from test_window_mode import rig_for as window_rig

from train_commute_companion.location import LocationEvent
from train_commute_companion.model import EventKind, Severity


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
