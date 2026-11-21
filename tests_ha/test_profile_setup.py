import asyncio
from dataclasses import replace
from datetime import date, datetime
from itertools import permutations
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import voluptuous as vol
import yaml
from builders import make_leg
from ha_rig import NOTIFY, PROFILE, fake_phone, profiles_path, write_profiles
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from runtime_rig import Rig

from custom_components.train_commute_companion import validate
from custom_components.train_commute_companion.const import DOMAIN
from custom_components.train_commute_companion.host import Parts, RuntimeHost
from custom_components.train_commute_companion.profile_flow import ProfileEditorFlow
from train_commute_companion.calendar import TZ
from train_commute_companion.model import Connection, Provenance, Stations
from train_commute_companion.providers.base import ProviderAnswer, ProviderError, Query, RateLimited

CATALOGUE = Stations(
    {"AAA": ["Home station"], "UT": ["Utrecht Centraal", "Utrecht"], "RTD": ["Rotterdam Centraal"]}
)
CHECK = "custom_components.train_commute_companion.config_flow.check_key"
CATALOGUE_CALL = "custom_components.train_commute_companion.profile_flow.station_catalogue"
TRIPS = "train_commute_companion.providers.ns.NsProvider.trips"
VALUES = {
    "name": "commute",
    "days": ["mon", "tue"],
    "home_station": "Home station",  # the single pickers send names, mapped to codes
    "work_station": "Rotterdam Centraal",
    "outbound_time": "08:12",
    "return_time": "17:52",
    "outbound_transfers": ["UT"],
    "return_transfers": [],
    "outbound_buffer": 10,
    "return_buffer": 5,
    "skip_public_holidays": True,
    "require_accessible": False,
    "disabled_categories": [],
}


@pytest.fixture(autouse=True)
def editor_config_directory(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


@pytest.fixture(autouse=True)
def no_ns_trip_requests():
    with patch(TRIPS, side_effect=ProviderError):
        yield


async def configure(hass, flow, values):
    return await hass.config_entries.flow.async_configure(flow["flow_id"], values)


async def details(hass, flow, **values):
    assert flow["step_id"] == "profile_details"
    return await configure(hass, flow, {"minimum_UT": 7, "pause_action": "keep", **values})


async def guided(hass, catalogue=CATALOGUE):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with (
        patch(CHECK, return_value=frozenset(catalogue)),
        patch(CATALOGUE_CALL, return_value=catalogue),
    ):
        return await configure(hass, flow, {"api_key": "k"})


async def test_guided_profile_waits_for_phone_confirmation_before_writing(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await guided(hass)
    assert flow["step_id"] == "profile"
    selector = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "home_station")
    # the value is the name: the closed picker shows its raw value
    assert selector.config["options"] == ["Home station", "Rotterdam Centraal", "Utrecht Centraal"]
    assert selector.config["custom_value"] is True  # what gives the picker its search box
    flow = await configure(
        hass, flow, {**VALUES, "require_accessible": True, "disabled_categories": ["crowding"]}
    )
    assert shown(flow)["custom_timetable_horizon"] is True
    assert shown(flow)["timetable_horizon_unit"] == "calendar_days"
    assert shown(flow)["timetable_horizon_count"] == 7
    assert shown(flow)["daily_brief"] is True
    assert shown(flow)["brief_at"] == shown(flow)["brief_evening_at"] == ""
    assert shown(flow)["brief_only_if_something"] is False
    flow = await details(
        hass, flow, pause_action="add", pause_start="2027-04-01", pause_end="2027-04-07"
    )
    assert flow["step_id"] == "notify" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    assert flow["step_id"] == "test" and not profiles_path(hass).exists()
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert flow["description"] == "guided"  # says where to add more commutes later
    raw = yaml.safe_load(profiles_path(hass).read_text())
    assert raw["profiles"][0]["name"] == "commute"
    assert raw["profiles"][0]["home_station"] == "AAA"
    assert raw["profiles"][0]["work_station"] == "RTD"
    assert raw["profiles"][0]["accessibility"] == {"require_accessible": True}
    assert raw["profiles"][0]["alerts"]["disabled_categories"] == ["crowding"]
    assert raw["profiles"][0]["transfer_min"]["UT"] == 7
    assert raw["profiles"][0]["pauses"] == [{"start": "2027-04-01", "end": "2027-04-07"}]


@pytest.mark.parametrize("field", ["repeat_monthly", "repeat_every_weeks", "repeat_end_after"])
async def test_recurrence_numbers_reject_booleans_in_the_form(hass, field):
    flow = await guided(hass)
    schema = flow["data_schema"]
    values = {**VALUES, "days": ["mon"], "repeat_start_date": "2026-11-01"}
    assert schema({**values, field: 1.0})[field] == 1
    with pytest.raises(vol.Invalid):
        schema({**values, field: True})


async def test_recurrence_form_accepts_monthly_rule_without_start(hass):
    flow = await guided(hass)
    flow = await configure(hass, flow, {**VALUES, "days": ["mon"], "repeat_monthly": 1.0})
    assert flow["step_id"] == "profile_details"


async def test_guided_preview_uses_dated_override_after_holiday_and_pause(hass, monkeypatch):
    fake_phone(hass, monkeypatch)

    class PreviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 24, 18, tzinfo=TZ).astimezone(tz)

    monkeypatch.setattr(
        "custom_components.train_commute_companion.profile_flow.datetime", PreviewClock
    )
    hass.states.async_set("zone.home", "0", {"latitude": 52.01123, "longitude": 5.06123})
    catalogue = Stations(
        {**CATALOGUE.labels, "BBB": ["Zuid"]},
        {"AAA": (52.05, 5.0), "RTD": (51.9, 4.4), "UT": (52.09, 5.1), "BBB": (52.01, 5.06)},
    )
    initial = await guided(hass, catalogue)
    selector = next(v for k, v in initial["data_schema"].schema.items() if str(k) == "home_station")
    assert selector.config["options"] == [
        "Zuid",
        "Home station",
        "Utrecht Centraal",
        "Rotterdam Centraal",
    ]
    latest = await configure(hass, initial, {**VALUES, "days": ["fri", "mon", "tue"]})
    latest = await details(
        hass,
        latest,
        edit_weekday_times=True,
        pause_action="add",
        pause_start="2026-12-28",
        pause_end="2026-12-28",
    )

    async def answer(query):
        assert (query.origin, query.destination, query.via) == ("AAA", "RTD", "UT")
        assert query.at == datetime(2026, 12, 29, 9, 5, tzinfo=TZ)
        first = make_leg("AAA", "UT", "09:05", "09:25", day=date(2026, 12, 29))
        first = replace(first, stops=(replace(first.board, planned_track="7a"), first.alight))
        second = make_leg("UT", "RTD", "09:35", "10:01", day=date(2026, 12, 29), number="2345")
        return ProviderAnswer(query, (Connection((first, second), "", None),), PreviewClock.now(TZ))

    with patch(TRIPS, side_effect=answer) as trips:
        latest = await configure(hass, latest, {"outbound_tue": "09:05"})
    assert trips.await_count == 1
    assert latest["step_id"] == "profile_preview"
    assert latest["description_placeholders"] == {
        "departure": "2026-12-29 09:05",
        "platform": "7a",
        "route": "Home station → Utrecht Centraal → Rotterdam Centraal",
    }
    assert not profiles_path(hass).exists()
    latest = await configure(hass, latest, {})
    assert latest["step_id"] == "notify" and not profiles_path(hass).exists()


