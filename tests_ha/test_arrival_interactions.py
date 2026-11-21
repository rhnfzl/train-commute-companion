from dataclasses import replace
from datetime import UTC, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from ha_rig import NOTIFY, PROFILE, jump, profiles_path, setup_tcc, state_path
from homeassistant.core import Context, EventOrigin
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import DateTimeSelector
from runtime_seam import save_state, seed_state

from custom_components.train_commute_companion.arrival_flow import local_timestamp
from custom_components.train_commute_companion.const import DOMAIN, EVENT_ALERT
from custom_components.train_commute_companion.notifier import ARRIVAL_PREFIX, MOBILE_APP
from custom_components.train_commute_companion.services import MOBILE_ACTION
from train_commute_companion.model import Direction, okey
from train_commute_companion.store import JsonFileStore, StorageError

pytestmark = pytest.mark.usefixtures("tcc_freezer")


async def world(hass, **kwargs):
    h = await setup_tcc(hass, PROFILE, **kwargs)
    await jump(h, "2026-10-27 12:00")
    return h


async def saved(h):
    return next(
        r
        for r in await h.host.run(lambda rt: rt.arrival_records())
        if r.key.profile_id == "p1" and r.key.direction is Direction.OUTBOUND
    )


async def configure(h, flow, values):
    return await h.hass.config_entries.options.async_configure(flow["flow_id"], values)


async def details(h):
    flow = await h.hass.config_entries.options.async_init(h.entry.entry_id)
    flow = await configure(h, flow, {"next_step_id": "arrival"})
    selector = next(iter(flow["data_schema"].schema.values()))
    option = next(o for o in selector.config["options"] if "office_a, outbound" in o["label"])
    return await configure(h, flow, {"journey": option["value"]})


def confirmation(flow, **extra):
    selector = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "intent_id")
    token = selector.config["options"][0]["value"]
    return {"intent_id": token, "confirmed": True, **extra}


async def review(h, *, destination="RTD", at="2026-10-27T10:00:00", **extra):
    return await configure(
        h, await details(h), {"destination": destination, "arrived_at": at, **extra}
    )


async def service(h, **changes):
    record = await saved(h)
    values = {
        "profile": "office_a",
        "direction": record.key.direction.value,
        "service_date": str(record.key.service_date),
        "destination": "RTD",
        "arrived_at": "2026-10-27T10:00:00+01:00",
        "expected_revision": record.revision,
        "action_id": "test-arrival",
        **changes,
    }
    return await h.hass.services.async_call(
        DOMAIN,
        "confirm_arrival",
        values,
        blocking=True,
        return_response=True,
    )


async def test_picker_review_requires_explicit_confirmation_without_profile_reload(hass):
    h = await world(hass)
    original = profiles_path(hass).read_bytes()
    options = dict(h.entry.options)
    await h.host.run(lambda rt: rt.poll_once())
    assert (await saved(h)).arrived_at is None
    flow = await details(h)
    fields = {str(k): v for k, v in flow["data_schema"].schema.items()}
    assert isinstance(fields["arrived_at"], DateTimeSelector)
    assert "original_planned_arrival" not in fields
    assert "office_a" in flow["description_placeholders"]["journey"]
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        flow = await configure(h, flow, {"destination": "RTD", "arrived_at": "2026-10-27T10:00:00"})
        assert flow["step_id"] == "arrival_review"
        assert "10:00 CET" in flow["description_placeholders"]["arrival"]
        assert (await saved(h)).arrived_at is None
        flow = await configure(h, flow, confirmation(flow, confirmed=False))
        assert flow["errors"] == {"base": "arrival_acknowledgement"}
        result = await configure(h, flow, confirmation(flow))
        assert result["type"] is FlowResultType.ABORT and result["reason"] == "arrival_saved"
        assert "ns.nl" in result["description_placeholders"]["result"]
        assert (await saved(h)).revision == 1
        assert profiles_path(hass).read_bytes() == original and dict(h.entry.options) == options
        assert not reload.called


