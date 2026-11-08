import json
from dataclasses import replace

import pytest
from lifecycle_fixtures import PROFILES, at, ctx
from test_delivery import Flaky, View, ob

from train_commute_companion.delivery import deliver, post_copy, revalidate
from train_commute_companion.ledger import LiveLedger, PollMemory
from train_commute_companion.model import AlertSettings, Config, EventKind, Severity, WebhookCopy
from train_commute_companion.notification_policy import CATEGORIES, category_for, enabled
from train_commute_companion.profile import parse_profiles
from train_commute_companion.serde import decode, dumps, loads
from train_commute_companion.store import PersistedState


def test_defaults_roundtrip_and_old_stored_settings():
    assert AlertSettings().disabled_categories == frozenset()
    settings = replace(AlertSettings(), disabled_categories=frozenset({"delays", "crowding"}))
    assert loads(AlertSettings, dumps(settings)) == settings
    assert decode(AlertSettings, {}).disabled_categories == frozenset()


@pytest.mark.parametrize(
    "value", ["delays", None, ["unknown"], [True], [1], {}, ["delays", "Delays"]]
)
def test_bad_disabled_categories_are_refused(value):
    text = PROFILES.replace("UT_MIN", "4").replace("TRANSFERS", "[UT]")
    profiles, errors = parse_profiles(
        text + "    alerts: {disabled_categories: " + json.dumps(value) + "}\n", None
    )
    assert not profiles and any("disabled_categories" in e.path for e in errors)


def test_valid_categories_parse_and_category_coverage():
    text = PROFILES.replace("UT_MIN", "4").replace("TRANSFERS", "[UT]")
    profiles, errors = parse_profiles(
        text + "    alerts: {disabled_categories: [delays, crowding]}\n", None
    )
    assert not errors and profiles[0].alerts.disabled_categories == {"delays", "crowding"}
    assert {category_for(k) for k in EventKind} - {None} == set(CATEGORIES)


@pytest.mark.parametrize("kind", list(EventKind))
def test_disabled_categories_and_protected_reliability_events(kind):
    settings = replace(AlertSettings(), disabled_categories=frozenset(CATEGORIES))
    assert enabled(settings, kind) == (category_for(kind) is None)


def test_disable_overrides_journey_warning_and_quiet_hold_but_preserves_app_warning():
    ledger = View(enabled=False, quiet=True)
    assert revalidate(ob(EventKind.CANCELLED), ledger, at("08:00")) == "drop"
    assert revalidate(ob(EventKind.DELAYED, Severity.SILENT), ledger, at("08:00")) == "drop"
    assert revalidate(ob(EventKind.KEY_REJECTED), ledger, at("08:00")) == "send"


async def test_suppression_prevents_primary_and_auxiliary_requests():
    obligation = ob(EventKind.CANCELLED)
    notifier = Flaky([True])
    assert await deliver(obligation, View(enabled=False), notifier, at("08:00")) == (None, False)
    assert notifier.sent == 0

    class Port:
        urls = ("https://example.test/hook",)

        def due(self, urls):
            raise AssertionError("disabled notification reached an auxiliary destination")

    assert (
        await post_copy(WebhookCopy("copy", obligation), View(enabled=False), Port(), at("08:00"))
        is None
    )


def test_live_ledger_reads_current_preferences_for_held_obligations():
    profile = ctx(at("07:50")).profile
    config = Config(1, (profile,))
    stored = PersistedState(config, {1: config}, {}, {}, {}, (), False)
    ledger = LiveLedger(lambda: stored, lambda: stored, PollMemory(), lambda: at("08:00"))
    obligation = ob(EventKind.CANCELLED)
    obligation = replace(obligation, event=replace(obligation.event, profile_id=profile.id))
    assert ledger.notification_enabled(obligation)
    profile = replace(
        profile, alerts=replace(profile.alerts, disabled_categories=frozenset({"journey"}))
    )
    stored = replace(stored, config=Config(2, (profile,)))
    assert not ledger.notification_enabled(obligation)
    assert revalidate(obligation, ledger, at("08:00")) == "drop"


def test_lookahead_restoration_follows_planned_works_preference():
    settings = replace(AlertSettings(), disabled_categories=frozenset({"planned_works"}))
    assert not enabled(settings, EventKind.RESTORED, look_ahead=True)
    assert enabled(settings, EventKind.RESTORED)


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.KEY_REJECTED,
        EventKind.PROFILE_REFUSED,
        EventKind.STORAGE_FAILED,
        EventKind.MISSING,
        EventKind.CANNOT_SEE_TRAIN,
        EventKind.CAN_SEE_TRAIN_AGAIN,
        EventKind.STOPPED_FOLLOWING,
        EventKind.OFFLINE,
        EventKind.DATA_UNCLEAR,
        EventKind.CHOOSE_REFUSED,
    ],
)
def test_system_data_reliability_and_command_feedback_are_always_enabled(kind):
    assert category_for(kind) is None
    assert enabled(replace(AlertSettings(), disabled_categories=frozenset(CATEGORIES)), kind)