def mobile_activity(hass, name):
    mobile = MockConfigEntry(domain="mobile_app", data={CONF_WEBHOOK_ID: f"hook-{name}"})
    mobile.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mobile.entry_id, identifiers={("mobile_app", name)}
    )
    registry = er.async_get(hass)
    tracker = registry.async_get_or_create(
        "device_tracker",
        "mobile_app",
        name,
        config_entry=mobile,
        device_id=device.id,
        suggested_object_id=name,
    )
    activity = registry.async_get_or_create(
        "sensor",
        "mobile_app",
        f"hook-{name}_detected_activity",
        config_entry=mobile,
        device_id=device.id,
        suggested_object_id=f"{name}_renamed",
    )
    return tracker.entity_id, activity.entity_id


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_location_suggests_only_the_selected_persons_unique_mobile_device(
    hass, monkeypatch, ambiguous
):
    fake_phone(hass, monkeypatch)
    tracker, activity = mobile_activity(hass, "alice_phone")
    other_tracker, _ = mobile_activity(hass, "bob_phone")
    hass.states.async_set(
        "person.alice",
        "home",
        {
            "device_trackers": [tracker, other_tracker] if ambiguous else [tracker],
            "source": tracker,
        },
    )
    hass.states.async_set("person.bob", "home", {"device_trackers": [other_tracker]})
    hass.states.async_set("zone.office", "0")
    hass.states.async_set("device_tracker.office_wifi", "home", {"source_type": "router"})
    flow = await configure(hass, await guided(hass), VALUES)
    flow = await details(hass, flow, ha_person="person.alice")
    assert flow["step_id"] == "profile_location"
    assert shown(flow).get("ha_activity") == (None if ambiguous else activity)
    flow = await configure(
        hass,
        flow,
        {
            "ha_work_zone": "zone.office",
            "ha_work_wifi": "device_tracker.office_wifi",
        },
    )
    assert flow["step_id"] == "notify"
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]
    assert saved["location"] == {
        "source": "ha",
        "entity": "person.alice",
        "zones": {"work": "zone.office"},
        "wifi": {"work": "device_tracker.office_wifi"},
    }


async def test_options_keeps_non_ha_location_until_person_is_selected(hass):
    tracker, activity = mobile_activity(hass, "alice_phone")
    hass.states.async_set("person.alice", "home", {"device_trackers": [tracker]})
    raw = yaml.safe_load(PROFILE)
    old = {
        "source": "owntracks",
        "zones": {"home": "Home", "other": "Other"},
        "other_station_dwell_min": 7,
    }
    raw["profiles"][0]["location"] = old
    raw["profiles"][0]["alerts"] = {"quiet_hours": ["21:00", "06:00"]}
    raw["profiles"][0]["works_digest"] = {"day": "fri", "time": "18:00"}
    write_profiles(hass, yaml.safe_dump(raw))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)

    async def step(flow, values):
        return await hass.config_entries.options.async_configure(flow["flow_id"], values)

    async def edit():
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        with patch(CATALOGUE_CALL, return_value=CATALOGUE):
            flow = await step(flow, {"next_step_id": "profiles"})
        flow = await step(flow, {"profile": "edit:p1"})
        return await step(flow, shown(flow))

    def saved():
        return yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]

    flow = await edit()
    defaults = shown(flow)
    assert (defaults["quiet_start"], defaults["quiet_end"]) == ("21:00", "06:00")
    assert (defaults["works_digest_day"], defaults["works_digest_time"]) == ("fri", "18:00")
    # HA omits optional text fields the user clears, even when the form suggested a value.
    for key in ("quiet_start", "quiet_end", "works_digest_time"):
        defaults.pop(key)
    result = await step(flow, {**defaults, "works_digest_day": "off", "disable_ha_location": True})
    assert result["reason"] == "profile_saved"

    assert saved()["location"] == old
    assert "quiet_hours" not in saved()["alerts"] and "works_digest" not in saved()

    flow = await edit()
    flow = await step(flow, {**shown(flow), "ha_person": "person.alice"})
    assert shown(flow)["ha_activity"] == activity
    result = await step(flow, {})
    assert result["reason"] == "profile_saved"
    assert saved()["id"] == "p1"
    assert saved()["location"] == {
        "source": "ha",
        "entity": "person.alice",
        "zones": {"other": "Other"},
        "other_station_dwell_min": 7,
    }

    flow = await edit()
    flow = await step(flow, {**shown(flow), "ha_person": "person.alice"})
    assert "ha_activity" not in shown(flow)  # clearing a suggestion sticks on future edits
    result = await step(flow, {})
    assert result["reason"] == "profile_saved"


