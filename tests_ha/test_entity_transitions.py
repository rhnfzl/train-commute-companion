from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.train_commute_companion.const import DOMAIN
from custom_components.train_commute_companion.entity import sync_entities


def test_failed_entity_construction_remains_pending_for_next_reconcile(hass):
    listeners = []
    added = []
    factories = {}
    failed = False
    entity = SimpleNamespace(profile_id="p1")

    def listen(callback):
        listeners.append(callback)
        return lambda: None

    def make():
        nonlocal failed
        if not failed:
            failed = True
            raise ValueError("construction failed")
        return entity

    def add_entities(entities, *, config_subentry_id=None):
        assert config_subentry_id is None
        added.extend(entities)

    coordinator = SimpleNamespace(
        config_entry=MockConfigEntry(domain=DOMAIN),
        hass=hass,
        data=SimpleNamespace(started=False, directions=()),
        async_add_listener=listen,
    )
    sync_entities(coordinator, "sensor", add_entities, lambda view: factories)
    factories["new"] = make
    with pytest.raises(ValueError, match="construction failed"):
        listeners[0]()
    listeners[0]()
    assert added == [entity]
