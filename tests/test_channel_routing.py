import json
from datetime import date

import pytest
from runtime_rig import PROFILE
from test_disruption_receipts import CLEAR, EMAIL, OTHER, START, World

from train_commute_companion.model import AlertSettings, EventKind
from train_commute_companion.notification_policy import channel_enabled, channels_for
from train_commute_companion.profile import load_config, parse_profiles
from train_commute_companion.serde import decode, dumps, encode, loads
from train_commute_companion.webhook import url_id


def profile(channels, disabled=()):
    alerts = json.dumps({"channels": channels, "disabled_categories": list(disabled)})
    return PROFILE.replace('    name: "NAME"', f'    name: "office_a"\n    alerts: {alerts}')


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"wrong": ["primary"]},
        {"delays": []},
        {"delays": "email"},
        {"delays": ["mail"]},
        {"delays": [True]},
    ],
)
def test_invalid_channel_syntax_is_refused(value):
    _, errors = parse_profiles(profile(value), None)
    assert any("alerts.channels" in error.path for error in errors)


def test_channels_defaults_disabled_precedence_reliability_and_roundtrip():
    settings = AlertSettings(
        channels={"delays": frozenset({"email"})}, disabled_categories=frozenset({"delays"})
    )
    assert not channels_for(settings, EventKind.DELAYED)
    assert channels_for(settings, EventKind.CANCELLED) == {"primary", "email", "webhook"}
    assert channel_enabled(settings, EventKind.PROFILE_REFUSED, "primary")
    assert channel_enabled(settings, EventKind.STORAGE_FAILED, "email")
    assert loads(AlertSettings, dumps(settings)) == settings
    raw = encode(settings)
    raw.pop("channels")
    assert decode(AlertSettings, raw).channels == {}


def test_host_availability_refuses_change_and_keeps_previous_config():
    old, errors = load_config(profile({"delays": ["primary"]}), None, None, date(2026, 10, 27))
    assert old is not None and not errors
    candidate, errors = load_config(
        profile({"delays": ["email"]}),
        None,
        old,
        date(2026, 10, 27),
        available_channels=frozenset({"primary"}),
    )
    assert candidate is None and errors
    assert "alerts.channels.delays" in errors[0].path
    parsed, errors = parse_profiles(profile({"delays": ["email"]}), None)
    assert parsed and not errors  # portable CLI syntax check has no host capability context
    _, errors = parse_profiles(
        profile({"delays": ["email"]}, ["delays"]), None, available_channels=frozenset({"primary"})
    )
    assert errors


async def test_email_only_disruptions_skip_primary_but_keep_start_and_clear(tmp_path):
    world = World(tmp_path)
    world.rig.write(text=profile({"disruptions": ["email"]}))
    await world.start()
    assert START not in [event.kind for event in world.rig.notifier.attempts]
    assert world.kinds() == [START]
    await world.poll(False)
    assert world.kinds() == [START, CLEAR]
    assert CLEAR not in [event.kind for event in world.rig.notifier.attempts]


@pytest.mark.parametrize("restart", [False, True])
async def test_initial_unknown_primary_holds_its_slot_while_auxiliary_can_send(tmp_path, restart):
    world = World(tmp_path)
    world.primary = None
    world.rig.write(text=profile({"disruptions": ["primary", "email"]}))
    await world.start()
    assert world.kinds() == [START]
    assert START not in [event.kind for event in world.rig.notifier.attempts]
    owed = [o for o in world.rig.rt.state.obligations if o.event.kind is START]
    assert len(owed) == 1 and owed[0].primary_target_pending
    assert url_id(EMAIL) in owed[0].target_ids
    world.primary = "first-bound-phone"
    if restart:
        world.build()
        await world.start()
    else:
        await world.rig.rt.watchdog_once()
        await world.rig.rt.webhooks_idle()
    assert [e.kind for e in world.rig.notifier.attempts].count(START) == 1
    assert world.kinds() == [START]


