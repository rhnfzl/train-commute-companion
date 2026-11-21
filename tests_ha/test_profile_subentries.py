import pytest
import yaml
from ha_rig import PROFILE, profiles_path, rebound, setup_tcc
from homeassistant import config_entries, data_entry_flow
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_profile_setup import VALUES

from custom_components.train_commute_companion import profile_subentries as subentries
from custom_components.train_commute_companion.const import CONF_PROFILES_PATH, DOMAIN
from custom_components.train_commute_companion.profile_editor import EditorError
from custom_components.train_commute_companion.profile_subentries import (
    PROFILE_SUBENTRY,
    sync_profile_subentries,
)
from train_commute_companion.store import JsonFileStore, StorageError

pytestmark = pytest.mark.usefixtures("tcc_freezer")


async def test_existing_profiles_map_to_one_stable_subentry_each(hass):
    h = await setup_tcc(hass, PROFILE)
    profiles = h.entry.runtime_data.coordinator.data.profiles
    path = str(profiles_path(hass))
    sync_profile_subentries(h.entry, path, profiles)
    before = {s.unique_id: s.subentry_id for s in h.entry.subentries.values()}
    sync_profile_subentries(h.entry, path, profiles)
    assert set(before) == {"p1", "p2"}
    assert {s.unique_id: s.subentry_id for s in h.entry.subentries.values()} == before
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "p1"), h.entry.entry_id)
    assert device is not None and device.config_subentry_id == before["p1"]
    sensor = er.async_get(hass).async_get("sensor.office_a_outbound_status")
    assert sensor is not None and sensor.config_subentry_id == before["p1"]


