import json
from dataclasses import replace

import pytest
from ha_rig import PROFILE, setup_tcc
from homeassistant.data_entry_flow import FlowResultType

from custom_components.train_commute_companion.diagnostics import async_get_config_entry_diagnostics
from custom_components.train_commute_companion.sensor import _status_attrs
from train_commute_companion.providers.weather import WeatherHour, WeatherView
from train_commute_companion.weather_allowance import weather_allowance


@pytest.mark.usefixtures("tcc_freezer")
async def test_user_can_set_clear_and_keep_their_own_warning_key(hass):
    h = await setup_tcc(hass, PROFILE)
    before = dict(h.entry.options)
    for key in ("user-weather-test-key", ""):
        result = await hass.config_entries.options.async_init(h.entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "weather"}
        )
        assert result["step_id"] == "weather"
        assert (
            result["description_placeholders"]["knmi_portal"]
            == "https://developer.dataplatform.knmi.nl/"
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"knmi_api_key": key}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert h.entry.options.get("knmi_api_key") == key
        assert all(h.entry.options[name] == value for name, value in before.items())
        diagnostics = await async_get_config_entry_diagnostics(hass, h.entry)
        assert "user-weather-test-key" not in json.dumps(diagnostics)


@pytest.mark.usefixtures("tcc_freezer")
async def test_weather_sensor_attributes_keep_attribution_and_unknown_warning_status(hass):
    h = await setup_tcc(hass, PROFILE)
    view = h.entry.runtime_data.coordinator.data
    occ = next(o for o in view.occurrences if o.legs)
    forecast = WeatherHour(view.at, 8, 2, 0, 61)
    weather = WeatherView("AAA", view.at, forecast, view.at, (), None, None)
    allowance = weather_allowance(occ.leave_by, 10, weather, view.at)
    view = replace(
        view, occurrences=(replace(occ, weather=(weather,), weather_allowance=allowance),)
    )
    attrs = _status_attrs(view, occ.key.profile_id, occ.key.direction)
    assert attrs["weather"][0]["forecast"]["temperature_c"] == 8
    assert "https://open-meteo.com/" in attrs["weather"][0]["text"]
    assert "warnings unavailable" in attrs["weather"][0]["text"]
    assert "polygons" not in json.dumps(attrs["weather"])
    assert attrs["leave_by"] == occ.leave_by.isoformat()
    assert attrs["weather_allowance"]["extra_min"] == 2
    assert (
        attrs["weather_allowance"]["suggested_leave_by"] == allowance.suggested_leave_by.isoformat()
    )
    assert attrs["leave_by_text"] == allowance.text