@pytest.mark.parametrize("runtime_state", ["ready", "cooldown", "unloading"])
async def test_options_preview_shares_the_runtime_request_gate(hass, tmp_path, runtime_state):
    write_profiles(hass, PROFILE)
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.rt.known_stations = CATALOGUE
    if runtime_state == "cooldown":
        rig.provider.error = lambda _: RateLimited()
        query = Query("AAA", "RTD", rig.rt.now())
        assert await rig.rt._gate.trips(query, False, None) == "rate"
        rig.provider.error = lambda _: None
    before = rig.rt.requests
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)

    async def run(fn):
        return await fn(rig.rt)

    entry.runtime_data = SimpleNamespace(host=SimpleNamespace(run=run))
    flow = await hass.config_entries.options.async_init(entry.entry_id)

    async def step(values):
        return await hass.config_entries.options.async_configure(flow["flow_id"], values)

    with patch(CATALOGUE_CALL, return_value=CATALOGUE) as direct_catalogue:
        flow = await step({"next_step_id": "profiles"})
    assert direct_catalogue.await_count == 0
    flow = await step({"profile": "edit:p1"})
    flow = await step(shown(flow))
    with patch(TRIPS, side_effect=ProviderError("wrong provider")) as direct:
        if runtime_state == "unloading":
            entered = Event()

            async def preview(_):
                entered.set()
                await asyncio.Future()

            async def idle():
                await asyncio.Future()

            async def close():
                pass

            runtime = SimpleNamespace(run_forever=idle, preview_trip=preview)
            host = RuntimeHost(lambda: Parts(runtime, rig.clock, close), retired=Event())
            await host.start()
            entry.runtime_data.host = host
            pending = asyncio.create_task(step(shown(flow)))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                await host.stop()
                result = await pending
            finally:
                await host.stop()
        else:
            result = await step(shown(flow))
    assert result["reason"] == "profile_saved"
    assert direct.await_count == 0
    assert rig.rt.requests == before + int(runtime_state == "ready")


async def test_initial_setup_preview_reuses_its_cooldown(hass):
    flow = ProfileEditorFlow()
    flow.hass = hass
    flow._editor_key = "test"
    query = Query("AAA", "RTD", datetime.now(TZ))
    with patch(TRIPS, side_effect=RateLimited()) as provider:
        assert await flow._preview_trips(query) == "rate"
        assert await flow._preview_trips(query) == "rate"
    assert provider.await_count == flow._preview_gate.requests == 1


@pytest.mark.parametrize(
    "case",
    [
        "planned_window",
        "empty",
        "off_date",
        "stale",
        "timeout",
        "cancelled",
        "unreachable",
        "late_arrival",
        "meets_arrival",
        "recurring",
    ],
)
async def test_preview_uses_only_current_dated_timetable_evidence(hass, monkeypatch, case):
    fake_phone(hass, monkeypatch)

    class PreviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 20, 18, tzinfo=TZ).astimezone(tz)

    monkeypatch.setattr(
        "custom_components.train_commute_companion.profile_flow.datetime", PreviewClock
    )
    if case == "timeout":
        monkeypatch.setattr(
            "custom_components.train_commute_companion.profile_preview.PREVIEW_TIMEOUT_S", 0
        )
    flow = await guided(hass)
    values = {**VALUES, "days": ["mon"], "outbound_transfers": []}
    day = 30 if case == "recurring" else 23
    if case == "recurring":
        values |= {"repeat_start_date": "2026-11-16", "repeat_every_weeks": 2}
    is_window = case in ("planned_window", "late_arrival", "meets_arrival")
    if is_window:
        values |= {
            "outbound_mode": "window",
            "outbound_time": "",
            "outbound_window_start": "08:00",
            "outbound_window_end": "09:00",
        }
    if case in ("late_arrival", "meets_arrival"):
        values |= {
            "outbound_pick": "arrive_by",
            "outbound_arrive_by": "09:00",
            "outbound_walk_after_min": 10,
        }
    flow = await configure(hass, flow, values)

    async def answer(query):
        assert (query.origin, query.destination, query.via) == ("AAA", "RTD", None)
        assert query.at == datetime(2026, 11, day, 8, 0 if is_window else 12, tzinfo=TZ)
        if case == "timeout":
            await asyncio.Future()
        leg = make_leg(
            "AAA",
            "RTD",
            "08:12",
            "08:55" if case == "late_arrival" else "08:50" if case == "meets_arrival" else "09:12",
            day=date(2026, 11, 24 if case == "off_date" else day),
            provenance=Provenance.PLANNED,
        )
        fetched = datetime(2026, 11, 19, tzinfo=TZ) if case == "stale" else PreviewClock.now(TZ)
        if case == "unreachable":
            leg = replace(leg, reachable=False)
        status = "CANCELLED" if case == "cancelled" else "NORMAL"
        connections = () if case == "empty" else (Connection((leg,), status, None),)
        return ProviderAnswer(query, connections, fetched)

    with patch(TRIPS, side_effect=answer) as trips:
        flow = await configure(hass, flow, {"pause_action": "keep"})
    assert trips.await_count == 1
    assert not profiles_path(hass).exists()
    if case in ("planned_window", "meets_arrival", "recurring"):
        assert flow["step_id"] == "profile_preview"
        assert flow["description_placeholders"] == {
            "departure": f"2026-11-{day} 08:12",
            "platform": "?",
            "route": "Home station → Rotterdam Centraal",
        }
    else:
        assert flow["step_id"] == "notify"


async def test_guided_setup_never_writes_over_an_existing_file(hass):
    write_profiles(hass, PROFILE)
    flow = await guided(hass)
    assert flow["step_id"] == "profiles_file"
    assert profiles_path(hass).read_text() == PROFILE


