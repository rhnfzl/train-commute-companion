import asyncio
import logging
import os
import threading

import harness
import pytest
from harness import (
    PROFILE,
    PROFILES,
    STATIONS,
    drive,
    profiles_path,
    rebound,
    setup_tcc,
    write_profiles,
)
from test_setup import tick

from custom_components.train_commute_companion.const import REFRESH_S
from custom_components.train_commute_companion.location import zones_for
from train_commute_companion.providers.base import ProviderError
from train_commute_companion.runtime import Runtime

pytestmark = pytest.mark.usefixtures("tcc_freezer")

VIA_UT = (PROFILES / "located_via_ut.yaml").read_text()
DIRECT = (PROFILES / "located_direct.yaml").read_text()
RADIUS_300 = (PROFILES / "located_300.yaml").read_text()
B_ONLY = (PROFILES / "office_b_only.yaml").read_text()
OTHER = (PROFILES / "located_via_ut_other.yaml").read_text()
TELEGRAM = (PROFILES / "located_via_ut_telegram.yaml").read_text()
OFF = (PROFILES / "located_via_ut_off.yaml").read_text()
MIXED = (PROFILES / "located_via_ut_mixed.yaml").read_text()
POSITIONS = {"AAA": (52.0, 5.0), "UT": (52.1, 5.1)}
HOME = (52.01, 5.01)
FAR = (52.5, 5.5)
PERSON = "person.someone"
HOST_LOGGER = "custom_components.train_commute_companion.host"


def set_person(hass, lat, lon, **extra):
    set_home_zone(hass)
    hass.states.async_set(PERSON, "not_home", {"latitude": lat, "longitude": lon, **extra})


def set_person_state(hass, state, coords=None, **extra):
    set_home_zone(hass)
    attrs = {} if coords is None else {"latitude": coords[0], "longitude": coords[1]}
    hass.states.async_set(PERSON, state, {**attrs, **extra})


def set_home_zone(hass):
    hass.states.async_set("zone.home", "0", {"latitude": 52.01, "longitude": 5.01, "radius": 100})


async def start_tracker(h):
    """Seeds from the person state now: the tracker restarts, and a spy records what it sends."""
    await h.hass.async_block_till_done()  # events of the setup are not under test
    h.calls = calls = []
    h.provider.positions = dict(POSITIONS)

    def install(rt: Runtime) -> None:
        orig = rt.location

        async def spy(profile_id, event, *rest, **kw):
            calls.append((event.zone, event.kind))
            await orig(profile_id, event, *rest, **kw)

        rt.location = spy

    await h.host.run(install)
    tracker = h.entry.runtime_data.tracker
    await tracker.stop()
    tracker.start()
    await h.hass.async_block_till_done()


async def spied(h):
    await h.hass.async_block_till_done()
    return h.calls


def texts(h):
    return [c.data["message"] for c in h.pushes]


def loud(call) -> bool:
    return call.data["data"]["push"]["interruption-level"] == "time-sensitive"


async def test_leaving_home_sends_leave_event(hass):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *HOME)
    await start_tracker(h)
    set_person(hass, 52.05, 5.05)
    assert await spied(h) == [("home", "leave")]


async def test_station_zone_report_after_departure_is_missed_first_train(hass):  # 10.4
    h = await setup_tcc(hass, VIA_UT, now="08:00")
    await start_tracker(h)
    await drive(h, "08:18")
    set_person(hass, 52.0, 5.0)  # in AAA's station zone, after the 08:12 left
    await drive(h, "08:20")
    assert await spied(h) == [("AAA", "enter")]
    assert any("missed" in c.data["message"].lower() and loud(c) for c in h.pushes), texts(h)


