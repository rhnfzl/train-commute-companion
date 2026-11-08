from types import SimpleNamespace

import pytest

from custom_components.train_commute_companion.entity import sync_entities


def test_failed_entity_construction_remains_pending_for_next_reconcile(hass):
    listeners = []
    added = []
    factories = {}
    failed = False

    def listen(callback):
        listeners.append(callback)
        return lambda: None

    def make():
        nonlocal failed
        if not failed:
            failed = True
            raise ValueError("construction failed")
        return "entity"

    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry"),
        hass=hass,
        data=SimpleNamespace(started=False, directions=()),
        async_add_listener=listen,
    )
    sync_entities(coordinator, "sensor", added.extend, lambda view: factories)
    factories["new"] = make
    with pytest.raises(ValueError, match="construction failed"):
        listeners[0]()
    listeners[0]()
    assert added == ["entity"]