async def test_arrival_push_only_accepts_bound_mobile_action(hass):
    import yaml
    from pytest_homeassistant_custom_component.common import async_capture_events

    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["alerts"] = {"compensation_reminder": True}
    h = await setup_tcc(hass, yaml.safe_dump(raw))
    await jump(h, "2026-10-27 12:00")
    original = await saved(h)
    hass.data[MOBILE_APP] = {
        "config_entries": {
            f"hook-{NOTIFY}": SimpleNamespace(data={"user_id": "owner", "device_id": "phone"})
        }
    }
    bus = async_capture_events(hass, EVENT_ALERT)

    async def queue(rt):
        profile = next(p for p in rt.config.profiles if p.id == original.key.profile_id)
        record, obligation = rt._arrival_question(
            original, profile, rt.now() - timedelta(minutes=10), rt.now()
        )
        assert rt._writer.commit_strict(
            replace(
                rt.state,
                obligations=(*rt.state.obligations, obligation),
                journey_records={**rt.state.journey_records, okey(record.key): record},
            )
        )
        await rt._deliver()
        return record.prompt

    prompt = await h.host.run(queue)
    push_data = h.pushes[-1].data["data"]
    yes, other = push_data["actions"]
    assert yes == {"action": ARRIVAL_PREFIX + prompt.id, "title": "Yes"}
    assert other == {
        "action": "URI",
        "title": "Other time",
        "uri": f"/config/integrations/integration/{DOMAIN}",
    }
    assert push_data["push"]["interruption-level"] == "passive"
    assert not bus  # the private action key is sent only to the bound phone

    for origin, user, device in (
        (EventOrigin.local, "owner", None),
        (EventOrigin.remote, "other", None),
        (EventOrigin.remote, "owner", "another-device"),
        (EventOrigin.remote, "owner", None),  # iOS does not supply a device ID
    ):
        hass.bus.async_fire(
            MOBILE_ACTION,
            {"action": yes["action"], "device_id": device},
            origin,
            Context(user_id=user),
        )
        await hass.async_block_till_done()
        accepted = origin is EventOrigin.remote and user == "owner" and device is None
        assert (await saved(h)).arrived_at == (prompt.at if accepted else None)
    assert (await saved(h)).revision == 1


def picker_value(flow, field="arrived_at"):
    marker = next(k for k in flow["data_schema"].schema if str(k) == field)
    return marker.description["suggested_value"]


@pytest.mark.parametrize("arrived_at", ["2026-10-27T10:05:00+01:00", "2026-10-27T09:05:00+00:00"])
async def test_saved_arrival_prefills_the_datetime_picker_in_amsterdam_wall_time(hass, arrived_at):
    h = await world(hass)
    await service(h, arrived_at=arrived_at)
    value = picker_value(await details(h))
    # HA's datetime selector splits on a space and otherwise displays midnight.
    assert value.split(" ")[1:] == ["10:05:00"]
    assert value == "2026-10-27 10:05:00"


async def test_edited_review_keeps_the_picker_time_instead_of_midnight(hass):
    h = await world(hass)
    flow = await review(h, at="2026-10-27T09:05:00+00:00")
    flow = await configure(h, flow, confirmation(flow, edit=True))
    assert picker_value(flow) == "2026-10-27 10:05:00"
    assert (await saved(h)).arrived_at is None


async def test_invalid_hour_retry_preserves_both_picker_values(hass):
    h = await world(hass, world=[])
    flow = await configure(
        h,
        await details(h),
        {
            "destination": "RTD",
            "arrived_at": "2026-03-29T02:30:00",
            "original_planned_arrival": "2026-10-27T09:00:00+01:00",
        },
    )
    assert flow["errors"] == {"base": "arrival_time"}
    assert picker_value(flow) == "2026-03-29 02:30:00"
    assert picker_value(flow, "original_planned_arrival") == "2026-10-27 09:00:00"
    assert (await saved(h)).arrived_at is None


@pytest.mark.parametrize(
    "changes", [{"destination": "UT"}, {"arrived_at": "2026-10-27T13:00:00+01:00"}]
)
async def test_service_refuses_wrong_destination_and_future_without_mutation(hass, changes):
    h = await world(hass)
    before = await saved(h)
    with pytest.raises(HomeAssistantError):
        await service(h, **changes)
    assert await saved(h) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": True},
        {"expected_revision": 0.0},
        {"arrived_at": "2026-10-27T10:00:00"},
    ],
)
async def test_service_schema_rejects_ambiguous_revision_and_naive_time(hass, changes):
    h = await world(hass)
    with pytest.raises(vol.Invalid):
        await service(h, **changes)
    assert (await saved(h)).arrived_at is None


