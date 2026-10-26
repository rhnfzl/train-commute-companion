import json
from dataclasses import replace
from datetime import date, datetime

import pytest
from builders import AMS, make_leg
from harness import (
    PROFILE,
    PROFILES,
    drive,
    jump,
    only_occurrence,
    rebound,
    setup_tcc,
    write_profiles,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_lifecycle_follow import journey, occ
from test_runtime import at

from custom_components.train_commute_companion.const import DOMAIN
from custom_components.train_commute_companion.entity import health_state, pick
from train_commute_companion.health import BoardHealth
from train_commute_companion.lifecycle import DirectionState, LegState, NotTravelling
from train_commute_companion.model import Direction, OccurrenceKey
from train_commute_companion.view import DirectionInfo, OccurrenceView, RuntimeView

pytestmark = pytest.mark.usefixtures("tcc_freezer")


def profile(name: str) -> str:
    return (PROFILES / name).read_text()


EDITED = profile("office_a_edited.yaml")
DIRECT = profile("office_a_direct.yaml")
RENAMED = profile("office_a_renamed.yaml")
B_ONLY = profile("office_b_only.yaml")


def registry_ids(hass) -> dict[str, str]:
    reg = er.async_get(hass)
    return {e.entity_id: e.unique_id for e in reg.entities.values() if e.platform == DOMAIN}


def device(hass, h, pid):
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, pid), h.entry.entry_id)


def state(hass, eid):
    return hass.states.get(eid).state


def attr(hass, eid, name):
    return hass.states.get(eid).attributes[name]


async def reload_file(h) -> None:
    await h.host.run(lambda rt: rt.check_file())
    await h.entry.runtime_data.coordinator.async_refresh()
    await h.hass.async_block_till_done()


async def test_entity_ids_follow_profile_name(hass):
    await setup_tcc(hass, PROFILE)
    for eid in (
        "sensor.office_a_outbound_status",
        "sensor.office_a_outbound_leg_3",
        "sensor.office_a_outbound_transfer_2",
        "sensor.office_a_return_works_ahead",
        "sensor.office_a_health",
    ):
        assert hass.states.get(eid) is not None, eid
    assert hass.states.get("sensor.office_a_outbound_leg_4") is None
    assert hass.states.get("sensor.office_a_return_leg_3") is None
    ids = registry_ids(hass)
    assert ids["sensor.office_a_outbound_status"] == "p1_outbound_status"
    assert ids["sensor.office_a_outbound_leg_2"] == "p1_outbound_leg_2"
    assert ids["sensor.office_a_return_works_ahead"] == "p1_return_works_ahead"
    assert ids["sensor.office_a_health"] == "p1_health"


async def test_leg_and_transfer_states_after_poll(hass):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:00")
    leg2 = "sensor.office_a_outbound_leg_2"
    assert state(hass, leg2) == "delayed" and attr(hass, leg2, "departure_delay_min") == 5
    assert state(hass, "sensor.office_a_outbound_leg_3") == "none"
    assert state(hass, "sensor.office_a_outbound_transfer_1") == "ok"
    assert state(hass, "sensor.office_a_outbound_transfer_2") == "none"
    assert attr(hass, "sensor.office_a_outbound_status", "attribution") == "Data: NS"
    assert state(hass, "sensor.office_a_outbound_lock") != "none"
    assert attr(hass, "sensor.office_a_outbound_lock", "trains")[0] == "08:12 AAA-UT 1234"
    assert state(hass, "sensor.office_a_health") == "ok"
    assert state(hass, "sensor.office_a_outbound_next_connection") != "unknown"


async def test_edit_name_stations_times_and_add_transfer_keeps_ids(hass):
    h = await setup_tcc(hass, PROFILE)
    ids = registry_ids(hass)
    write_profiles(hass, EDITED)
    await reload_file(h)
    after = registry_ids(hass)
    assert all(after[e] == u for e, u in ids.items())
    # added at once; a new entity is named after the device as it is now (HA), old ones keep theirs
    reg = er.async_get(hass)
    for uid in ("p1_outbound_leg_4", "p1_outbound_transfer_3"):
        eid = reg.async_get_entity_id("sensor", DOMAIN, uid)
        assert eid is not None and hass.states.get(eid) is not None, uid
    assert hass.states.get("sensor.office_a_outbound_status") is not None


