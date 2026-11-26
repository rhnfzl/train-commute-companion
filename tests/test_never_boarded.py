"""2026-11-18 evening: two confirmed misses from work, then the user left by road at 17:40 for an
event and was never at the station. The 17:36 they did not board must end quietly and leave the
rest of the window open. Times are the real ones; stations are the test world's."""

import json

import pytest
from builders import make_connection, make_leg
from lifecycle_fixtures import L1, L2, journey, occ
from runtime_rig import Rig, at

from train_commute_companion.location import NO_EVIDENCE, Evidence, LocationEvent
from train_commute_companion.model import DirectionState, EventKind, Severity
from train_commute_companion.serde import decode, encode
from train_commute_companion.state import OccurrenceState

KEY = "p1|return|2026-10-27"
PROFILE = """
profiles:
  - id: "p1"
    name: "office"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "RTD"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "16:36", buffer_min: 2, active_lead_min: 45, window: ["15:45", "21:30"]}
    location: LOCATION
"""
LOCATED = "{source: ha, entity: person.me}"


def evening(cancelled: str = "") -> list:
    delays = {"17:06": 3, "17:36": 6}
    out = []
    deps = ("16:36", "17:06", "17:36", "18:06", "18:36", "19:06", "19:36", "20:06", "20:36")
    for i, dep in enumerate(deps):
        arr = f"{int(dep[:2]) + 1}:{dep[3:]}"
        d = delays.get(dep, 0)
        n, gone = f"35{62 + 2 * i}", dep == cancelled
        leg = make_leg(
            "RTD", "AAA", dep, arr, number=n, delay_dep=d, delay_arr=d // 2, cancelled=gone
        )
        out.append(make_connection([leg]))
    return out


async def rig_for(tmp_path, location: str = LOCATED, extra: str = "") -> Rig:
    rig = Rig(tmp_path, "15:40")
    rig.write(text=PROFILE.replace("LOCATION", location) + extra)
    rig.provider.world = evening()
    await rig.rt.start()
    return rig


async def report(
    rig: Rig, kind: str, hhmm: str, ev: Evidence = NO_EVIDENCE, zone: str = "work"
) -> None:
    rig.clock.set(hhmm)
    await rig.rt.report_location("p1", LocationEvent(zone, kind, at(hhmm), ev))  # type: ignore[arg-type]


async def replay_to_departure(rig: Rig) -> None:
    await rig.run_until("16:30")
    await report(rig, "report", "16:30")
    await rig.run_until("16:35")
    await report(rig, "still", "16:35")
    await rig.run_until("16:48")
    await report(rig, "still", "16:48")  # the 16:36 left 12 min ago: missed
    await rig.run_until("17:08")
    await report(rig, "still", "17:08")
    await rig.run_until("17:21")
    await report(rig, "still", "17:21")  # the 17:06 left at 17:09: missed
    await rig.run_until("17:35")
    await report(rig, "still", "17:35")
    await rig.run_until("17:40")
    walking = Evidence(activity="Walking", activity_at=at("17:40:23"))
    await report(rig, "leave", "17:40:22", walking)  # out of the office to a car
    await report(rig, "enter", "17:42:25", Evidence(speed=12.0))  # a bounce on the way out
    road = Evidence(speed=6.0, activity="Automotive", activity_at=at("17:42:38"))
    await report(rig, "report", "17:42:39", road)
    await report(rig, "leave", "17:43:49", road)
    rig.clock.set("17:44")  # back on the rig's poll beat


def loud_after(rig: Rig, hhmm: str) -> list[tuple[EventKind, str]]:
    return [
        (e.kind, e.text)
        for e in rig.rt.events_log
        if e.severity is Severity.LOUD and e.created_at > at(hhmm)
    ]


def misses(rig: Rig) -> list[tuple[str, Severity]]:
    return [
        (e.body.splitlines()[0], e.severity)
        for e in rig.rt.events_log
        if e.kind is EventKind.MISSED_FIRST_TRAIN
    ]


