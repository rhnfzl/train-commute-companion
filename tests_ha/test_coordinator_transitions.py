from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.train_commute_companion.coordinator import TccCoordinator


def test_reauth_latch_commits_only_when_flow_start_is_accepted(hass, monkeypatch):
    entry = MockConfigEntry(domain="train_commute_companion", options={"notify_service": "phone"})
    entry.add_to_hass(hass)
    coordinator = TccCoordinator(hass, entry, SimpleNamespace(alive=True))
    calls = []

    def start_reauth(self, hass):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("flow queue unavailable")

    monkeypatch.setattr(type(entry), "async_start_reauth", start_reauth)
    view = SimpleNamespace(
        started=True, directions=(), key_rejected=True, refusal=None, moved=(), undelivered=0
    )
    with pytest.raises(RuntimeError, match="flow queue unavailable"):
        coordinator._sync_issues(view)
    coordinator._sync_issues(view)
    assert calls == [True, True]
    coordinator._sync_issues(view)
    assert calls == [True, True]