async def test_station_list_down_at_restart_recovers_station_zones(hass):  # 8, 7.11
    h = await setup_tcc(hass, VIA_UT, now="08:00")
    down = [True]

    async def stations():  # as the NS provider: positions come with the station list
        if down[0]:
            raise ProviderError("station endpoint down")
        h.provider.positions = dict(POSITIONS)
        return STATIONS

    h.provider.stations = stations
    assert await hass.config_entries.async_reload(h.entry.entry_id)
    h = await rebound(h)  # the stored config runs; the station list failed
    await start_tracker(h)
    h.provider.positions = {}
    set_person(hass, 52.0, 5.0)  # at AAA, but no station zone exists yet
    set_person(hass, *FAR)
    assert await spied(h) == []
    down[0] = False
    await drive(h, "08:02")
    await h.host.run(lambda rt: rt.check_file())  # the 10 s file watch, with the file unchanged
    set_person(hass, 52.0, 5.0)
    assert await spied(h) == [("AAA", "enter")]


async def test_starting_inside_a_zone_sends_nothing(hass):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *HOME)
    await start_tracker(h)
    assert await spied(h) == []


async def test_unavailable_or_no_coordinates_sends_nothing(hass):  # Review Focus 3
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *HOME)
    await start_tracker(h)
    set_person_state(hass, "unavailable")
    set_person_state(hass, "not_home", coords=None)
    set_person_state(hass, "unknown")
    assert await spied(h) == []


async def test_home_then_no_coordinates_sends_no_leave(hass):
    h = await setup_tcc(hass, VIA_UT)
    set_home_zone(hass)
    set_person_state(hass, "home", coords=HOME)
    await start_tracker(h)
    set_person_state(hass, "not_home", coords=None)  # a router tracker
    assert await spied(h) == []


async def test_updates_inside_one_zone_send_one_report_per_2_min(hass):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, 52.1, 5.1)
    await start_tracker(h)
    for i in range(1, 11):
        harness.FREEZER.tick(5)
        set_person(hass, 52.1 + i * 1e-5, 5.1)
    assert await spied(h) == [("UT", "report")]
    harness.FREEZER.tick(120)
    set_person(hass, 52.1 + 11e-5, 5.1)
    assert await spied(h) == [("UT", "report")] * 2


async def test_attribute_change_with_old_coordinates_sends_nothing(hass):
    h = await setup_tcc(hass, VIA_UT, now="08:00")
    set_person(hass, 52.0, 5.0)  # in AAA's zone before the train
    await start_tracker(h)
    await drive(h, "08:18")
    set_person(hass, 52.0, 5.0, battery_level=40)  # last_updated moves, the fix does not
    await drive(h, "08:20")
    assert await spied(h) == []
    assert not any("missed" in t.lower() for t in texts(h)), texts(h)


async def test_running_old_route_and_removed_profile_keep_tracking(hass):
    h = await setup_tcc(hass, VIA_UT, now="07:50")
    await start_tracker(h)
    set_person(hass, *FAR)
    await drive(h, "08:00")
    write_profiles(hass, DIRECT)  # UT is no longer a transfer, but the occurrence rides via it
    await h.host.run(lambda rt: rt.check_file())
    await h.entry.runtime_data.coordinator.async_refresh()
    set_person(hass, 52.1, 5.1)
    assert await spied(h) == [("UT", "enter")]
    write_profiles(hass, B_ONLY)  # p1 is removed but still runs
    await h.host.run(lambda rt: rt.check_file())
    await h.entry.runtime_data.coordinator.async_refresh()
    assert "p1" not in {
        p.id for p in h.entry.runtime_data.coordinator.data.profiles if p.id != "p1"
    }
    set_person(hass, *FAR)
    assert await spied(h) == [("UT", "enter"), ("UT", "leave")]


async def test_station_radius_comes_from_profile(hass):
    h = await setup_tcc(hass, RADIUS_300)
    set_person(hass, *FAR)
    await start_tracker(h)
    set_person(hass, 52.1018, 5.1)  # about 200 m from UT
    assert await spied(h) == [("UT", "enter")]