def station_keys(flow) -> dict:
    return {str(k): k for k in flow["data_schema"].schema if str(k) in STATION_FIELDS}


STATION_FIELDS = ("home_station", "work_station")


async def test_a_new_profile_starts_with_empty_stations(hass):
    """Required pick lists get their first item from HA; optional ones with no value stay
    empty, and the step itself asks for both stations."""
    flow = await guided(hass)
    keys = station_keys(flow)
    assert set(keys) == set(STATION_FIELDS)
    for key in keys.values():
        assert isinstance(key, vol.Optional) and key.default is vol.UNDEFINED
        assert not (key.description or {}).get("suggested_value")
    empty = {k: v for k, v in VALUES.items() if k not in STATION_FIELDS}
    flow = await configure(hass, flow, empty)
    assert flow["step_id"] == "profile"
    assert flow["errors"] == dict.fromkeys(STATION_FIELDS, "unknown_station")
    assert set(station_keys(flow)) == set(STATION_FIELDS)  # still there to be filled
    assert not profiles_path(hass).exists()


async def test_guided_arrival_choice_requires_an_explicit_target(hass):
    flow = await guided(hass)
    fields = {str(k): k for k in flow["data_schema"].schema}
    assert fields["outbound_arrive_by"].default is vol.UNDEFINED
    assert not fields["outbound_arrive_by"].description.get("suggested_value")
    values = {
        **VALUES,
        "outbound_mode": "window",
        "outbound_pick": "arrive_by",
        "outbound_window_start": "07:00",
        "outbound_window_end": "08:45",
        "outbound_walk_after_min": 10,
        "outbound_prefer_direct_min": 5,
    }
    flow = await configure(hass, flow, values)
    assert flow["step_id"] == "profile"
    assert flow["errors"]["base"] == "invalid_profile"
    flow = await configure(hass, flow, {**values, "outbound_arrive_by": "09:00"})
    assert flow["step_id"] == "profile_details"
    switch = next(k for k in flow["data_schema"].schema if str(k) == "arrive_by")
    assert switch.default() is True


async def test_check_in_fields_are_optional_without_a_forced_default(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await guided(hass)
    for direction in ("outbound", "return"):
        key = next(k for k in flow["data_schema"].schema if str(k) == f"{direction}_active_lead")
        assert isinstance(key, vol.Optional) and key.default is vol.UNDEFINED
        assert not (key.description or {}).get("suggested_value")
    flow = await configure(
        hass, flow, {**VALUES, "outbound_active_lead": 40, "return_active_lead": 25}
    )
    assert flow["step_id"] == "profile_details"
    switch = next(k for k in flow["data_schema"].schema if str(k) == "leave_by")
    assert switch.default() is True
    ntfy = next(k for k in flow["data_schema"].schema if str(k) == "ntfy_enabled")
    assert ntfy.default() is True
    assert shown(flow)["alert_history"] is False
    switch = next(k for k in flow["data_schema"].schema if str(k) == "weather_allowance")
    assert switch.default() is True
    flow = await details(hass, flow, leave_by=False, weather_allowance=False)
    assert flow["step_id"] == "notify"
    assert not profiles_path(hass).exists()


@pytest.mark.parametrize("work", ["Home station", "home station", "AAA"])
async def test_home_and_work_must_differ(hass, work):
    flow = await configure(hass, await guided(hass), {**VALUES, "work_station": work})
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {"work_station": "same_station"}
    assert not profiles_path(hass).exists()


async def test_an_older_same_station_profile_opens_and_can_be_fixed(hass):
    """Older versions saved home == work. The file still loads (a warning only), so the editor
    opens it: a healthy profile saves, and the old one is refused until its stations differ."""
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][1]["work_station"] = "AAA"  # office_b, on sat: no day clash with office_a
    write_profiles(hass, yaml.safe_dump(raw, sort_keys=False))
    assert validate.file_problem(str(profiles_path(hass))) == ""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)

    async def edit(profile_id):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        with patch(CATALOGUE_CALL, return_value=CATALOGUE):
            flow = await hass.config_entries.options.async_configure(
                flow["flow_id"], {"next_step_id": "profiles"}
            )
        assert flow["step_id"] == "profiles", flow.get("errors")
        return await hass.config_entries.options.async_configure(
            flow["flow_id"], {"profile": f"edit:{profile_id}"}
        )

    async def save(flow, values):
        flow = await hass.config_entries.options.async_configure(flow["flow_id"], values)
        assert flow["step_id"] == "profile_details", flow.get("errors")
        flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
        assert flow["reason"] == "profile_saved"

    flow = await edit("p1")
    await save(flow, {**shown(flow), "name": "office a renamed"})
    flow = await edit("p2")
    unchanged = shown(flow)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], unchanged)
    assert flow["errors"] == {"work_station": "same_station"}
    await save(flow, {**unchanged, "work_station": "Rotterdam Centraal"})
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"]
    assert [p["name"] for p in saved] == ["office a renamed", "office_b"]
    assert saved[1]["work_station"] == "RTD"


async def test_a_name_another_profile_has_is_refused_in_plain_words(hass):
    write_profiles(hass, PROFILE)
    flow = await options_add(hass)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**VALUES, "name": "Office A", "days": ["sun"]}
    )
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {"name": "name_taken"}
    assert flow["description_placeholders"]["report"] == ""
    assert profiles_path(hass).read_text() == PROFILE


async def options_add(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    return await hass.config_entries.options.async_configure(flow["flow_id"], {"profile": "new"})


async def test_add_a_profile_suggests_a_free_name_and_explains_a_day_clash(hass):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][1]["name"] = "commute"  # office_a runs mon-fri, commute on sat
    write_profiles(hass, yaml.safe_dump(raw))
    flow = await options_add(hass)
    assert flow["step_id"] == "profile"
    assert shown(flow)["name"] == "commute 2"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**VALUES, "name": "commute 2", "days": ["tue", "sun"]}
    )
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {"days": "days_clash"}
    assert flow["description_placeholders"]["other"] == "office_a"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**VALUES, "name": "commute 2", "days": ["sun"]}
    )
    assert flow["step_id"] == "profile_details", flow.get("errors")


