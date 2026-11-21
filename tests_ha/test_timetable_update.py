from dataclasses import replace
from threading import get_ident
from types import SimpleNamespace

import pytest
import yaml
from ha_rig import NOTIFY, PROFILE, profiles_path, setup_tcc
from homeassistant.core import Context, EventOrigin
from lookahead_fixtures import NEXT_TUE, OUT, TUE, confirmed
from runtime_seam import seed_state

from custom_components.train_commute_companion.notifier import MOBILE_APP, TIMETABLE_PREFIX
from custom_components.train_commute_companion.services import MOBILE_ACTION
from train_commute_companion.model import AlertEvent, EventKind, Severity, direction_key
from train_commute_companion.state import BaselineHistory

pytestmark = pytest.mark.usefixtures("tcc_freezer")


async def test_mobile_timetable_update_keeps_the_old_train_until_its_change_date(hass):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["outbound"]["window"] = ["07:12", "10:12"]
    h = await setup_tcc(hass, yaml.safe_dump(raw))
    path = profiles_path(hass)
    before = path.read_bytes()
    hass.data[MOBILE_APP] = {
        "config_entries": {
            f"hook-{NOTIFY}": SimpleNamespace(data={"user_id": "owner", "device_id": "phone"})
        }
    }
    key = direction_key("p1", OUT)
    baseline = BaselineHistory(
        (
            confirmed(),
            replace(confirmed(2, NEXT_TUE, l1=("08:18", "08:44")), update_nonce="fixture-secret"),
        )
    )

    async def deliver(rt):
        rt.known_stations = frozenset({"AAA", "UT", "RTD"})
        seed_state(rt, baselines={**rt.state.baselines, key: baseline})
        event = AlertEvent(
            "p1",
            "office_a",
            OUT,
            NEXT_TUE,
            None,
            EventKind.TIMETABLE_ADOPTED,
            Severity.SILENT,
            None,
            "NS moved your 08:12 to 08:18.",
            "adopt:2",
            rt.now(),
        )
        obligation = replace(
            rt._obligation(event, None, rt.now()),
            look_ahead=("p1", OUT, NEXT_TUE),
            baseline_version=2,
        )
        event = rt._ledger.current(obligation, rt._primary_destination())
        assert event is not None
        assert await rt._notifier.send(event)

    await h.host.run(deliver)
    action = h.pushes[-1].data["data"]["actions"][0]["action"]
    assert action.startswith(TIMETABLE_PREFIX)

    for origin, user, device in (
        (EventOrigin.local, "owner", None),
        (EventOrigin.remote, "other", None),
        (EventOrigin.remote, "owner", "another-phone"),
    ):
        hass.bus.async_fire(
            MOBILE_ACTION, {"action": action, "device_id": device}, origin, Context(user_id=user)
        )
        await hass.async_block_till_done()
        assert path.read_bytes() == before
    edited_elsewhere = yaml.safe_load(before)
    edited_elsewhere["profiles"][0]["name"] = "Manual edit"
    path.write_text(yaml.safe_dump(edited_elsewhere))
    manual = path.read_bytes()
    hass.bus.async_fire(
        MOBILE_ACTION, {"action": action}, EventOrigin.remote, Context(user_id="owner")
    )
    await hass.async_block_till_done()
    assert path.read_bytes() == manual
    path.write_bytes(before)
    reads = []

    async def record_reload_io(rt):
        original = rt._path
        owner = get_ident()

        def read_text(*args, **kwargs):
            reads.append(get_ident() == owner)
            return original.read_text(*args, **kwargs)

        rt._path = SimpleNamespace(name=original.name, stat=original.stat, read_text=read_text)

    await h.host.run(record_reload_io)
    hass.bus.async_fire(
        MOBILE_ACTION, {"action": action}, EventOrigin.remote, Context(user_id="owner")
    )
    await hass.async_block_till_done()
    assert reads == [True]
    changed = yaml.safe_load(path.read_text())
    journey = changed["profiles"][0]["outbound"]
    assert journey["first_train"] == "08:18"
    assert journey["window"] == ["07:18", "10:18"]
    assert changed["profiles"][1:] == raw["profiles"][1:]
    saved = await h.host.run(lambda rt: rt.state.baselines[key])
    assert saved.in_force(TUE) == baseline.versions[0]
    assert saved.in_force(NEXT_TUE) == baseline.versions[1]
    unchanged = path.read_bytes()
    hass.bus.async_fire(
        MOBILE_ACTION, {"action": action}, EventOrigin.remote, Context(user_id="owner")
    )
    await hass.async_block_till_done()
    assert path.read_bytes() == unchanged