async def test_fix_less_accurate_than_the_station_radius_is_ignored(hass):  # 8, rule 6
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *FAR)
    await start_tracker(h)
    set_person(hass, 52.1, 5.1, gps_accuracy=4000)  # centred on UT, but anywhere for kilometres
    assert await spied(h) == []
    set_person(hass, 52.1, 5.1, gps_accuracy=20)  # the same point, now accurate
    assert await spied(h) == [("UT", "enter")]


async def test_zones_for_names_places_by_role_and_skips_missing_entities(hass):
    h = await setup_tcc(hass, VIA_UT)
    profile = h.entry.runtime_data.coordinator.data.profiles[0]
    assert zones_for(hass, profile, POSITIONS) == [
        z for z in zones_for(hass, profile, POSITIONS) if z.name in ("AAA", "UT")
    ]  # no zone.home yet, no work entity
    set_home_zone(hass)
    names = {z.name: z.radius_m for z in zones_for(hass, profile, POSITIONS, running=["RTD"])}
    assert names == {"AAA": 150, "UT": 150, "home": 100}  # RTD has no position


async def test_profile_without_location_is_not_tracked(hass):
    h = await setup_tcc(hass, PROFILE)
    await start_tracker(h)
    set_person(hass, *FAR)
    set_person(hass, 52.1, 5.1)
    assert await spied(h) == []


async def test_unload_with_a_location_event_in_flight_is_clean(hass, caplog):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *FAR)
    await start_tracker(h)
    set_person(hass, 52.1, 5.1)
    assert await hass.config_entries.async_unload(h.entry.entry_id)
    await hass.async_block_till_done()
    # the host's own 10 s join error under the frozen test clock is not about location
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name != HOST_LOGGER]
    assert not errors, errors
    assert not any(t.name == "tcc-runtime" for t in threading.enumerate())


async def reload_profiles(h, text):
    write_profiles(h.hass, text)
    await h.host.run(lambda rt: rt.check_file())
    await h.entry.runtime_data.coordinator.async_refresh()


@pytest.mark.parametrize("text", [TELEGRAM, OFF, OTHER], ids=["telegram", "disabled", "replaced"])
async def test_source_change_during_pending_work_drops_it(hass, text, monkeypatch):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *FAR)
    await start_tracker(h)
    waiting, release = asyncio.Event(), asyncio.Event()
    real = h.host.positions

    async def held():
        waiting.set()
        await release.wait()
        return await real()

    monkeypatch.setattr(h.host, "positions", held)
    set_person(hass, 52.1, 5.1)  # the old source enters UT while the positions read waits
    await waiting.wait()
    await reload_profiles(h, text)
    release.set()
    assert await spied(h) == []


def evidence(rt: Runtime) -> list[str]:
    return [z for s in rt.state.occurrences.values() for z in s.zone_reports]


@pytest.mark.parametrize("text", [TELEGRAM, OFF, OTHER], ids=["telegram", "disabled", "replaced"])
async def test_source_change_during_runtime_request_writes_no_evidence(hass, text, monkeypatch):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *FAR)
    await start_tracker(h)
    hit, release = threading.Event(), asyncio.Event()  # release lives on the runtime loop
    real = h.provider.trips

    async def held(q):
        if not hit.is_set():  # only the location event's request; a reload may ask too
            hit.set()
            await release.wait()
        return await real(q)

    monkeypatch.setattr(h.provider, "trips", held)
    set_person(hass, 52.1, 5.1)  # positions read, event dispatched, the core's NS request waits
    assert await hass.async_add_executor_job(hit.wait, 30)
    await reload_profiles(h, text)
    await h.host.run(lambda rt: release.set())
    assert await spied(h) == [("UT", "enter")]  # dispatched under the old source
    assert await h.host.run(evidence) == []  # and never written


@pytest.mark.parametrize("state", [None, "unavailable", "unknown", "not_home"])
async def test_replaced_tracker_starts_with_no_zone(hass, state):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *HOME)
    await start_tracker(h)  # seeds home for person.someone
    if state is not None:
        hass.states.async_set("person.other", state, {})  # unseedable: no coordinates
    await reload_profiles(h, OTHER)
    await hass.async_block_till_done()
    hass.states.async_set("person.other", "not_home", {"latitude": FAR[0], "longitude": FAR[1]})
    assert await spied(h) == []  # no leave of a home the new tracker was never seen in