async def test_routing_edit_removes_pending_primary_without_adding_new_email(tmp_path):
    world = World(tmp_path)
    world.rig.write(text=profile({"disruptions": ["primary"]}))
    world.rig.notifier.fails = lambda event: event.kind is START
    await world.start()
    assert world.kinds() == []
    owed = [o for o in world.rig.rt.state.obligations if o.event.kind is START]
    assert owed[0].target_ids == frozenset({world.primary})
    world.rig.write(text=profile({"disruptions": ["email"]}))
    await world.rig.rt.reload()
    await world.rig.rt.webhooks_idle()
    assert world.kinds() == []
    assert not any(o.event.kind is START for o in world.rig.rt.state.obligations)


async def test_pin_is_written_before_primary_network_request_and_recipient_change(tmp_path):
    world = World(tmp_path)
    send = world.rig.notifier.send
    observed = []

    async def phone(event):
        if event.kind is START:
            pending = [o for o in world.rig.store.load().obligations if o.event.kind is START]
            assert len(pending) == 1
            observed.append(pending[0])
        return await send(event)

    world.rig.notifier.send = phone
    world.rig.notifier.fails = lambda event: event.kind is START
    await world.start()
    assert observed and observed[0].target_ids == {world.primary, url_id(EMAIL), url_id(OTHER)}
    world.primary = "replacement-phone"
    world.build()
    await world.start()
    assert len(observed) == 1  # original fact cannot transfer to the replacement phone


async def test_missing_explicit_transport_preserves_stored_pending_and_reports_refusal(tmp_path):
    import runtime_seam

    from train_commute_companion.runtime import Runtime

    world = World(tmp_path)
    world.rig.write(text=profile({"disruptions": ["email"]}))
    world.port.fail = {EMAIL, OTHER}
    await world.start()
    assert runtime_seam.owed_copies(world.rig.rt)
    rig = world.rig
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        primary_destination=lambda: world.primary,
    )
    await rig.rt.start()
    assert rig.rt.config.profiles[0].alerts.channels["disruptions"] == {"email"}
    assert rig.rt.view().refusal and "email" in rig.rt.view().refusal
    assert runtime_seam.owed_copies(rig.rt)
    assert any(e.kind is EventKind.PROFILE_REFUSED for e in rig.notifier.attempts)
    assert START not in [e.kind for e in rig.notifier.attempts]
    assert rig.store.load().webhook_copies


async def test_failed_pin_write_does_not_transfer_retry_to_replacement_primary(tmp_path):
    from dataclasses import replace

    import runtime_seam

    from train_commute_companion.model import AlertFacts, Obligation, Severity

    world = World(tmp_path)
    await world.start()
    rig = world.rig
    original = next(event for event in rig.notifier.attempts if event.profile_id == "p1")
    event = replace(
        original,
        kind=EventKind.CANCELLED,
        severity=Severity.LOUD,
        key="private-cancellation",
        facts=AlertFacts(),
    )
    owed = Obligation(
        "persisted-unpinned",
        event,
        rig.occ().key,
        None,
        rig.occ().route_revision,
        None,
        event.key,
        None,
        0,
    )
    runtime_seam.seed_state(rig.rt, obligations=(owed,))
    runtime_seam.save_state(rig.rt)
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.store.fail = True
    await rig.rt.watchdog_once()
    await rig.rt.webhooks_idle()
    assert [e.kind for e in rig.notifier.attempts].count(EventKind.CANCELLED) == 0
    assert not any(e.kind is EventKind.CANCELLED for e in world.records[EMAIL])
    world.primary = "replacement-phone"
    await rig.rt.watchdog_once()
    await rig.rt.webhooks_idle()
    assert [e.kind for e in rig.notifier.attempts].count(EventKind.CANCELLED) == 0
    assert any(e.kind is EventKind.STORAGE_FAILED for e in rig.notifier.attempts)
    rig.store.fail = False
    await rig.rt.watchdog_once()
    assert not any(o.id == owed.id for o in rig.store.load().obligations)


