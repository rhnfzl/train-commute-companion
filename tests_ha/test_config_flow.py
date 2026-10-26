import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from harness import NOTIFY, PROFILE, PROFILES, profiles_path, setup_tcc, write_profiles
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.train_commute_companion import notifier
from custom_components.train_commute_companion.const import DOMAIN
from train_commute_companion.providers.base import AuthError

CHECK_KEY = "custom_components.train_commute_companion.config_flow.check_key"
SETUP = "custom_components.train_commute_companion.async_setup_entry"
KNOWN = frozenset({"AAA", "BBB", "UT", "RTD"})
ROOT = Path(__file__).resolve().parents[1]
CAVEAT = (
    "Phone location is not always reliable. In stations, tunnels, or with battery saving on, "
    "your position can be late or wrong. Location only helps the companion. It never hides a "
    "warning about your train."
)


@pytest.fixture(autouse=True)
def allow_config_dir(hass):
    # HA adds its config folder to this list at startup; the test hass leaves it empty
    hass.config.allowlist_external_dirs = {str(Path(hass.config.config_dir).resolve())}


def read_fixture(name: str) -> str:
    return (ROOT / "tests" / name).read_text()


def level(call) -> str:
    return call.data["data"]["push"]["interruption-level"]


async def step(hass, r, data):
    return await hass.config_entries.flow.async_configure(r["flow_id"], data)


async def flow_to_notify(hass, key=KNOWN):
    write_profiles(hass, PROFILE)
    with patch(CHECK_KEY, return_value=key):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert r["step_id"] == "user"
        r = await step(hass, r, {"api_key": "k", "profiles_path": str(profiles_path(hass))})
    return r


async def flow_to_test_menu(hass, style=None):
    r = await flow_to_notify(hass)
    assert r["step_id"] == "notify"
    data = {"notify_service": NOTIFY, **({"loud_style": style} if style else {})}
    return await step(hass, r, data)


async def test_entry_created_only_after_both_test_pushes_confirmed(hass):
    calls = async_mock_service(hass, "notify", NOTIFY)
    r = await flow_to_test_menu(hass)
    assert [level(c) for c in calls] == ["time-sensitive", "passive"]
    assert [c.data["message"] for c in calls] == [
        "Train Commute Companion test: this is how a loud alert arrives.",
        "Train Commute Companion test: this is how a silent update arrives.",
    ]
    assert r["type"] is FlowResultType.MENU and r["step_id"] == "test"
    assert not hass.config_entries.async_entries(DOMAIN)
    with patch(SETUP, return_value=True):
        r = await step(hass, r, {"next_step_id": "test_ok"})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["data"] == {"api_key": "k"}
    assert (
        r["options"]["notify_service"] == NOTIFY and r["options"]["loud_style"] == "time_sensitive"
    )
    assert r["options"]["profiles_path"] == str(profiles_path(hass))
    assert r["title"] == "Train Commute Companion"


async def test_critical_style_test_push_is_critical(hass):
    calls = async_mock_service(hass, "notify", NOTIFY)
    r = await flow_to_test_menu(hass, "critical")
    assert [level(c) for c in calls] == ["critical", "passive"]
    with patch(SETUP, return_value=True):
        r = await step(hass, r, {"next_step_id": "test_ok"})
    assert r["options"]["loud_style"] == "critical"


async def test_resend_sends_both_again(hass):
    calls = async_mock_service(hass, "notify", NOTIFY)
    r = await flow_to_test_menu(hass)
    r = await step(hass, r, {"next_step_id": "test_resend"})
    assert r["type"] is FlowResultType.MENU and r["step_id"] == "test"
    assert [level(c) for c in calls] == ["time-sensitive", "passive"] * 2
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("failure", ["missing", "raising", "hanging"])
async def test_failed_test_push_stays_on_notify_step(hass, failure):
    async def hang(call):
        await asyncio.sleep(3600)

    if failure == "raising":
        async_mock_service(hass, "notify", NOTIFY, raise_exception=HomeAssistantError("no"))
    else:
        hass.services.async_register("notify", NOTIFY, hang)
    r = await flow_to_notify(hass)
    assert r["step_id"] == "notify"
    if failure == "missing":
        hass.services.async_register("notify", "mobile_app_other", hang)
        hass.services.async_remove("notify", NOTIFY)
    with patch.object(notifier, "NOTIFY_TIMEOUT_S", 0.05):
        r = await step(hass, r, {"notify_service": NOTIFY})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "notify"
    assert r["errors"] == {"base": "send_failed"}
    assert r["description_placeholders"] == {"service": NOTIFY}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_step_errors(hass):
    async_mock_service(hass, "notify", NOTIFY)
    write_profiles(hass, PROFILE)
    path = str(profiles_path(hass))
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    for bad, error in (("invalid_auth", "invalid_auth"), ("cannot_connect", "cannot_connect")):
        with patch(CHECK_KEY, return_value=bad):
            r = await step(hass, r, {"api_key": "k", "profiles_path": path})
        assert r["errors"] == {"base": error}
    folder = hass.config.config_dir
    with patch(CHECK_KEY, return_value=KNOWN):
        r = await step(hass, r, {"api_key": "k", "profiles_path": folder})
    assert r["errors"] == {"profiles_path": "file_missing"}
    assert r["description_placeholders"] == {"path": folder}