@pytest.mark.parametrize("field", ["home_station", "work_station"])
@pytest.mark.parametrize("typed", ["Nowhere", "Rotterdam", " "])
async def test_a_station_not_in_the_list_is_refused(hass, field, typed):
    flow = await configure(hass, await guided(hass), {**VALUES, field: typed})
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {field: "unknown_station"}
    assert shown(flow).get(field, " ") == typed  # the typed text stays to be fixed
    assert not profiles_path(hass).exists()


@pytest.mark.parametrize(
    "typed",
    ["rotterdam centraal", "Rotterdam Centraal ", " ROTTERDAM CENTRAAL", "Rotterdam C.", "RTD"],
)
async def test_a_typed_name_the_catalogue_knows_is_taken(hass, monkeypatch, typed):
    """Other case, extra spaces, a short name or the code all mean the one station."""
    fake_phone(hass, monkeypatch)
    catalogue = Stations({**CATALOGUE.labels, "RTD": ["Rotterdam Centraal", "Rotterdam C."]})
    flow = await configure(hass, await guided(hass, catalogue), {**VALUES, "work_station": typed})
    flow = await details(hass, flow)
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["work_station"] == "RTD"


@pytest.mark.parametrize("typed", ["Home station", "home station"])
async def test_a_name_two_stations_share_is_refused_when_typed(hass, typed):
    catalogue = Stations({**CATALOGUE.labels, "BBB": ["Home station"]})
    flow = await configure(hass, await guided(hass, catalogue), {**VALUES, "home_station": typed})
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {"home_station": "unknown_station"}


COLLIDING = {"AAA": ["Home"], "BBB": ["Home"], "CCC": ["Home (AAA)"], "DDD": ["Home (AAA) (AAA)"]}


@pytest.mark.parametrize("order", list(permutations(COLLIDING)))
def test_every_station_keeps_its_own_label(order):
    """A real name that looks like a generated label never hides a station, in any order."""
    flow = ProfileEditorFlow()
    flow._catalogue = Stations({code: COLLIDING[code] for code in order})
    labels = flow._station_codes()
    assert sorted(labels.values()) == sorted(COLLIDING)  # one label per station
    assert labels["Home (AAA)"] == "CCC" and labels["Home (AAA) (AAA)"] == "DDD"
    assert all(flow._station_code(label) == code for label, code in labels.items())


async def test_an_existing_profile_with_a_colliding_label_opens_filled(hass):
    write_profiles(hass, PROFILE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    catalogue = Stations({**CATALOGUE.labels, **COLLIDING})
    with patch(CATALOGUE_CALL, return_value=catalogue):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    defaults = shown(flow)
    assert defaults["home_station"] == "Home (AAA) (AAA) (AAA)"  # AAA, never an empty field
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], defaults)
    assert flow["step_id"] == "profile_details", flow.get("errors")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
    assert flow["reason"] == "profile_saved"
    assert yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["home_station"] == "AAA"


@pytest.mark.parametrize("typed", ["Home station (BBB)", " Home station (BBB) "])
async def test_two_stations_with_one_name_stay_apart(hass, typed):
    catalogue = Stations({**CATALOGUE.labels, "BBB": ["Home station"]})
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with (
        patch(CHECK, return_value=frozenset(catalogue)),
        patch(CATALOGUE_CALL, return_value=catalogue),
    ):
        flow = await configure(hass, flow, {"api_key": "k"})
    selector = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "home_station")
    assert {"Home station (AAA)", "Home station (BBB)"} <= set(selector.config["options"])
    flow = await configure(hass, flow, {**VALUES, "home_station": typed})
    assert flow["step_id"] == "profile_details", flow.get("errors")


async def test_guided_weekday_times_wait_for_validation_and_phone_confirmation(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await configure(hass, await guided(hass), VALUES)
    flow = await details(hass, flow, edit_weekday_times=True)
    assert flow["step_id"] == "profile_weekdays" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"outbound_tue": "25:00", "return_tue": "18:40"})
    assert flow["step_id"] == "profile_weekdays" and flow["errors"]["base"] == "invalid_profile"
    assert not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"outbound_tue": "09:12", "return_tue": "18:40"})
    assert flow["step_id"] == "notify" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]
    assert saved["outbound"]["first_train_by_weekday"] == {"tue": "09:12"}
    assert saved["return"]["first_train_by_weekday"] == {"tue": "18:40"}