async def test_removed_transfer_entities_stay_while_old_revision_runs(hass):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:00")
    write_profiles(hass, DIRECT)
    await reload_file(h)
    reg = er.async_get(hass)
    for eid in ("sensor.office_a_outbound_leg_3", "sensor.office_a_outbound_transfer_2"):
        assert reg.async_get(eid) is not None, eid
    await h.host.run(lambda rt: rt.act("p1", NotTravelling(None, False)))
    await jump(h, "2026-10-27 20:00")
    for _ in range(3):
        await h.host.run(lambda rt: rt.poll_once())
        await h.host.run(lambda rt: rt.watchdog_once())
        await h.entry.runtime_data.coordinator.async_refresh()
    await h.hass.async_block_till_done()
    for eid in ("sensor.office_a_outbound_leg_3", "sensor.office_a_outbound_transfer_2"):
        assert reg.async_get(eid) is None, eid
    assert reg.async_get("sensor.office_a_outbound_leg_2") is not None


async def test_removed_profile_removes_its_device(hass):
    h = await setup_tcc(hass, PROFILE)
    assert device(hass, h, "p1") is not None
    write_profiles(hass, B_ONLY)
    await h.host.run(lambda rt: rt.act("p1", NotTravelling(None, False)))
    await reload_file(h)
    await jump(h, "2026-10-27 20:00")
    for _ in range(3):
        await h.host.run(lambda rt: rt.watchdog_once())
        await h.entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert device(hass, h, "p1") is None
    assert not [s for s in hass.states.async_all() if s.entity_id.startswith("sensor.office_a_")]
    assert device(hass, h, "p2") is not None


async def test_readded_profile_gets_its_sensors_and_buttons_back(hass):  # 5.4
    h = await setup_tcc(hass, PROFILE)
    write_profiles(hass, B_ONLY)
    await h.host.run(lambda rt: rt.act("p1", NotTravelling(None, False)))
    await reload_file(h)
    await jump(h, "2026-10-27 20:00")
    for _ in range(3):
        await h.host.run(lambda rt: rt.watchdog_once())
        await h.entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert device(hass, h, "p1") is None
    write_profiles(hass, PROFILE)
    await reload_file(h)
    reg = er.async_get(hass)
    for domain, uid in (("sensor", "p1_outbound_status"), ("button", "p1_outbound_leaving")):
        eid = reg.async_get_entity_id(domain, DOMAIN, uid)
        assert eid is not None and hass.states.get(eid) is not None, uid
    assert device(hass, h, "p1") is not None


async def test_reload_keeps_unique_ids_and_user_customisation(hass):
    h = await setup_tcc(hass, PROFILE)
    reg = er.async_get(hass)
    leg3, status = "sensor.office_a_outbound_leg_3", "sensor.office_a_outbound_status"
    reg.async_update_entity(leg3, disabled_by=er.RegistryEntryDisabler.USER)
    reg.async_update_entity(status, name="My status")
    await hass.async_block_till_done()
    ids = registry_ids(hass)
    assert await hass.config_entries.async_reload(h.entry.entry_id)
    h = await rebound(h)
    assert registry_ids(hass) == ids
    assert reg.async_get(leg3).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(leg3) is None
    assert reg.async_get(status).name == "My status"
    assert ids[status] == "p1_outbound_status" and ids[leg3] == "p1_outbound_leg_3"


async def test_first_view_without_config_removes_nothing(hass):
    h = await setup_tcc(hass, PROFILE)
    ids = registry_ids(hass)
    assert await hass.config_entries.async_reload(h.entry.entry_id)
    coordinator = h.entry.runtime_data.coordinator
    assert not coordinator.data.started  # the first view of the new runtime knows no config
    assert registry_ids(hass) == ids
    assert device(hass, h, "p1") is not None


async def test_rename_while_riding_keeps_lock_and_ids(hass):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:20")

    def lock(rt):
        s = only_occurrence(rt).lock
        return s and [x.train for x in s.legs]

    before = await h.host.run(lock)
    assert before and state(hass, "sensor.office_a_outbound_status") == "riding"
    ids = registry_ids(hass)
    write_profiles(hass, RENAMED)
    await reload_file(h)
    assert await h.host.run(lock) == before
    assert registry_ids(hass) == ids
    assert state(hass, "sensor.office_a_outbound_status") == "riding"