async def test_path_outside_allowed_dirs_errors(hass):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(CHECK_KEY, return_value=KNOWN):
        r = await step(hass, r, {"api_key": "k", "profiles_path": "/etc/hosts"})
    assert r["errors"] == {"profiles_path": "path_not_allowed"}


async def test_no_mobile_app_aborts(hass):
    r = await flow_to_notify(hass)  # no notify service exists yet, so the notify step aborts
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "no_mobile_app"


async def test_second_entry_aborts_single_instance(hass):
    MockConfigEntry(domain=DOMAIN, data={"api_key": "k"}).add_to_hass(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "single_instance_allowed"


@pytest.mark.usefixtures("tcc_freezer")
async def test_key_rejected_starts_reauth_and_new_key_reloads(hass):
    h = await setup_tcc(hass, PROFILE)
    h.provider.error = lambda q: AuthError()
    await h.host.run(lambda rt: rt.poll_once())
    await h.entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    await h.entry.runtime_data.coordinator.async_refresh()  # once per outage
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    old = h.entry.runtime_data
    h.provider.error = lambda q: None
    with patch(CHECK_KEY, return_value="invalid_auth"):
        r = await step(hass, flows[0], {"api_key": "bad"})
    assert r["errors"] == {"base": "invalid_auth"}
    with patch(CHECK_KEY, return_value=KNOWN):
        r = await step(hass, flows[0], {"api_key": "new"})
    await hass.async_block_till_done()
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reauth_successful"
    assert h.entry.data["api_key"] == "new" and h.entry.data["started"] is True
    assert h.entry.runtime_data is not old


# --- options flow ---


def add_entry(hass, **options):
    write_profiles(hass, PROFILE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={
            "profiles_path": str(profiles_path(hass)),
            "notify_service": NOTIFY,
            "loud_style": "time_sensitive",
            **options,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def options_validate(hass, text, key=KNOWN, entry=None):
    entry = entry or add_entry(hass)
    write_profiles(hass, text)
    with patch(CHECK_KEY, return_value=key):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        assert r["type"] is FlowResultType.MENU
        return await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "validate"}
        )


async def test_options_validate_lists_errors_suggestions_and_dst(hass):
    r = await options_validate(hass, read_fixture("data/profiles/bad_starter.yaml"))
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "validate"
    report = r["description_placeholders"]["report"]
    assert "transfer_min" in report and "warning:" in report
    # submitting only closes the screen: nothing saved, so nothing reloads
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "validated"


async def test_options_validate_reports_missing_person_and_zone(hass):
    r = await options_validate(hass, (PROFILES / "located.yaml").read_text())
    report = r["description_placeholders"]["report"]
    assert "office_a.location.entity: person.someone not found in Home Assistant" in report
    assert "office_a.location.zones.home: zone.home not found in Home Assistant" in report

    hass.states.async_set("zone.home", "0")
    r = await options_validate(hass, (PROFILES / "located_bad.yaml").read_text())
    report = r["description_placeholders"]["report"]
    assert "office_a.location.entity: light.x must be a person or device_tracker" in report
    assert "office_a.location.zones.work: sensor.y must be a zone" in report
    assert "zones.home" not in report

    hass.states.async_set("person.someone", "home")
    r = await options_validate(hass, (PROFILES / "located.yaml").read_text())
    assert r["description_placeholders"]["report"] == "No problems found."


async def test_options_validate_starter_file_reports_only_its_placeholder(hass):
    r = await options_validate(
        hass, (ROOT / "examples" / "profiles.yaml").read_text(), frozenset({"UT", "RTD", "BBB"})
    )
    lines = r["description_placeholders"]["report"].splitlines()
    assert lines and all("AAA" in x for x in lines), lines


async def test_options_validate_unloaded_with_key_failure(hass):
    text = (PROFILES / "office_a.yaml").read_text()
    for key, start in (("invalid_auth", "NS key rejected"), ("cannot_connect", "NS not reachable")):
        r = await options_validate(hass, text, key=key, entry=None)
        lines = r["description_placeholders"]["report"].splitlines()
        assert lines[0].startswith(start)
        assert lines[-1].startswith("station codes not checked")
        assert len(lines) == 2  # no station is named unknown
        for e in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_remove(e.entry_id)


@pytest.mark.usefixtures("tcc_freezer")
async def test_options_validate_loaded_uses_the_runtimes_stations(hass):
    h = await setup_tcc(hass, PROFILE)
    with patch(CHECK_KEY, side_effect=AssertionError("not asked")):
        r = await hass.config_entries.options.async_init(h.entry.entry_id)
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "validate"}
        )
    assert r["description_placeholders"]["report"] == "No problems found."