async def test_service_response_and_stale_open_form_guard(hass):
    h = await world(hass)
    flow = await review(h)
    result = await service(h)
    assert result["record"]["revision"] == 1 and "ns.nl" in result["text"]
    refused = await configure(h, flow, confirmation(flow))
    assert refused["errors"] == {"base": "arrival_refused"}
    assert (await saved(h)).revision == 1


@pytest.mark.parametrize("archived", [False, True])
async def test_service_exact_saved_profile_id_wins_another_profiles_name(hass, archived):
    import copy
    from dataclasses import replace

    import yaml

    from train_commute_companion.model import okey

    raw = yaml.safe_load(PROFILE)
    first = raw["profiles"][0]
    second = {**copy.deepcopy(first), "id": "office_a", "name": "office_b", "days": ["sat"]}
    raw["profiles"] = [first, second]
    h = await setup_tcc(hass, yaml.safe_dump(raw))
    await jump(h, "2026-10-27 12:00")
    original = await saved(h)
    # A retained journey can predate its profile's current weekday schedule.
    retained = replace(original, key=replace(original.key, profile_id="office_a"))
    await h.host.run(
        lambda rt: seed_state(
            rt,
            journey_records={
                **rt.state.journey_records,
                okey(retained.key): retained,
            },
        )
    )
    await h.host.run(save_state)
    before = {
        r.key.profile_id: r
        for r in await h.host.run(lambda rt: rt.arrival_records())
        if r.key.direction is Direction.OUTBOUND
    }
    assert before["p1"].destination == before["office_a"].destination
    assert before["p1"].revision == before["office_a"].revision == 0
    if archived:
        raw["profiles"] = [first]
        profiles_path(hass).write_text(yaml.safe_dump(raw))
        await h.host.run(lambda rt: seed_state(rt, occurrences={}))
        await h.host.run(save_state)
        await h.host.run(lambda rt: rt.reload())
        await h.entry.runtime_data.coordinator.async_refresh()
        assert all(
            i.profile_id != "office_a" for i in h.entry.runtime_data.coordinator.data.directions
        )
    result = await service(h, profile="office_a")
    assert result["record"]["key"]["profile_id"] == "office_a"
    after = {
        r.key.profile_id: r
        for r in await h.host.run(lambda rt: rt.arrival_records())
        if r.key.direction is Direction.OUTBOUND
    }
    assert after["p1"] == before["p1"]
    assert after["office_a"].revision == 1


async def test_review_edit_token_cannot_confirm_changed_details(hass):
    h = await world(hass)
    first = await review(h)
    old = confirmation(first)
    flow = await configure(h, first, confirmation(first, edit=True))
    flow = await configure(h, flow, {"destination": "RTD", "arrived_at": "2026-10-27T10:05:00"})
    with pytest.raises(InvalidData):
        await configure(h, flow, old)
    assert (await saved(h)).arrived_at is None


async def test_missing_reference_requires_explicit_original_plan(hass):
    h = await world(hass, world=[])
    assert (await saved(h)).plan is None
    flow = await details(h)
    assert any(str(k) == "original_planned_arrival" for k in flow["data_schema"].schema)
    flow = await configure(
        h,
        flow,
        {
            "destination": "RTD",
            "arrived_at": "2026-10-27T10:00:00",
            "original_planned_arrival": "2026-10-27T09:00:00",
        },
    )
    result = await configure(h, flow, confirmation(flow))
    assert result["reason"] == "arrival_saved"
    record = await saved(h)
    assert record.plan.source == "user" and record.delay_minutes == 60


