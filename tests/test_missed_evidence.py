"""Q12 (2026-11-17): a missed train is decided from several phone signals, not one late fix.
Synthetic times and stations; the evening it replays rode the train it was told it missed."""

import json
from dataclasses import replace

from builders import ONE_TRANSFER, make_connection, make_leg, only_occurrence
from lifecycle_fixtures import DAY, L1, L2, LATER, ans, conn, ctx, journey, occ, poll
from location_fixtures import USUAL, event
from runtime_rig import PROFILE, Rig, at, started

from train_commute_companion.lifecycle import on_location
from train_commute_companion.location import NO_EVIDENCE, Evidence, LocationEvent
from train_commute_companion.messages import name_stations
from train_commute_companion.model import (
    Ctx,
    Direction,
    EventKind,
    LegState,
    LocationSettings,
    OccurrenceKey,
    Severity,
)
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.serde import decode, encode
from train_commute_companion.state import OccurrenceState

# the 08:12 leaves a minute late, at 08:13 (as the evening's train did)
LATE_BY_ONE = (
    make_connection(
        [
            make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=1),
            ONE_TRANSFER[0].legs[1],
        ]
    ),
    ONE_TRANSFER[1],
)


def kinds_of(rig: Rig) -> list[EventKind]:
    return [e.kind for e in rig.rt.events_log]


def missed(rig: Rig) -> list[tuple[str, Severity]]:
    return [
        (e.text, e.severity)
        for e in rig.rt.events_log
        if e.kind in (EventKind.MISSED_FIRST_TRAIN, EventKind.MISSED_TRANSFER)
    ]


async def report(rig: Rig, zone: str, kind: str, hhmm: str, ev: Evidence = NO_EVIDENCE) -> None:
    rig.clock.set(hhmm)
    await rig.rt.report_location("p1", LocationEvent(zone, kind, at(hhmm), ev))  # type: ignore[arg-type]


async def test_late_station_fix_then_coarse_exit_follows_the_train_to_the_end(tmp_path):
    rig = await started(tmp_path)
    rig.provider.world = list(LATE_BY_ONE)
    await rig.run_until("08:11")
    await report(rig, "AAA", "enter", "08:11:19")  # at the station before it leaves
    await rig.run_until("08:16")
    await report(rig, "AAA", "report", "08:16:17")  # 3.3 min after 08:13: a late fix, no speed
    await rig.run_until("08:22")
    # the next fix is cell-level kilometres down the line: the shell sends it as a leave
    await report(rig, "AAA", "leave", "08:22:13")
    await rig.run_until("08:40")
    await report(rig, "UT", "enter", "08:40:30")
    await rig.run_until("08:52")
    await report(rig, "UT", "leave", "08:52")
    await rig.run_until("09:26")
    await report(rig, "RTD", "enter", "09:26")
    await rig.run_until("09:45")
    assert missed(rig) == []
    assert EventKind.NEXT_CONNECTION not in kinds_of(rig)  # it waited at AAA (Q4)
    s = only_occurrence(rig.rt)
    assert [x.train.train_number for x in s.lock.legs] == ["1234", "5678"]
    assert [x.state for x in s.lock.legs] == [LegState.DONE, LegState.DONE]


async def test_true_miss_with_the_phone_at_rest_is_one_loud_miss_at_twelve_minutes(tmp_path):
    rig = await started(tmp_path)
    rig.provider.world = list(ONE_TRANSFER)
    await rig.run_until("08:05")
    await report(rig, "AAA", "enter", "08:05")  # then the phone lies still: no more fixes
    await rig.run_until("08:24")
    assert missed(rig) == []
    await report(rig, "AAA", "still", "08:24")  # the shell's vouch at departure + 12
    await rig.run_until("08:30")
    assert missed(rig) == [
        (
            (
                "Office A: you missed the 08:12. Next: 08:44 to RTD, change at UT, on time per NS,"
                " arrives 09:50."
            ),
            Severity.LOUD,
        )
    ]
    assert EventKind.NEXT_CONNECTION not in kinds_of(rig)
    assert only_occurrence(rig.rt).retained == ()  # dropped once confirmed (Q2)


async def test_home_wifi_at_departure_is_an_immediate_miss_with_leave_by(tmp_path):
    rig = await started(tmp_path)
    rig.write(text=PROFILE.replace("NAME", "office_a").replace("buffer_min: 10", "buffer_min: 20"))
    await rig.rt.reload_if_changed()
    rig.provider.world = list(ONE_TRANSFER)
    await rig.run_until("08:05")
    await report(rig, "home", "report", "08:05")
    await rig.run_until("08:12")
    await report(rig, "home", "still", "08:12:10", Evidence(wifi=True))  # connected at departure
    assert missed(rig) == [
        (
            "Office A: you missed the 08:12. Next you can catch: 08:44. Leave by 08:24.",
            Severity.LOUD,
        )
    ]
    assert EventKind.NEXT_CONNECTION not in kinds_of(rig)