def test_validate_description_has_location_caveat_verbatim():
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((ROOT / "custom_components" / DOMAIN / name).read_text())
        desc = data["options"]["step"]["validate"]["description"]
        assert desc == "{report}\n\n" + CAVEAT
    a = (ROOT / "custom_components" / DOMAIN / "strings.json").read_text()
    assert a == (ROOT / "custom_components" / DOMAIN / "translations/en.json").read_text()


async def settings(hass, entry, **user):
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"next_step_id": "settings"}
    )
    assert r["step_id"] == "settings"
    return await hass.config_entries.options.async_configure(r["flow_id"], user)


def current(entry, **change):
    return {
        "profiles_path": entry.options["profiles_path"],
        "notify_service": entry.options["notify_service"],
        "loud_style": entry.options["loud_style"],
        **change,
    }


async def test_options_settings_path_checked_like_setup(hass):
    async_mock_service(hass, "notify", NOTIFY)
    entry = add_entry(hass)
    before = dict(entry.options)
    for path, error in (
        ("/etc/hosts", "path_not_allowed"),
        (str(Path(hass.config.config_dir) / "nope.yaml"), "file_missing"),
        (hass.config.config_dir, "file_missing"),
    ):
        r = await settings(hass, entry, **current(entry, profiles_path=path))
        assert r["type"] is FlowResultType.FORM and r["errors"] == {"profiles_path": error}
        hass.config_entries.options.async_abort(r["flow_id"])
    assert dict(entry.options) == before


async def test_options_settings_path_only_saves_without_test_push(hass):
    calls = async_mock_service(hass, "notify", NOTIFY)
    entry = add_entry(hass)
    other = Path(hass.config.config_dir) / "other.yaml"
    other.write_text(PROFILE)
    with patch(SETUP, return_value=True):
        r = await settings(hass, entry, **current(entry, profiles_path=str(other)))
        await hass.async_block_till_done()
    assert r["type"] is FlowResultType.CREATE_ENTRY and not calls
    assert entry.options["profiles_path"] == str(other)


async def test_options_new_notify_target_needs_test_before_saving(hass):
    async_mock_service(hass, "notify", NOTIFY)
    tablet = async_mock_service(hass, "notify", "mobile_app_tablet")
    entry = add_entry(hass)
    r = await settings(hass, entry, **current(entry, notify_service="mobile_app_tablet"))
    assert r["type"] is FlowResultType.MENU and r["step_id"] == "test"
    assert [level(c) for c in tablet] == ["time-sensitive", "passive"]
    assert entry.options["notify_service"] == NOTIFY
    with patch(SETUP, return_value=True):
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "test_ok"}
        )
        await hass.async_block_till_done()
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["notify_service"] == "mobile_app_tablet"


async def test_options_new_loud_style_needs_test_before_saving(hass):
    calls = async_mock_service(hass, "notify", NOTIFY)
    entry = add_entry(hass)
    r = await settings(hass, entry, **current(entry, loud_style="critical"))
    assert r["type"] is FlowResultType.MENU
    assert [level(c) for c in calls] == ["critical", "passive"]
    assert entry.options["loud_style"] == "time_sensitive"
    with patch(SETUP, return_value=True):
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"next_step_id": "test_ok"}
        )
        await hass.async_block_till_done()
    assert entry.options["loud_style"] == "critical"


async def test_options_failed_test_push_returns_to_settings(hass):
    async_mock_service(hass, "notify", NOTIFY)
    async_mock_service(
        hass, "notify", "mobile_app_tablet", raise_exception=HomeAssistantError("no")
    )
    entry = add_entry(hass)
    r = await settings(hass, entry, **current(entry, notify_service="mobile_app_tablet"))
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "settings"
    assert r["errors"] == {"base": "send_failed"}
    assert entry.options["notify_service"] == NOTIFY