async def test_archived_record_still_supports_correction(hass):
    h = await world(hass)
    await service(h)
    original = await saved(h)
    await h.host.run(lambda rt: seed_state(rt, occurrences={}))
    await h.host.run(save_state)
    flow = await review(h, at="2026-10-27T09:40:00")
    result = await configure(h, flow, confirmation(flow))
    assert result["reason"] == "arrival_saved"
    corrected = await saved(h)
    assert corrected.revision == 2 and corrected.plan == original.plan
    assert corrected.arrived_at == original.arrived_at - timedelta(minutes=20)


async def test_failed_strict_save_stays_in_review_and_does_not_activate(hass, monkeypatch):
    h = await world(hass)
    flow = await review(h)
    before = state_path(hass, h.entry).read_bytes()

    def fail(*args):
        raise StorageError("disk full")

    monkeypatch.setattr(JsonFileStore, "save", fail)
    refused = await configure(h, flow, confirmation(flow))
    assert refused["errors"] == {"base": "arrival_refused"}
    assert (await saved(h)).arrived_at is None
    assert state_path(hass, h.entry).read_bytes() == before


def test_picker_rejects_missing_hour_and_requires_explicit_repeated_hour_offset():
    with pytest.raises(vol.Invalid):
        local_timestamp("2026-03-29T02:30:00")
    with pytest.raises(vol.Invalid):
        local_timestamp("2026-10-25T02:30:00")
    first = local_timestamp("2026-10-25T02:30:00", "7200.0")
    second = local_timestamp("2026-10-25T02:30:00", "3600.0")
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)


async def test_repeated_hour_picker_requires_choice_before_review_and_saves_nothing(hass):
    h = await world(hass)
    flow = await configure(
        h,
        await details(h),
        {
            "destination": "RTD",
            "arrived_at": "2026-10-25T02:30:00",
        },
    )
    assert flow["errors"] == {"base": "arrival_time"}
    fields = {str(k): v for k, v in flow["data_schema"].schema.items()}
    choices = fields["arrival_offset"].config["options"]
    assert {o["value"] for o in choices} == {"7200.0", "3600.0"}
    flow = await configure(
        h,
        flow,
        {
            "destination": "RTD",
            "arrived_at": "2026-10-25T02:30:00",
            "arrival_offset": "3600.0",
        },
    )
    assert flow["step_id"] == "arrival_review"
    assert "02:30 CET" in flow["description_placeholders"]["arrival"]
    assert (await saved(h)).arrived_at is None


def test_picker_aware_input_is_reviewed_as_canonical_amsterdam_time():
    value = local_timestamp("2026-10-27T09:00:00+00:00")
    assert value.hour == 10 and value.utcoffset() == timedelta(hours=1)


def test_compensation_preference_roundtrips_without_losing_other_alerts(tmp_path):
    import yaml
    from test_profile_editor import KNOWN, TODAY, VALUES, document

    from custom_components.train_commute_companion.profile_editor import edit_profile

    doc = document(tmp_path)
    text = edit_profile(doc, "p1", {**VALUES, "compensation_reminder": True}, KNOWN, TODAY)
    raw = yaml.safe_load(text)
    assert raw["profiles"][0]["alerts"]["compensation_reminder"] is True
    doc.path.write_text(text)
    doc = document(tmp_path, text)
    text = edit_profile(doc, "p1", {**VALUES, "compensation_reminder": False}, KNOWN, TODAY)
    assert yaml.safe_load(text)["profiles"][0]["alerts"]["compensation_reminder"] is False


def test_arrival_and_navigation_preferences_save_independently(tmp_path):
    import yaml
    from test_profile_editor import KNOWN, TODAY, VALUES, document

    from custom_components.train_commute_companion.profile_editor import edit_profile

    doc = document(tmp_path)
    values = {**VALUES, "arrival_prompt": False, "navigate": True}
    text = edit_profile(doc, "p1", values, KNOWN, TODAY)
    alerts = yaml.safe_load(text)["profiles"][0]["alerts"]
    assert alerts["arrival_prompt"] is False and alerts.get("navigate", True) is True
    doc.path.write_text(text)
    doc = document(tmp_path, text)
    values.update(arrival_prompt=True, navigate=False)
    text = edit_profile(doc, "p1", values, KNOWN, TODAY)
    alerts = yaml.safe_load(text)["profiles"][0]["alerts"]
    assert alerts["arrival_prompt"] is True and alerts["navigate"] is False