async def test_two_occurrences_of_one_direction_show_oldest_and_list_others(hass):
    h = await setup_tcc(hass, PROFILE)
    mon = date(2026, 10, 26)
    night = make_leg("AAA", "RTD", "23:50", "03:40", number="9999", day=mon)

    def seed(rt):
        ride = replace(journey([night], (LegState.RIDING,)), planned_final_arrival=at("08:40"))
        old = replace(
            occ(ride),
            key=replace(occ(None).key, service_date=mon),
            route_revision=rt.config.revision,
            settings_revision=rt.config.revision,
        )
        rt._st = replace(
            rt.state, occurrences={**rt.state.occurrences, "p1|outbound|2026-10-26": old}
        )

    await h.host.run(seed)
    await h.entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    eid = "sensor.office_a_outbound_status"
    assert state(hass, eid) == "riding" and attr(hass, eid, "service_date") == "2026-10-26"
    others = attr(hass, eid, "others")
    assert [o["service_date"] for o in others] == ["2026-10-27"]
    assert state(hass, "sensor.office_a_outbound_leg_1") != "none"


async def test_attributes_are_json_primitives(hass):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:20")

    def retain(rt):
        s = only_occurrence(rt)
        kept = journey([make_leg("AAA", "UT", "08:12", "08:40", number="1234")], (LegState.DONE,))
        rt._st = replace(
            rt.state,
            occurrences={
                **rt.state.occurrences,
                s.key.profile_id + "|outbound|2026-10-27": replace(s, retained=(kept,)),
            },
        )

    await h.host.run(retain)
    await h.entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    tcc = [s for s in hass.states.async_all() if s.entity_id.startswith("sensor.office_a_")]
    assert len(tcc) > 10
    for s in tcc:
        json.dumps(dict(s.attributes))  # a dataclass or datetime would raise
    retained = attr(hass, "sensor.office_a_outbound_status", "retained")
    assert retained == [["08:12 AAA-UT 1234"]]


# --- pure ---

HEALTH = BoardHealth(True, None, 0, False)


def ov(day: int, *, resolved: bool = False, retained=(), health=HEALTH, pid="p1") -> OccurrenceView:
    return OccurrenceView(
        OccurrenceKey(pid, Direction.OUTBOUND, date(2026, 10, day)), "n",
        DirectionState.DONE if resolved else DirectionState.LOCKED, resolved, False, False,
        None, None, (), (), retained, None, None, health,
    )  # fmt: skip


def rv(*occs: OccurrenceView, **kw) -> RuntimeView:
    base = {
        "at": datetime(2026, 10, 27, 8, tzinfo=AMS), "started": True, "profiles": (),
        "directions": (DirectionInfo("p1", "n", Direction.OUTBOUND, 0),), "occurrences": occs,
        "works": {}, "moved": (), "refusal": None, "store_ok": True, "key_rejected": False,
        "undelivered": 0, "requests": 0,
    }  # fmt: skip
    return RuntimeView(**{**base, **kw})


def test_pick_oldest_unresolved_then_newest():
    a, b, c = ov(25, resolved=True), ov(26), ov(27)
    assert pick(rv(c, b, a), "p1", Direction.OUTBOUND) == (b, (a, c))
    assert pick(rv(ov(25, resolved=True), ov(26, resolved=True)), "p1", Direction.OUTBOUND)[
        0
    ].key.service_date == date(2026, 10, 26)
    # a DONE occurrence that still runs a retained remainder is not resolved: picked first
    kept = ov(25, retained=((),))
    assert pick(rv(ov(27), kept), "p1", Direction.OUTBOUND)[0] is kept
    assert pick(rv(), "p1", Direction.OUTBOUND) == (None, ())
    assert pick(rv(ov(27, pid="p2")), "p1", Direction.OUTBOUND) == (None, ())
    assert pick(rv(ov(27)), "p1", Direction.RETURN) == (None, ())


def test_health_state_order():
    live = ov(27)
    assert health_state(rv(), "p1") == "idle"
    assert health_state(rv(ov(27, resolved=True)), "p1") == "idle"
    assert health_state(rv(live), "p1") == "ok"
    one = ov(27, health=BoardHealth(True, None, 1, False))  # fresh stays true through one failure
    assert health_state(rv(one), "p1") == "ok"
    two = ov(27, health=BoardHealth(True, None, 2, False))
    assert health_state(rv(two), "p1") == "cannot_see"
    stale = ov(27, health=BoardHealth(False, None, 0, False))
    assert health_state(rv(stale), "p1") == "cannot_see"
    assert health_state(rv(stale, undelivered=1), "p1") == "notify_failing"
    assert health_state(rv(stale, undelivered=1, store_ok=False), "p1") == "storage_failed"
    assert (
        health_state(rv(stale, undelivered=1, store_ok=False, key_rejected=True), "p1")
        == "key_rejected"
    )
    assert health_state(rv(stale), "p2") == "idle"