async def test_station_bounce_after_departure_is_still_a_miss_at_twelve_minutes(tmp_path):
    rig = await started(tmp_path)
    rig.provider.world = list(ONE_TRANSFER)
    await rig.run_until("08:05")
    await report(rig, "AAA", "enter", "08:05")  # waits on the platform
    await rig.run_until("08:15")
    await report(rig, "AAA", "leave", "08:15")  # a GPS bounce over the circle's edge
    await report(rig, "AAA", "enter", "08:15:40")  # back in: the leave is no riding sign
    await rig.run_until("08:24")
    await report(rig, "AAA", "still", "08:24")
    await rig.run_until("08:30")
    assert [sev for _, sev in missed(rig)] == [Severity.LOUD]
    assert only_occurrence(rig.rt).lock.legs[0].train.train_number == "1236"


async def test_place_wifi_proves_nothing_with_a_buffer_inside_its_lag(tmp_path):  # rule 3a
    async def told(buffer: int, kind: str, hhmm: str) -> list[tuple[str, Severity]]:
        (path := tmp_path / f"{buffer}{kind}").mkdir()
        rig = await started(path)
        text = PROFILE.replace("NAME", "office_a").replace(
            "buffer_min: 10", f"buffer_min: {buffer}"
        )
        rig.write(text=text)
        await rig.rt.reload_if_changed()
        rig.provider.world = list(ONE_TRANSFER)
        await rig.run_until("08:08")
        await report(rig, "home", "report", "08:08")
        await rig.run_until(hhmm[:5])
        # the user may have left minutes ago: the Wi-Fi tracker says "not home" up to 5 min late
        await report(rig, "home", kind, hhmm, Evidence(wifi=True))
        return missed(rig)

    assert await told(5, "still", "08:12:15") == []  # the departed vouch
    assert await told(2, "report", "08:15:20") == []  # a lagging home fix
    assert [sev for _, sev in await told(6, "still", "08:12:15")] == [Severity.LOUD]


async def test_automotive_activity_after_departure_means_riding(tmp_path):
    rig = await started(tmp_path)
    rig.provider.world = list(ONE_TRANSFER)
    await rig.run_until("08:05")
    await report(rig, "AAA", "enter", "08:05")
    await rig.run_until("08:16")
    riding = Evidence(activity="Automotive", activity_at=at("08:15"))
    await report(rig, "AAA", "report", "08:16", riding)  # still inside the station circle
    await rig.run_until("08:24")
    await report(rig, "AAA", "still", "08:24")
    await rig.run_until("08:30")
    assert missed(rig) == []
    assert only_occurrence(rig.rt).lock.legs[0].train.train_number == "1234"


async def test_stationary_activity_in_the_zone_three_minutes_after_is_a_miss(tmp_path):
    rig = await started(tmp_path)
    rig.provider.world = list(ONE_TRANSFER)
    await rig.run_until("08:05")
    await report(rig, "AAA", "enter", "08:05")
    await rig.run_until("08:15")
    await report(
        rig, "AAA", "report", "08:15", Evidence(activity="Stationary", activity_at=at("08:14"))
    )
    assert [sev for _, sev in missed(rig)] == [Severity.LOUD]


def test_at_rest_activity_needs_the_grace_and_a_report_after_departure():  # Q12 3b
    def told(report_at: str, activity_at: str) -> bool:
        ev = Evidence(activity="Walking", activity_at=at(activity_at))
        e = LocationEvent("AAA", "report", at(report_at), ev)
        t = on_location(occ(journey([L1, L2])), ctx(at(report_at)), e, ans("AAA", USUAL, LATER))
        return any(x.kind is EventKind.MISSED_FIRST_TRAIN for x in t.events)

    assert told("08:15", "08:13")
    assert not told("08:14:59", "08:13")  # inside the grace
    assert not told("08:15", "08:11")  # reported before the departure
    assert not told("08:20", "08:14:59")  # older than 5 min


async def test_no_signals_at_all_gives_only_the_silent_hedge(tmp_path):
    rig = await started(tmp_path)
    rig.provider.world = list(ONE_TRANSFER)
    await rig.run_until("08:30")
    hedges = [e for e in rig.rt.events_log if e.kind is EventKind.NEXT_CONNECTION]
    assert [e.severity for e in hedges] == [Severity.SILENT]
    assert "If you are not on it" in hedges[0].text
    assert missed(rig) == []
    assert only_occurrence(rig.rt).lock.legs[0].train.train_number == "1234"  # possibly ridden