async def test_a_train_never_boarded_ends_quietly_and_keeps_the_window_open(tmp_path):
    rig = await rig_for(tmp_path)
    await replay_to_departure(rig)
    assert misses(rig) == [
        ("Missed 16:36", Severity.LOUD),
        ("Missed 17:06", Severity.LOUD),  # loud again by default (2026-11-18 decision)
    ]
    await rig.run_until("19:25")  # the 17:36 arrived 18:39; its last watch ended about 19:19
    assert loud_after(rig, "17:21:30") == []
    s = rig.occ(KEY)
    assert s.direction_state is not DirectionState.DONE
    # no train is guessed again: one the user is not seen near would only bring loud news
    assert s.lock is None and s.released
    await rig.run_until("21:31")  # nobody seen at a station: the window closes, still quietly
    assert loud_after(rig, "17:21:30") == []
    assert rig.occ(KEY) is None or rig.occ(KEY).direction_state is DirectionState.DONE


async def test_after_a_release_reaching_the_station_follows_the_next_train(tmp_path):
    rig = await rig_for(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:54")
    assert rig.occ(KEY).lock is None
    await report(rig, "enter", "19:55", zone="RTD")
    s = rig.occ(KEY)
    assert s.lock is not None and s.lock.legs[0].train.train_number == "3576"  # the 20:06
    assert not s.released


async def test_after_a_release_a_leaving_tap_follows_the_next_train(tmp_path):
    rig = await rig_for(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:50")
    assert rig.occ(KEY).lock is None
    assert await rig.rt.leaving("p1") is None
    await rig.run_until("19:54")
    s = rig.occ(KEY)
    assert s.lock is not None and s.lock.legs[0].train.train_number == "3576"  # the 20:06
    assert not s.released


async def test_quiet_repeat_misses_makes_the_second_miss_from_work_silent(tmp_path):
    rig = await rig_for(tmp_path, extra="    alerts: {quiet_repeat_misses: true}\n")
    await replay_to_departure(rig)
    assert misses(rig) == [
        ("Missed 16:36", Severity.LOUD),
        ("Missed 17:06", Severity.SILENT),
    ]


@pytest.mark.parametrize(("quiet", "second"), [(True, Severity.SILENT), (False, Severity.LOUD)])
async def test_a_cancelled_replacement_does_not_reset_the_repeat_miss(tmp_path, quiet, second):
    rig = await rig_for(
        tmp_path, extra=f"    alerts: {{quiet_repeat_misses: {str(quiet).lower()}}}\n"
    )
    await rig.run_until("16:30")
    await report(rig, "report", "16:30")
    await rig.run_until("16:48")
    await report(rig, "still", "16:48")  # the 16:36 missed: the 17:06 is followed
    rig.provider.world = evening(cancelled="17:06")
    await rig.run_until("17:00")  # the 17:06 is cancelled: the 17:36 replaces it
    assert rig.occ(KEY).lock.legs[0].train.train_number == "3566"
    await rig.run_until("17:55")
    await report(rig, "still", "17:55")  # the 17:36 left at 17:42: missed too
    assert misses(rig) == [
        ("Missed 16:36", Severity.LOUD),
        ("Missed 17:36", second),
    ]


async def test_without_location_the_end_of_an_unconfirmed_train_is_still_told(tmp_path):
    rig = await rig_for(tmp_path, "null")
    await rig.run_until("19:25")
    assert [k for k, _ in loud_after(rig, "18:00")] == [EventKind.STOPPED_FOLLOWING]
    assert rig.occ(KEY).direction_state is DirectionState.DONE


def test_state_written_by_0_2_5_loads_with_nothing_released():
    s = occ(journey([L1, L2]))
    raw = json.loads(json.dumps(encode(s)))
    del raw["released"], raw["missed_from_origin"]  # 0.2.5 had no such fields
    assert decode(OccurrenceState, raw) == s
