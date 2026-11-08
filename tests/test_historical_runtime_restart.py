"""Run real pre-refactor saved journeys through startup, migration and another restart."""

import json
from pathlib import Path

import pytest
from runtime_rig import PROFILE, Rig

from train_commute_companion.runtime import Runtime
from train_commute_companion.serde import encode


@pytest.mark.parametrize("version", [1, 2])
async def test_pre_refactor_runtime_state_resumes_without_rebinding_receipts(tmp_path, version):
    fixture = Path(__file__).parent / "data/state/pre-explicit-runtime-v2.json"
    original = json.loads(fixture.read_text())["state"]
    expected_records = original["journey_records"]
    old_alerts = {o["event"]["key"] for o in original["obligations"]}
    if version == 1:
        for state in original["occurrences"].values():
            state.pop("baseline_frozen")
            state.pop("baseline_signature")
            if state["advice"] is not None:
                state["advice"].pop("delivered")
    rig = Rig(tmp_path, now="08:05")
    rig.write(text=PROFILE[: PROFILE.index('  - id: "p2"')])
    rig.store.text = json.dumps(original)
    rig.store.version = version
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        primary_destination=lambda: "phone:new",
    )
    await rig.rt.start()
    assert rig.rt.started and rig.rt.store_ok
    assert encode(rig.rt.state.journey_records) == expected_records
    assert all(state.baseline_frozen for state in rig.rt.state.occurrences.values())
    assert not old_alerts.intersection(event.key for event in rig.notifier.attempts)
    assert rig.store.version == 2
    await rig.restart()
    assert rig.rt.started and rig.rt.store_ok
    assert encode(rig.rt.state.journey_records) == expected_records
    assert not old_alerts.intersection(event.key for event in rig.notifier.attempts)