async def test_reversed_holiday_remains_on_details_form_without_writing(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await configure(hass, await guided(hass), VALUES)
    flow = await details(
        hass, flow, pause_action="add", pause_start="2027-04-07", pause_end="2027-04-01"
    )
    assert flow["step_id"] == "profile_details"
    assert flow["errors"] == {"base": "invalid_profile"}
    assert "start" in flow["description_placeholders"]["report"]
    assert not profiles_path(hass).exists()


async def test_catalogue_failure_is_visible_and_does_not_write(hass):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with (
        patch(CHECK, return_value=frozenset(CATALOGUE)),
        patch(CATALOGUE_CALL, side_effect=ProviderError),
    ):
        flow = await configure(hass, flow, {"api_key": "k"})
    assert flow["errors"] == {"base": "cannot_connect"}
    assert not profiles_path(hass).exists()


async def test_an_empty_catalogue_is_a_clear_error_and_does_not_write(hass):
    flow = await guided(hass, Stations({}))
    assert flow["step_id"] == "profile_error"
    assert flow["errors"] == {"base": "cannot_connect"}
    assert not profiles_path(hass).exists()


@pytest.mark.parametrize(
    "live_today, telegram_status_pin, saved_history",
    [(True, True, None), (False, True, True), (True, False, False)],
)
async def test_options_profile_edit_preserves_advanced_settings(
    hass, live_today, telegram_status_pin, saved_history
):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["pauses"] = [{"start": "2027-04-01", "end": "2027-04-07"}]
    raw["profiles"][0]["alerts"] = {
        "delay_min": 7,
        "delay_step_min": 4,
        "weather": False,
        "leave_by": False,
        "weather_allowance": False,
        "custom_timetable_horizon": False,
        "timetable_horizon_unit": "commute_dates",
        "timetable_horizon_count": 14,
        **({"alert_history": saved_history} if saved_history is not None else {}),
        "daily_brief": False,
    }
    raw["profiles"][0]["brief"] = {"at": "06:45", "evening_at": "19:30", "only_if_something": True}
    write_profiles(hass, yaml.safe_dump(raw))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    assert flow["step_id"] == "profiles"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    assert flow["step_id"] == "profile"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**VALUES, "name": "renamed"}
    )
    assert flow["step_id"] == "profile_details"
    assert shown(flow)["delay_min"] == 7
    assert shown(flow)["delay_step_min"] == 4
    assert shown(flow)["weather_allowance"] is False
    assert shown(flow)["leave_by"] is False
    assert shown(flow)["custom_timetable_horizon"] is False
    assert shown(flow)["timetable_horizon_unit"] == "commute_dates"
    assert shown(flow)["timetable_horizon_count"] == 14
    assert shown(flow)["daily_brief"] is False
    assert shown(flow)["brief_at"] == "06:45"
    assert shown(flow)["brief_evening_at"] == "19:30"
    assert shown(flow)["brief_only_if_something"] is True
    count_selector = next(
        selector
        for field, selector in flow["data_schema"].schema.items()
        if field.schema == "timetable_horizon_count"
    )
    assert count_selector.serialize()["selector"]["number"]["max"] == 14
    for invalid in (True, False, "7", None, 7.5):
        with pytest.raises(vol.Invalid):
            count_selector(invalid)
    assert shown(flow)["live_today"] is True
    assert shown(flow)["telegram_status_pin"] is True
    assert shown(flow)["ntfy_enabled"] is True
    assert shown(flow)["alert_history"] is bool(saved_history)
    assert shown(flow)["calendar_feed"] is True
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "minimum_UT": 8,
            "pause_action": "keep",
            "delay_min": 10.0,
            "delay_step_min": 5.0,
            "custom_timetable_horizon": True,
            "timetable_horizon_unit": "calendar_days",
            "timetable_horizon_count": 10.0,
            "live_today": live_today,
            "telegram_status_pin": telegram_status_pin,
            "ntfy_enabled": False,
            "alert_history": not saved_history,
            "calendar_feed": False,
            "daily_brief": True,
            "brief_at": "",
            "brief_evening_at": "21:00",
            "brief_only_if_something": False,
        },
    )
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())
    assert saved["profiles"][0]["id"] == "p1"
    assert saved["profiles"][0]["pauses"] == raw["profiles"][0]["pauses"]
    assert saved["profiles"][0]["alerts"] == {
        "delay_min": 10,
        "delay_step_min": 5,
        "disabled_categories": [],
        "weather": False,
        "leave_by": False,
        "weather_allowance": False,
        "custom_timetable_horizon": True,
        "timetable_horizon_unit": "calendar_days",
        "timetable_horizon_count": 10,
        "ntfy_enabled": False,
        "alert_history": not saved_history,
        "calendar_feed": False,
        **({"live_today": False} if not live_today else {}),
        **({"telegram_status_pin": False} if not telegram_status_pin else {}),
        "daily_brief": True,
    }
    assert saved["profiles"][0]["brief"] == {"evening_at": "21:00"}
    assert saved["profiles"][1] == raw["profiles"][1]


@pytest.mark.parametrize("webhooks", [False, True])
async def test_channel_form_only_offers_configured_destinations_and_saves_selection(hass, webhooks):
    raw = yaml.safe_load(PROFILE)
    if webhooks:
        raw["profiles"][0].setdefault("alerts", {})["channels"] = {
            "journey": ["primary", "webhook"]
        }
    write_profiles(hass, yaml.safe_dump(raw))
    options = {"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY}
    if webhooks:
        options["webhook_urls"] = ["https://example.test/hook"]
    entry = MockConfigEntry(domain=DOMAIN, data={"api_key": "k"}, options=options)
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], VALUES)
    selector = next(
        value for key, value in flow["data_schema"].schema.items() if str(key) == "channel_journey"
    )
    choices = {option["value"] for option in selector.config["options"]}
    assert choices == (
        {"default", "primary", "webhook", "primary_webhook"} if webhooks else {"default", "primary"}
    )
    if webhooks:
        field = next(k for k in flow["data_schema"].schema if str(k) == "channel_journey")
        assert field.default() == "primary_webhook"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "minimum_UT": 7,
            "pause_action": "keep",
            "channel_journey": "primary_webhook" if webhooks else "primary",
        },
    )
    assert flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())
    assert saved["profiles"][0]["alerts"]["channels"] == {
        "journey": ["primary", "webhook"] if webhooks else ["primary"]
    }