def test_buffer_two_still_at_work_alone_is_no_loud_miss():  # Q12 rule 6
    p = ctx(at("17:40")).profile
    p = replace(p, return_=replace(p.return_, buffer_min=2))

    def c(now: str) -> Ctx:
        sched = dated_schedule(p, Direction.RETURN, DAY, None)
        return Ctx(p, Direction.RETURN, sched, None, at(now))

    home = make_leg("RTD", "AAA", "17:52", "18:50", number="9999")
    later = make_leg("RTD", "AAA", "18:22", "19:20", number="9998")
    s = occ(journey([home]), key=OccurrenceKey("p1", Direction.RETURN, DAY))
    a = ans("RTD", conn(home), conn(later), when="17:51")
    s = on_location(s, c("17:51"), event("work", "still", "17:51"), a).state
    t = poll(s, c("17:53"), ans("RTD", conn(home), conn(later), when="17:53"))
    assert not any(e.kind is EventKind.MISSED_FIRST_TRAIN for e in t.events)
    assert [e.severity for e in t.events if e.kind is EventKind.NEXT_CONNECTION] == [
        Severity.SILENT
    ]


def test_state_and_settings_written_by_0_2_4_load_with_defaults():
    s = occ(journey([L1, L2]))
    raw = json.loads(json.dumps(encode(s)))
    del raw["moved_at"], raw["wifi_reports"]  # 0.2.4 had neither
    assert decode(OccurrenceState, raw) == s
    loc = LocationSettings("ha", "person.me", {"home": "zone.home"}, 150)
    raw = encode(loc)
    del raw["wifi"], raw["activity_entity"]
    assert decode(LocationSettings, raw) == loc


LOCATED = """
profiles:
  - id: "p1"
    name: "office"
    days: [mon]
    home_station: "AAA"
    work_station: "RTD"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
    location: LOCATION
"""


def test_profile_location_wifi_and_activity_entity_are_validated():
    good = "{source: ha, entity: person.me, wifi: {home: device_tracker.phone_wifi}, activity_entity: sensor.phone_activity}"
    ps, errs = parse_profiles(LOCATED.replace("LOCATION", good), None)
    assert errs == []
    loc = ps[0].location
    assert loc is not None and loc.wifi == {"home": "device_tracker.phone_wifi"}
    assert loc.activity_entity == "sensor.phone_activity"
    for bad, path in (
        ("{source: ha, entity: person.me, wifi: {garden: device_tracker.x}}", "location.wifi"),
        ("{source: ha, entity: person.me, wifi: [device_tracker.x]}", "location.wifi"),
        ("{source: ha, entity: person.me, activity_entity: 5}", "location.activity_entity"),
        ("{source: owntracks, wifi: {home: device_tracker.x}}", "location"),
    ):
        _, errs = parse_profiles(LOCATED.replace("LOCATION", bad), None)
        assert any(e.path.endswith(path) for e in errs), (bad, errs)


# --- station numbers in messages ---


def test_name_stations_shows_catalogue_names_for_station_numbers():
    labels = {"8400206": "Eindhoven Centraal"}
    text = "your 17:06 AAA to RTD train runs only to 8400206, change there."
    assert name_stations(text, labels) == (
        "your 17:06 AAA to RTD train runs only to Eindhoven Centraal, change there."
    )
    assert name_stations("train 1234 to 8499999", labels) == "train 1234 to 8499999"  # no name


async def test_ns_provider_keeps_catalogue_names_by_station_number():
    payload = {
        "payload": [
            {"code": "EHV", "UICCode": "8400206", "namen": {"lang": "Eindhoven Centraal"}},
            {"code": "XYZ", "UICCode": "8400999", "namen": {}},
        ]
    }
    ns = NsProvider("key", None, lambda: at("08:00"))  # type: ignore[arg-type]

    async def get(path, params=None, **kw):
        return payload

    ns._get = get  # type: ignore[method-assign]
    await ns.stations()
    assert ns.station_labels == {"8400206": "Eindhoven Centraal", "8400999": "XYZ"}


async def test_cut_short_at_a_numbered_stop_is_told_by_its_name(tmp_path):
    rig = await started(tmp_path, "07:56")
    rig.provider.station_labels = {"8400206": "Eindhoven Centraal"}  # type: ignore[attr-defined]
    cut = make_leg(
        "AAA", "UT", "08:12", "08:40", number="1234", stops=["8400206"], cut_after="8400206"
    )
    rig.clock.set("08:20")
    rig.provider.world = [make_connection([cut, L2])]
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    told = [e.text for e in rig.rt.events_log if e.kind is EventKind.CUT_SHORT]
    assert told and all("runs only to Eindhoven Centraal" in t for t in told), told
    assert not any("8400206" in e.text for e in rig.rt.events_log)