async def test_native_subentry_add_and_edit_share_the_portable_yaml(hass):
    h = await setup_tcc(hass, PROFILE)
    manager = hass.config_entries.subentries
    handler = (h.entry.entry_id, PROFILE_SUBENTRY)
    flow = await manager.async_init(handler, context={"source": config_entries.SOURCE_USER})
    assert flow["step_id"] == "profile"
    values = {
        **VALUES,
        "name": "Sunday commute",
        "days": ["sun"],
        "home_station": "AAA",
        "work_station": "RTD",
    }
    flow = await manager.async_configure(flow["flow_id"], values)
    assert flow["step_id"] == "profile_details"
    flow = await manager.async_configure(flow["flow_id"], {"minimum_UT": 4, "pause_action": "keep"})
    if flow.get("step_id") == "profile_preview":
        flow = await manager.async_configure(flow["flow_id"], {})
    assert flow["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    subentry = next(s for s in h.entry.subentries.values() if s.title == "Sunday commute")
    assert subentry.title == "Sunday commute"
    rows = yaml.safe_load(profiles_path(hass).read_text())["profiles"]
    assert rows[-1]["id"] == subentry.unique_id
    assert rows[-1]["name"] == subentry.title

    flow = await manager.async_init(
        handler,
        context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert flow["step_id"] == "profile"
    flow = await manager.async_configure(flow["flow_id"], {**values, "name": "Weekend commute"})
    flow = await manager.async_configure(flow["flow_id"], {"minimum_UT": 4, "pause_action": "keep"})
    if flow.get("step_id") == "profile_preview":
        flow = await manager.async_configure(flow["flow_id"], {})
    assert flow.get("reason") == "reconfigure_successful", (
        flow.get("step_id"),
        flow.get("errors"),
        flow.get("description_placeholders"),
    )
    assert h.entry.subentries[subentry.subentry_id].title == "Weekend commute"
    rows = yaml.safe_load(profiles_path(hass).read_text())["profiles"]
    assert rows[-1]["id"] == subentry.unique_id
    assert rows[-1]["name"] == "Weekend commute"


async def test_native_remove_disables_monitoring_and_updates_portable_yaml(hass):
    h = await setup_tcc(hass, PROFILE)
    subentry = next(s for s in h.entry.subentries.values() if s.unique_id == "p1")
    hass.config_entries.async_remove_subentry(h.entry, subentry.subentry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert {s.unique_id for s in h.entry.subentries.values()} == {"p2"}
    assert await h.host.run(lambda rt: [p.id for p in rt.config.profiles]) == ["p2"]
    assert not await h.host.run(
        lambda rt: any(o.key.profile_id == "p1" for o in rt.state.occurrences.values())
    )
    assert [row["id"] for row in yaml.safe_load(profiles_path(hass).read_text())["profiles"]] == [
        "p2"
    ]
    assert er.async_get(hass).async_get("sensor.office_a_outbound_status") is None


async def test_remove_the_last_profile_then_add_a_replacement_after_restart(hass):
    rows = yaml.safe_load(PROFILE)["profiles"][:1]
    h = await setup_tcc(hass, yaml.safe_dump({"defaults": {"notes": "kept"}, "profiles": rows}))
    path = profiles_path(hass)
    subentry = next(iter(h.entry.subentries.values()))
    hass.config_entries.async_remove_subentry(h.entry, subentry.subentry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert yaml.safe_load(path.read_text()) == {"defaults": {"notes": "kept"}, "profiles": []}

    assert await hass.config_entries.async_reload(h.entry.entry_id)
    h = await rebound(h)
    assert not h.entry.subentries
    manager = hass.config_entries.subentries
    handler = (h.entry.entry_id, PROFILE_SUBENTRY)
    flow = await manager.async_init(handler, context={"source": config_entries.SOURCE_USER})
    assert flow["step_id"] == "profile"
    flow = await manager.async_configure(
        flow["flow_id"],
        {**VALUES, "name": "Replacement", "home_station": "AAA", "work_station": "RTD"},
    )
    assert flow["step_id"] == "profile_details"
    flow = await manager.async_configure(flow["flow_id"], {"minimum_UT": 4, "pause_action": "keep"})
    if flow.get("step_id") == "profile_preview":
        flow = await manager.async_configure(flow["flow_id"], {})
    assert flow["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    document = yaml.safe_load(path.read_text())
    assert document["defaults"] == {"notes": "kept"}
    assert [row["name"] for row in document["profiles"]] == ["Replacement"]


async def test_failed_yaml_removal_does_not_revive_profile_after_restart(hass, monkeypatch):
    h = await setup_tcc(hass, PROFILE)
    subentry = next(s for s in h.entry.subentries.values() if s.unique_id == "p1")

    def fail_write(*_):
        raise EditorError("file_changed")

    with monkeypatch.context() as patch:
        patch.setattr(subentries, "_remove_from_yaml", fail_write)
        hass.config_entries.async_remove_subentry(h.entry, subentry.subentry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert await h.host.run(lambda rt: [p.id for p in rt.config.profiles]) == ["p2"]
        assert [
            row["id"] for row in yaml.safe_load(profiles_path(hass).read_text())["profiles"]
        ] == ["p1", "p2"]
        assert await hass.config_entries.async_reload(h.entry.entry_id)
        h = await rebound(h)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert {s.unique_id for s in h.entry.subentries.values()} == {"p2"}
    host = h.entry.runtime_data.host
    assert await host.run(lambda rt: [p.id for p in rt.config.profiles]) == ["p2"]


async def test_failed_yaml_removal_applies_only_to_its_source_file(hass, monkeypatch):
    h = await setup_tcc(hass, PROFILE)
    initial = profiles_path(hass)
    alternate = initial.with_name("imported.yaml")
    alternate.write_text(PROFILE.replace("office_a", "imported_office"))
    subentry = next(s for s in h.entry.subentries.values() if s.unique_id == "p1")

    def fail_write(*_):
        raise EditorError("file_changed")

    monkeypatch.setattr(subentries, "_remove_from_yaml", fail_write)
    hass.config_entries.async_remove_subentry(h.entry, subentry.subentry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    async def switch(path):
        hass.config_entries.async_update_entry(
            h.entry, options={**h.entry.options, CONF_PROFILES_PATH: str(path)}
        )
        assert await hass.config_entries.async_reload(h.entry.entry_id)
        restarted = await rebound(h)
        await hass.async_block_till_done(wait_background_tasks=True)
        return await restarted.host.run(lambda rt: [p.id for p in rt.config.profiles])

    assert await switch(alternate) == ["p1", "p2"]
    assert "imported_office" in alternate.read_text()
    assert await switch(initial) == ["p2"]
    assert {s.unique_id for s in h.entry.subentries.values()} == {"p2"}


async def test_failed_state_removal_is_retried_before_the_yaml_changes(hass, monkeypatch):
    h = await setup_tcc(hass, PROFILE)
    path = profiles_path(hass)
    subentry = next(s for s in h.entry.subentries.values() if s.unique_id == "p1")

    def fail_write(*_):
        raise StorageError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(JsonFileStore, "save", fail_write)
        hass.config_entries.async_remove_subentry(h.entry, subentry.subentry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert [row["id"] for row in yaml.safe_load(path.read_text())["profiles"]] == ["p1", "p2"]
        h.provider.calls.clear()
        await h.host.run(lambda rt: rt.lookahead_once())
        assert h.provider.calls
        assert all(
            q.via != "UT" and "UT" not in (q.origin, q.destination) for q in h.provider.calls
        )

    assert await hass.config_entries.async_reload(h.entry.entry_id)
    restored = await rebound(h)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await restored.host.run(lambda rt: [p.id for p in rt.config.profiles]) == ["p2"]
    assert [row["id"] for row in yaml.safe_load(path.read_text())["profiles"]] == ["p2"]


async def test_valid_yaml_removal_can_be_imported_again(hass):
    h = await setup_tcc(hass, PROFILE, now="05:00")
    path = profiles_path(hass)
    rows = yaml.safe_load(path.read_text())["profiles"]
    path.write_text(yaml.safe_dump({"profiles": rows[1:]}))
    assert await h.host.run(lambda rt: rt.reload()) is None
    await h.entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert {s.unique_id for s in h.entry.subentries.values()} == {"p2"}

    path.write_text(yaml.safe_dump({"profiles": rows}))
    assert await h.host.run(lambda rt: rt.reload()) is None
    await h.entry.runtime_data.coordinator.async_refresh()
    assert {s.unique_id for s in h.entry.subentries.values()} == {"p1", "p2"}


async def test_native_remove_stops_a_profile_retained_after_yaml_edit(hass):
    h = await setup_tcc(hass, PROFILE)
    path = profiles_path(hass)
    subentry = next(s for s in h.entry.subentries.values() if s.unique_id == "p1")
    rows = yaml.safe_load(path.read_text())["profiles"]
    path.write_text(yaml.safe_dump({"profiles": rows[1:]}))
    assert await h.host.run(lambda rt: rt.reload()) is None
    await h.entry.runtime_data.coordinator.async_refresh()
    assert await h.host.run(
        lambda rt: any(o.key.profile_id == "p1" for o in rt.state.occurrences.values())
    )
    hass.config_entries.async_remove_subentry(h.entry, subentry.subentry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not await h.host.run(
        lambda rt: any(o.key.profile_id == "p1" for o in rt.state.occurrences.values())
    )


async def test_migration_keeps_custom_entity_id_and_disabled_state(hass):
    h = await setup_tcc(hass, PROFILE, start=False)
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=h.entry.entry_id, identifiers={(DOMAIN, "p1")}, name="Office A"
    )
    status = registry.async_get_or_create(
        "sensor", DOMAIN, "p1_outbound_status", config_entry=h.entry, device_id=device.id
    )
    registry.async_update_entity(
        status.entity_id,
        new_entity_id="sensor.my_departure",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await rebound(h)
    subentry = next(s for s in h.entry.subentries.values() if s.unique_id == "p1")
    migrated = registry.async_get("sensor.my_departure")
    assert migrated is not None and migrated.config_subentry_id == subentry.subentry_id
    assert migrated.disabled_by is er.RegistryEntryDisabler.USER
    assert migrated.device_id == device.id