async def test_restoring_transport_resumes_original_stored_email_targets(tmp_path):
    from train_commute_companion.runtime import Runtime

    world = World(tmp_path)
    world.rig.write(text=profile({"disruptions": ["email"]}))
    world.port.fail = {EMAIL, OTHER}
    await world.start()
    rig = world.rig
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        primary_destination=lambda: world.primary,
    )
    await rig.rt.start()
    assert rig.store.load().webhook_copies
    world.build()
    await world.start()
    rig.clock.jump(61 / 60)
    await rig.rt.watchdog_once()
    await rig.rt.webhooks_idle()
    assert world.kinds() == [START, START]
    assert world.notice().receipts[url_id(EMAIL)].needs_clear
    assert START not in [e.kind for e in rig.notifier.attempts]


@pytest.mark.parametrize(
    "kind,category",
    [
        (EventKind.CANCELLED, "journey"),
        (EventKind.DELAYED, "delays"),
        (EventKind.TRANSFER_BROKEN, "transfers"),
        (EventKind.PLATFORM_CHANGE, "platforms"),
        (EventKind.WORKS_AHEAD, "planned_works"),
    ],
)
def test_each_selected_category_routes_to_its_explicit_channels(kind, category):
    settings = AlertSettings(channels={category: frozenset({"email"})})
    assert channels_for(settings, kind) == {"email"}
    assert not channel_enabled(settings, kind, "primary")
    assert channel_enabled(settings, kind, "email")


def test_lookahead_recovery_uses_planned_works_channel_policy():
    settings = AlertSettings(
        channels={"planned_works": frozenset({"email"}), "journey": frozenset({"primary"})}
    )
    assert channels_for(settings, EventKind.RESTORED, look_ahead=True) == {"email"}
    assert channels_for(settings, EventKind.RESTORED) == {"primary"}


@pytest.mark.parametrize("binding", [False, True])
async def test_failed_initial_pin_or_binding_never_discloses_before_crash_and_restart(
    tmp_path, binding
):
    from dataclasses import replace

    import runtime_seam

    from train_commute_companion.model import AlertFacts, Obligation, Severity

    world = World(tmp_path)
    if binding:
        world.primary = None
    await world.start()
    rig = world.rig
    if binding:
        kind, key = (
            START,
            next(o.event.key for o in rig.rt.state.obligations if o.event.kind is START),
        )
    else:
        kind, key = EventKind.CANCELLED, "private-cancellation-before-crash"
        original = next(e for e in rig.notifier.attempts if e.profile_id == "p1")
        event = replace(original, kind=kind, severity=Severity.LOUD, key=key, facts=AlertFacts())
        owed = Obligation(
            "persisted-unpinned",
            event,
            rig.occ().key,
            None,
            rig.occ().route_revision,
            None,
            key,
            None,
            0,
        )
        runtime_seam.seed_state(rig.rt, obligations=(owed,))
        runtime_seam.save_state(rig.rt)
    reached = []
    send = rig.notifier.send

    async def phone(event):
        if event.key == key:
            reached.append(world.primary)
            saved = next(o for o in rig.store.load().obligations if o.event.key == key)
            assert world.primary in saved.target_ids and not saved.primary_target_pending
        return await send(event)

    rig.notifier.send = phone
    world.primary = "opaque-phone-one"
    rig.store.fail = True
    await rig.rt.watchdog_once()
    await rig.rt.webhooks_idle()
    assert reached == []
    if not binding:
        assert not any(e.key == key for e in world.records[EMAIL])
        assert not any(e.key == key for e in world.records[OTHER])
    # Lose all in-memory writer operations, then restart with a different recipient.
    rig.store.fail = False
    world.primary = "replacement-phone"
    world.build()
    await world.start()
    assert reached == ["replacement-phone"]
    assert [e.key for e in rig.notifier.attempts].count(key) == 1