async def test_mixed_case_entity_is_tracked(hass):
    h = await setup_tcc(hass, MIXED)
    set_person(hass, *FAR)
    await start_tracker(h)
    set_person(hass, 52.1, 5.1)
    assert await spied(h) == [("UT", "enter")]


_MTIME = [2_000_000_000 * 10**9]


async def switch_away_and_back(h) -> None:
    """TELEGRAM then VIA_UT again, both read by the runtime before the coordinator refreshes
    once: the tracker never sees the telegram source, only a new epoch."""
    for text in (TELEGRAM, VIA_UT):
        write_profiles(h.hass, text)
        _MTIME[0] += 10**9  # two writes in one tick are both seen by the file watch
        os.utime(profiles_path(h.hass), ns=(_MTIME[0], _MTIME[0]))
        await h.host.run(lambda rt: rt.check_file())
    await h.entry.runtime_data.coordinator.async_refresh()


def hold_trips(h, monkeypatch):
    """The first NS request waits on `release` (set on the runtime loop); `hit` marks it."""
    hit, release = threading.Event(), asyncio.Event()
    real = h.provider.trips

    async def held(q):
        if not hit.is_set():  # only the location event's request; a reload may ask too
            hit.set()
            await release.wait()
        return await real(q)

    monkeypatch.setattr(h.provider, "trips", held)
    return hit, release


async def test_source_restored_during_runtime_request_writes_no_evidence(hass, monkeypatch):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *FAR)
    await start_tracker(h)
    hit, release = hold_trips(h, monkeypatch)
    set_person(hass, 52.1, 5.1)
    assert await hass.async_add_executor_job(hit.wait, 30)
    await reload_profiles(h, TELEGRAM)
    await reload_profiles(h, VIA_UT)  # the same settings as when the event was read
    await h.host.run(lambda rt: release.set())
    assert await spied(h) == [("UT", "enter")]
    assert await h.host.run(evidence) == []


async def test_events_after_a_source_change_and_refresh_are_written(hass):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *FAR)
    await start_tracker(h)
    await reload_profiles(h, TELEGRAM)
    await reload_profiles(h, VIA_UT)
    await tick(hass, REFRESH_S + 1)
    set_person(hass, 52.5001, 5.5)  # still far: seeded by the re-tracked source
    set_person(hass, 52.1, 5.1)
    assert await spied(h) == [("UT", "enter")]
    assert "UT" in await h.host.run(evidence)  # the new epoch is read, not a cached one


async def test_event_queued_behind_the_lock_across_a_switch_back_is_dropped(hass, monkeypatch):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, *FAR)
    await start_tracker(h)
    hit, release = hold_trips(h, monkeypatch)
    set_person(hass, 52.1, 5.1)  # held in the runtime request, under the profile lock
    assert await hass.async_add_executor_job(hit.wait, 30)
    set_person(hass, 52.0, 5.0)  # queues behind the lock with the old epoch
    await switch_away_and_back(h)
    await tick(hass, REFRESH_S + 1)
    await h.host.run(lambda rt: release.set())
    await spied(h)
    assert await h.host.run(evidence) == []  # the queued event kept its old epoch


async def test_zone_radius_edit_reseeds(hass):
    h = await setup_tcc(hass, VIA_UT)
    set_person(hass, 52.1018, 5.1)  # about 200 m from UT: outside its 150 m circle
    await start_tracker(h)
    await reload_profiles(h, RADIUS_300)  # the same entity, a wider circle: a new epoch
    await hass.async_block_till_done()
    set_person(hass, 52.1019, 5.1)  # inside the new circle, no move across it
    calls = await spied(h)
    assert not [c for c in calls if c[1] in ("enter", "leave")], calls
