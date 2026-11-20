"""Other boarding station and window mode: the cases the final 0.3.0 review found. Each one
failed before its fix."""

from builders import make_connection, make_leg
from runtime_rig import at
from test_never_boarded import KEY, replay_to_departure
from test_other_station import NEAR_VL, ROAD, VL, Phone, of
from test_other_station import rig_for as station_rig
from test_window_mode import OUT_KEY
from test_window_mode import rig_for as window_rig

from train_commute_companion.model import EventKind, Severity
from train_commute_companion.providers.base import ProviderError

S2 = (51.55, 5.95)


def loud_sent(rig, hhmm: str) -> list:
    """Loud in the log and at the notifier, in case delivery judges severity again."""
    logged = [
        (e.kind, e.text)
        for e in rig.rt.events_log
        if e.severity is Severity.LOUD and e.created_at > at(hhmm)
    ]
    sent = [
        (e.kind, e.text)
        for e in rig.notifier.attempts
        if e.severity is Severity.LOUD and e.created_at > at(hhmm)
    ]
    return logged + sent


def _s2_trains(rig) -> None:
    rig.provider.positions = {**rig.provider.positions, "S2": S2}
    for dep, n in (("20:20", "1"), ("20:40", "2")):
        rig.provider.world.append(make_connection([make_leg("S2", "AAA", dep, "21:30", number=n)]))


# --- the 30-minute guard survives leaving a station circle ---


async def test_leaving_a_known_station_keeps_the_30_minute_guard(tmp_path):
    rig = await station_rig(tmp_path, other="other_stations: [VL]")
    _s2_trains(rig)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    assert await phone.fix(NEAR_VL, "19:50") == [("VL", "enter")]
    assert rig.occ(KEY).lock is not None  # the provisional VL guess
    assert ("VL", "leave") in await phone.fix(ROAD, "19:53")
    await phone.fix((S2[0] + 0.001, S2[1]), "19:55", acc=20)
    assert of(rig, EventKind.STATION_ASK) == []
    assert at("19:50") == rig.occ(KEY).station_visits["VL"]


# --- a retained guess keeps the silent rule; a confirmed station drops an older guess ---


async def test_a_retained_known_station_guess_stays_silent(tmp_path):
    """Known VL noticed at 19:50 by its wide circle only; NS then omits the 20:06 until the
    errand timeout keeps it retained, and shows it cancelled after."""
    rig = await station_rig(tmp_path, other="other_stations: [VL]")
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    s = rig.occ(KEY)
    assert s.lock.commitment.value == "provisional" and s.lock.legs[0].train.train_number == "6066"
    world = rig.provider.world
    rig.provider.world = [c for c in world if c.legs[0].train.train_number != "6066"]
    await rig.run_until("20:36")
    s = rig.occ(KEY)
    assert s.lock is None or s.lock.legs[0].train.train_number != "6066"
    assert [r.legs[0].train.train_number for r in s.retained] == ["6066"]
    gone = make_connection([make_leg("VL", "AAA", "20:06", "21:06", number="6066", cancelled=True)])
    rig.provider.world = [*rig.provider.world, gone]
    await rig.run_until("21:35")
    assert loud_sent(rig, "19:49") == []


async def test_yes_at_another_station_drops_the_departed_usual_guess(tmp_path):
    """TCC's own uncommitted guess, the 16:36 from the usual station, has left when the user
    says Yes to the 16:45 at VL: it is no train the user is on, so it is not kept."""
    rig = await station_rig(tmp_path)
    early = make_connection([make_leg("VL", "AAA", "16:45", "17:45", number="6016")])
    rig.provider.world = [*rig.provider.world, early]
    await rig.run_until("16:20")
    phone = Phone(rig)
    await phone.fix(ROAD, "16:20")
    await phone.fix(NEAR_VL, "16:22")
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert "16:45" in ask.text
    await rig.run_until("16:38")
    s = rig.occ(KEY)
    assert s.lock.legs[0].board_station == "RTD" and s.lock.commitment.value != "committed"
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    s = rig.occ(KEY)
    assert s.lock.legs[0].train.train_number == "6016" and s.retained == ()
    world = rig.provider.world
    rig.provider.world = [c for c in world if c.legs[0].train.train_number != "3562"]
    await rig.run_until("17:00")
    assert loud_sent(rig, "16:21") == []


# --- the watchdog never warns loudly about a train the user was not shown to take ---


def down(q):
    return ProviderError("down")


async def test_ns_failing_during_a_known_station_guess_warns_only_once_committed(tmp_path):
    rig = await station_rig(tmp_path, other="other_stations: [VL]")
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    assert rig.occ(KEY).lock.commitment.value == "provisional"
    rig.provider.error = down
    await rig.run_until("19:58")
    assert loud_sent(rig, "19:49") == []
    assert await phone.fix(VL, "19:59") == [("VL", "report")]  # inside the circle: committed
    assert rig.occ(KEY).lock.commitment.value == "committed"
    await rig.run_until("20:02")
    assert EventKind.CANNOT_SEE_TRAIN in [k for k, _ in loud_sent(rig, "19:49")]


async def test_on_leave_before_leaving_watches_no_invented_train(tmp_path):
    on_leave = '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, pick: on_leave}'
    rig = await window_rig(tmp_path, out=on_leave)
    rig.provider.error = down
    await rig.run_until("07:30")
    assert rig.occ(OUT_KEY).lock is None
    assert loud_sent(rig, "06:00") == []


async def test_fixed_day_keeps_the_usual_train_watch_before_a_lock(tmp_path):
    """0.2.6: before a lock a fixed day still watches its usual train."""
    rig = await window_rig(tmp_path, out='{first_train: "08:00", buffer_min: 10}')
    rig.provider.error = down
    await rig.run_until("07:55")
    assert EventKind.CANNOT_SEE_TRAIN in [k for k, _ in loud_sent(rig, "06:00")]