async def test_editor_rejects_new_file_created_during_notification_tests(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await guided(hass)
    flow = await configure(hass, flow, VALUES)
    flow = await details(hass, flow)
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    write_profiles(hass, PROFILE)
    flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["step_id"] == "profile_error"
    assert flow["errors"] == {"base": "file_changed"}
    assert not hass.config_entries.async_entries(DOMAIN)
    assert profiles_path(hass).read_text() == PROFILE
    # retrying moves on to the path form with the new file filled in, not the same error
    flow = await configure(hass, flow, {})
    assert flow["step_id"] == "profiles_file" and not flow["errors"]
    path = next(k.default() for k in flow["data_schema"].schema if str(k) == "profiles_path")
    assert path == str(profiles_path(hass))
    flow = await configure(hass, flow, {"profiles_path": path})
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY and flow["description"] is None
    assert flow["options"]["profiles_path"] == path
    assert profiles_path(hass).read_text() == PROFILE  # the draft was dropped, never written


async def test_guided_window_profile_and_day_overrides_round_trip(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    step1 = {k: v for k, v in VALUES.items() if k != "return_time"}
    window = {"return_mode": "window", "return_window_start": "15:00"}
    flow = await configure(hass, await guided(hass), {**step1, **window})
    assert flow["step_id"] == "profile" and flow["errors"]["base"] == "invalid_profile"
    assert "return.window" in flow["description_placeholders"]["report"]
    window["return_window_end"] = "20:00"
    flow = await configure(hass, flow, {**step1, **window, "return_min_stay": 480})
    flow = await details(hass, flow, edit_day_overrides=True)
    assert flow["step_id"] == "profile_day_pick"
    selector = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "day")
    assert "return_fri" in {o["value"] for o in selector.config["options"]}
    flow = await configure(hass, flow, {"day": "return_fri"})
    assert flow["step_id"] == "profile_day"
    flow = await configure(hass, flow, {"mode": "fixed", "pick": "default"})
    assert flow["step_id"] == "profile_day" and flow["errors"]["base"] == "invalid_profile"
    flow = await configure(
        hass,
        flow,
        {
            "mode": "default",
            "window_start": "13:00",
            "window_end": "18:00",
            "min_stay": 300,
            "pick": "default",
        },
    )
    assert flow["step_id"] == "profile_day_pick"
    assert "return Friday" in flow["description_placeholders"]["overrides"]
    flow = await configure(hass, flow, {"day": "return_thu"})
    flow = await configure(hass, flow, {"mode": "default", "pick": "default", "min_stay": 0})
    assert flow["step_id"] == "profile_day_pick"  # 0 turns the stay off on Thursday
    flow = await configure(hass, flow, {"day": "done"})
    assert flow["step_id"] == "notify" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["return"]
    assert "first_train" not in saved
    assert saved["mode"] == "window" and saved["window"] == ["15:00", "20:00"]
    assert saved["min_stay_min"] == 480
    assert saved["by_weekday"] == {
        "fri": {"window": ["13:00", "18:00"], "min_stay_min": 300},
        "thu": {"min_stay_min": 0},
    }


def shown(flow) -> dict:
    """What the frontend sends back for an untouched form: suggested values and defaults."""
    out = {}
    for key in flow["data_schema"].schema:
        suggested = (key.description or {}).get("suggested_value")
        if suggested not in (None, ""):
            out[str(key)] = suggested
        elif not isinstance(key.default, type(vol.UNDEFINED)):
            out[str(key)] = key.default()
    return out


async def test_reopening_a_window_profile_saves_it_unchanged(hass):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["return"] = {
        "mode": "window",
        "window": ["15:00", "20:00"],
        "pick": "on_leave",
        "min_stay_min": 480,
        "buffer_min": 5,
        "transfers": [],
    }
    write_profiles(hass, yaml.safe_dump(raw))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    assert "return_time" not in shown(flow)  # blank: no first train is invented
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
    assert flow["step_id"] == "profile_details"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["return"]
    assert saved == raw["profiles"][0]["return"]


@pytest.mark.parametrize("keep_default_target", [False, True])
async def test_default_pick_change_lets_editor_repair_inherited_arrival_overrides(
    hass, keep_default_target
):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["outbound"].update(
        mode="window",
        window=["07:00", "09:00"],
        pick="arrive_by",
        arrive_by="09:00",
        by_weekday={
            "mon": {"arrive_by": "09:15"},
            "tue": {"arrive_by": "09:20", "walk_after_min": 8},
        },
    )
    if keep_default_target:
        raw["profiles"][0]["outbound"]["by_weekday"]["wed"] = {
            "pick": "arrive_by",
            "prefer_direct_min": 7,
        }
    write_profiles(hass, yaml.safe_dump(raw))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)

    async def step(flow, values):
        return await hass.config_entries.options.async_configure(flow["flow_id"], values)

    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await step(flow, {"next_step_id": "profiles"})
    flow = await step(flow, {"profile": "edit:p1"})
    flow = await step(flow, {**shown(flow), "outbound_pick": "earliest"})
    assert flow["step_id"] == "profile_details", flow.get("errors")
    flow = await step(flow, {**shown(flow), "edit_weekday_times": True})
    flow = await step(flow, shown(flow))
    assert flow["step_id"] == "profile_day_pick"
    flow = await step(flow, {"day": "done"})
    assert flow["step_id"] == "profile_day_pick" and flow["errors"]["base"] == "invalid_profile"
    assert "outbound.by_weekday.mon.arrive_by" in flow["description_placeholders"]["report"]
    assert yaml.safe_load(profiles_path(hass).read_text()) == raw

    flow = await step(flow, {"day": "outbound_mon"})
    assert shown(flow)["arrive_by"] == "09:15"
    flow = await step(flow, {**shown(flow), "pick": "arrive_by"})
    assert flow["step_id"] == "profile_day_pick"
    flow = await step(flow, {"day": "outbound_tue"})
    assert shown(flow)["walk_after_min"] == 8
    flow = await step(flow, {**shown(flow), "pick": "arrive_by"})
    flow = await step(flow, {"day": "done"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["outbound"]
    assert saved.get("pick", "earliest") == "earliest"
    assert saved["arrive_by"] == "09:00"
    expected = {
        "mon": {"pick": "arrive_by", "arrive_by": "09:15"},
        "tue": {"pick": "arrive_by", "arrive_by": "09:20", "walk_after_min": 8},
    }
    if keep_default_target:
        expected["wed"] = {"pick": "arrive_by", "prefer_direct_min": 7}
    assert saved["by_weekday"] == expected


async def test_editing_a_located_profile_shows_and_saves_other_stations_and_day_overrides(hass):
    """The options flow on a profile with a location block: the first step carries the window
    settings, the details step the other station settings, and a day override is saved too."""
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["location"] = {"source": "ha", "entity": "person.me"}
    write_profiles(hass, yaml.safe_dump(raw, sort_keys=False))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    catalogue = Stations({**CATALOGUE.labels, "BBB": ["Other station"]})

    async def edit():
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        with patch(CATALOGUE_CALL, return_value=catalogue):
            flow = await hass.config_entries.options.async_configure(
                flow["flow_id"], {"next_step_id": "profiles"}
            )
        return await hass.config_entries.options.async_configure(
            flow["flow_id"], {"profile": "edit:p1"}
        )

    async def step(flow, values):
        return await hass.config_entries.options.async_configure(flow["flow_id"], values)

    flow = await edit()
    assert flow["step_id"] == "profile"
    keys = {str(k) for k in flow["data_schema"].schema}
    for d in ("outbound", "return"):
        assert {
            f"{d}_{k}" for k in ("mode", "window_start", "window_end", "pick", "min_stay")
        } <= keys
    window = {
        "return_mode": "window",
        "return_window_start": "15:00",
        "return_window_end": "20:00",
        "return_pick": "earliest_silent",
        "return_min_stay": 480,
    }
    first = {k: v for k, v in shown(flow).items() if k != "return_time"}
    flow = await step(flow, {**first, **window})
    assert flow["step_id"] == "profile_details", flow.get("errors")
    defaults = shown(flow)
    assert {
        k: defaults[k]
        for k in (
            "outbound_other_stations",
            "return_other_stations",
            "other_station_radius_m",
            "other_station_dwell_min",
            "ask_to_remember_station",
            "edit_day_overrides",
        )
    } == {
        "outbound_other_stations": [],
        "return_other_stations": [],
        "other_station_radius_m": 500,
        "other_station_dwell_min": 0,
        "ask_to_remember_station": True,
        "edit_day_overrides": False,
    }
    selector = next(
        v for k, v in flow["data_schema"].schema.items() if str(k) == "return_other_stations"
    )
    assert "BBB" in {o["value"] for o in selector.config["options"]}
    flow = await step(
        flow,
        {
            **defaults,
            "return_other_stations": ["BBB"],
            "other_station_radius_m": 400.0,
            "other_station_dwell_min": 2.0,
            "ask_to_remember_station": False,
            "edit_day_overrides": True,
        },
    )
    assert flow["step_id"] == "profile_day_pick", flow.get("errors")
    flow = await step(flow, {"day": "return_fri"})
    assert flow["step_id"] == "profile_day"
    flow = await step(
        flow, {"mode": "default", "window_start": "13:00", "window_end": "18:00", "pick": "default"}
    )
    assert flow["step_id"] == "profile_day_pick", flow.get("errors")
    flow = await step(flow, {"day": "done"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"

    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]
    assert saved["location"] == {
        "source": "ha",
        "entity": "person.me",
        "other_station_radius_m": 400,
        "other_station_dwell_min": 2,
        "ask_to_remember_station": False,
    }
    assert saved["return"]["other_stations"] == ["BBB"]
    assert "other_stations" not in saved["outbound"]
    assert saved["return"]["mode"] == "window" and saved["return"]["window"] == ["15:00", "20:00"]
    assert saved["return"]["pick"] == "earliest_silent" and saved["return"]["min_stay_min"] == 480
    assert saved["return"]["by_weekday"] == {"fri": {"window": ["13:00", "18:00"]}}

    flow = await edit()  # reopened, the saved settings are what the forms show
    assert {k: shown(flow).get(k) for k in window} == window
    flow = await step(flow, shown(flow))
    again = shown(flow)
    assert again["return_other_stations"] == ["BBB"]
    assert again["other_station_radius_m"] == 400 and again["other_station_dwell_min"] == 2
    assert again["ask_to_remember_station"] is False
    flow = await step(flow, {**again, "edit_day_overrides": True})
    assert flow["step_id"] == "profile_day_pick"
    assert "return Friday" in flow["description_placeholders"]["overrides"]


async def test_a_profile_that_names_its_stations_opens_filled_and_saves_codes(hass):
    """Names fill the station fields; the edited profile is saved with codes and one
    transfer minimum per station, and the other profile keeps its names."""
    raw = yaml.safe_load(PROFILE)
    first, second = raw["profiles"]
    first.update(home_station="Home station", transfer_min={"Utrecht": 9})
    first["outbound"]["transfers"] = ["Utrecht Centraal"]
    first["location"] = {"source": "ha", "entity": "person.me"}
    first["return"]["other_stations"] = ["Other station"]
    second["work_station"] = "Rotterdam Centraal"
    write_profiles(hass, yaml.safe_dump(raw, sort_keys=False))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    catalogue = Stations({**CATALOGUE.labels, "BBB": ["Other station"]})
    with patch(CATALOGUE_CALL, return_value=catalogue):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    defaults = shown(flow)
    assert (defaults["home_station"], defaults["outbound_transfers"]) == ("Home station", ["UT"])
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], defaults)
    assert flow["step_id"] == "profile_details", flow.get("errors")
    defaults = shown(flow)
    assert defaults["minimum_UT"] == 9 and defaults["return_other_stations"] == ["BBB"]
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], defaults)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"]
    assert saved[0]["home_station"] == "AAA" and saved[0]["transfer_min"] == {"UT": 9}
    assert saved[0]["outbound"]["transfers"] == ["UT"]
    assert saved[0]["return"]["other_stations"] == ["BBB"]
    assert saved[1] == second


async def test_an_old_profile_stored_with_codes_opens_with_names_and_saves_codes(hass):
    write_profiles(hass, PROFILE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    defaults = shown(flow)
    assert (defaults["home_station"], defaults["work_station"]) == (
        "Home station",
        "Rotterdam Centraal",
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], defaults)
    assert flow["step_id"] == "profile_details", flow.get("errors")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
    assert flow["reason"] == "profile_saved"
    before = yaml.safe_load(PROFILE)["profiles"][0]
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]
    assert (saved["home_station"], saved["work_station"]) == ("AAA", "RTD")
    assert saved["outbound"]["transfers"] == before["outbound"]["transfers"]
