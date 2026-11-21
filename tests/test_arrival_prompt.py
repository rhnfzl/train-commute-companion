from datetime import timedelta

import pytest
from builders import make_connection, make_leg
from runtime_rig import DAY, PROFILE, Rig, at
from test_travel_plans import save

from train_commute_companion.compensation_state import JourneyRecord
from train_commute_companion.messages import Refused
from train_commute_companion.model import Direction, EventKind, Provenance, Severity
from train_commute_companion.notify import NotificationPaused
from train_commute_companion.profile import load_config
from train_commute_companion.runtime import Runtime

BASE = PROFILE[: PROFILE.index('  - id: "p2"')]
TARGET = "test-phone"
PROMPT = EventKind.ARRIVAL_PROMPT


def write_profile(rig, alerts="compensation_reminder: true"):
    rig.write(text=f"{BASE}    alerts: {{{alerts}}}\n")


async def start(rig):
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        primary_destination=lambda: TARGET,
    )
    await rig.rt.start()


async def world(tmp_path, alerts="compensation_reminder: true"):
    rig = Rig(tmp_path, now="08:05")
    write_profile(rig, alerts)
    await start(rig)
    return rig


def set_late(rig, minutes=30, provenance=Provenance.REPORTED):
    rig.provider.world = [
        make_connection(
            [
                make_leg("AAA", "UT", "08:12", "08:40", number="1234"),
                make_leg(
                    "UT",
                    "RTD",
                    "08:50",
                    "09:24",
                    number="5678",
                    delay_arr=minutes,
                    provenance=provenance,
                ),
            ]
        )
    ]


async def observe_arrival(rig):
    for when in ("08:51", "09:40", "09:59", "10:03"):
        rig.clock.set(when)
        await rig.rt.poll_once()


def saved(rig):
    return next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)


def attempts(rig):
    return [event for event in rig.notifier.attempts if event.kind is PROMPT]


async def flexible_trip(tmp_path):
    rig = Rig(tmp_path, now="07:00")
    write_profile(rig)
    await start(rig)
    change = {
        "action": "create",
        "plan": {
            "name": "Offsite visit",
            "profile_id": "p1",
            "origin": "AAA",
            "destination": "RTD",
            "start_date": DAY.isoformat(),
            "outbound": {"mode": "exact", "departure": "08:12"},
        },
        "conflict_policy": "replace",
    }
    pid = save(rig.rt, change, "arrival-preferences")["plan_id"]
    return rig, pid


@pytest.mark.parametrize("active", [False, True])
async def test_flexible_trip_obeys_source_arrival_opt_out(tmp_path, active):
    rig, pid = await flexible_trip(tmp_path)
    if active:
        await rig.run_until("08:05")
    write_profile(rig, "compensation_reminder: false, navigate: false, arrival_prompt: false")
    assert await rig.rt.reload() is None
    await rig.run_until("08:08")
    profile = next(p for p in rig.rt.view().profiles if p.id == pid)
    assert not profile.alerts.navigate
    set_late(rig)
    await observe_arrival(rig)
    assert not attempts(rig)


async def test_flexible_trip_tap_and_queued_details_obey_source_opt_out(tmp_path):
    rig, pid = await flexible_trip(tmp_path)
    await rig.run_until("08:05")
    set_late(rig)
    await observe_arrival(rig)
    (question,) = attempts(rig)
    assert question.profile_id == pid
    write_profile(
        rig, "compensation_reminder: true, arrival_prompt: false, compensation_details: false"
    )
    assert await rig.rt.reload() is None
    assert isinstance(
        await rig.rt.answer_arrival_prompt(question.facts.prompt_id, "yes", TARGET), Refused
    )
    record = next(r for r in rig.rt.arrival_records() if r.key.profile_id == pid)
    await rig.rt.confirm_arrival(
        record.key, "RTD", at("10:00"), expected_revision=record.revision, action_id="manual"
    )
    (reminder,) = [e for e in rig.notifier.attempts if e.kind is EventKind.COMPENSATION_REMINDER]
    assert "Planned trains:" not in reminder.text


async def test_arrival_question_survives_repeated_failed_saves(tmp_path):
    rig = await world(tmp_path)
    set_late(rig)
    for when in ("08:51", "09:40", "09:59"):
        rig.clock.set(when)
        await rig.rt.poll_once()
    rig.store.fail = True
    for when in ("10:03", "10:05"):
        rig.clock.set(when)
        await rig.rt.poll_once()
    assert not attempts(rig)
    rig.store.fail = False
    rig.clock.set("10:07")
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    (question,) = attempts(rig)
    assert saved(rig).prompt.delivered
    assert question.facts.prompt_id == saved(rig).prompt.id


async def test_accepted_ns_arrival_needs_bound_tap_and_survives_restart(tmp_path):
    rig = await world(tmp_path)
    set_late(rig)
    await observe_arrival(rig)
    [event] = attempts(rig)
    assert event.severity is Severity.SILENT
    assert "2026-10-27T09:54+01:00" in event.text
    record = saved(rig)
    assert record.arrived_at is None
    assert record.prompt.delivered
    nonce = record.prompt.id
    await start(rig)
    await rig.rt.watchdog_once()
    assert len(attempts(rig)) == 1
    assert isinstance(await rig.rt.answer_arrival_prompt(nonce, "yes", "wrong"), Refused)
    rig.store.fail = True
    assert isinstance(await rig.rt.answer_arrival_prompt(nonce, "yes", TARGET), Refused)
    rig.store.fail = False
    assert saved(rig).arrived_at is None
    outcome = await rig.rt.answer_arrival_prompt(nonce, "yes", TARGET)
    assert isinstance(outcome, JourneyRecord)
    assert outcome.arrived_at == at("09:54") and outcome.arrival_source == "user"
    assert outcome.reminder_created and outcome.delay_minutes == 30
    (reminder,) = [
        event for event in rig.notifier.attempts if event.kind is EventKind.COMPENSATION_REMINDER
    ]
    assert "Travel date: 2026-10-27" in reminder.text
    assert "Planned origin: AAA\nDestination: RTD\nPlanned trains: 1234, 5678" in reminder.text
    assert "NS-reported train arrival: 2026-10-27T09:54+01:00" in reminder.text
    assert "Your confirmed arrival: 2026-10-27T09:54+01:00" in reminder.text
    assert isinstance(await rig.rt.answer_arrival_prompt(nonce, "yes", TARGET), Refused)
    assert saved(rig).revision == 1


async def test_queued_reminder_obeys_journey_details_preference_on_retry(tmp_path):
    rig = await world(tmp_path)
    set_late(rig)
    await observe_arrival(rig)
    record = saved(rig)
    rig.notifier.fails = lambda event: event.kind is EventKind.COMPENSATION_REMINDER
    await rig.rt.confirm_arrival(
        record.key, "RTD", at("10:00"), expected_revision=0, action_id="manual"
    )
    (first,) = [
        event for event in rig.notifier.attempts if event.kind is EventKind.COMPENSATION_REMINDER
    ]
    assert "NS-reported train arrival: 2026-10-27T09:54+01:00" in first.text
    assert "Your confirmed arrival: 2026-10-27T10:00+01:00" in first.text
    rig.notifier.fails = lambda _: False
    write_profile(rig, "compensation_reminder: true, compensation_details: false")
    await rig.rt.reload()
    await rig.rt.watchdog_once()
    (sent,) = [
        event for event in rig.rt.events_log if event.kind is EventKind.COMPENSATION_REMINDER
    ]
    assert "36 minutes after" in sent.text
    assert "Journey details" not in sent.text


@pytest.mark.parametrize("prompt", ["true", "false"])
async def test_reminder_omits_ns_arrival_after_a_conflicting_report(tmp_path, prompt):
    rig = await world(tmp_path, f"compensation_reminder: true, arrival_prompt: {prompt}")
    set_late(rig)
    await observe_arrival(rig)
    assert saved(rig).reported_train_arrival == at("09:54")

    set_late(rig, minutes=25)
    rig.clock.set("10:06")
    await rig.rt.poll_once()
    rig.clock.set("10:16")
    await rig.rt.watchdog_once()
    await rig.rt.confirm_arrival(
        saved(rig).key, "RTD", at("10:00"), expected_revision=0, action_id="manual"
    )
    (sent,) = [
        event for event in rig.rt.events_log if event.kind is EventKind.COMPENSATION_REMINDER
    ]
    assert "NS-reported train arrival" not in sent.text
    assert "Your confirmed arrival: 2026-10-27T10:00+01:00" in sent.text


@pytest.mark.parametrize(
    ("alerts", "minutes", "provenance"),
    [
        ("", 30, Provenance.REPORTED),
        ("compensation_reminder: true, arrival_prompt: false", 30, Provenance.REPORTED),
        ("compensation_reminder: true, disabled_categories: [routine]", 30, Provenance.REPORTED),
        ("compensation_reminder: true", 29, Provenance.REPORTED),
        ("compensation_reminder: true", 30, Provenance.PLANNED),
    ],
)
async def test_arrival_prompt_requires_permission_and_reported_delay(
    tmp_path, alerts, minutes, provenance
):
    rig = await world(tmp_path, alerts)
    set_late(rig, minutes, provenance)
    await observe_arrival(rig)
    assert not attempts(rig)
    assert saved(rig).arrived_at is None


async def test_uncertain_delivery_never_repeats_or_authorizes_a_tap(tmp_path):
    rig = await world(tmp_path)
    rig.notifier.fails = lambda event: event.kind is PROMPT
    set_late(rig)
    await observe_arrival(rig)
    nonce = saved(rig).prompt.id
    await rig.rt.watchdog_once()
    await start(rig)
    await rig.rt.watchdog_once()
    assert len(attempts(rig)) == 1
    assert isinstance(await rig.rt.answer_arrival_prompt(nonce, "yes", TARGET), Refused)


@pytest.mark.parametrize(
    "invalidate", ["settings", "manual", "expiry", "correction", "earlier", "other"]
)
async def test_arrival_prompt_becomes_stale(tmp_path, invalidate):
    rig = await world(tmp_path)
    set_late(rig)
    await observe_arrival(rig)
    record = saved(rig)
    if invalidate == "settings":
        write_profile(rig, "compensation_reminder: false")
        assert await rig.rt.reload() is None
        write_profile(rig)
        assert await rig.rt.reload() is None
    elif invalidate == "manual":
        await rig.rt.confirm_arrival(
            record.key, record.destination, at("09:58"), expected_revision=0, action_id="manual"
        )
    elif invalidate == "expiry":
        rig.clock.t = record.prompt.at + timedelta(hours=2)
    elif invalidate in ("correction", "earlier"):
        set_late(rig, 35 if invalidate == "correction" else 25)
        rig.clock.set("10:06")
        await rig.rt.poll_once()
    else:
        result = await rig.rt.answer_arrival_prompt(record.prompt.id, "other", TARGET)
        assert isinstance(result, JourneyRecord) and result.arrived_at is None
    assert isinstance(await rig.rt.answer_arrival_prompt(record.prompt.id, "yes", TARGET), Refused)
    assert len(attempts(rig)) == 1
    assert saved(rig).arrived_at == (at("09:58") if invalidate == "manual" else None)


async def test_paused_prompt_is_rechecked_after_restart_and_opt_out(tmp_path):
    rig = await world(tmp_path)
    set_late(rig)

    def paused(event):
        if event.kind is PROMPT:
            raise NotificationPaused
        return False

    rig.notifier.fails = paused
    await observe_arrival(rig)
    assert any(o.event.kind is PROMPT for o in rig.rt.state.obligations)
    write_profile(rig, "compensation_reminder: true, arrival_prompt: false")
    rig.notifier.fails = lambda event: False
    await start(rig)
    await rig.rt.watchdog_once()
    assert len(attempts(rig)) == 1
    assert not any(o.event.kind is PROMPT for o in rig.rt.state.obligations)


@pytest.mark.parametrize("field", ["arrival_prompt", "navigate"])
def test_feature_preferences_require_booleans(field):
    config, errors = load_config(BASE, None, None, DAY)
    assert not errors and getattr(config.profiles[0].alerts, field)
    assert not config.profiles[0].alerts.compensation_reminder
    config, errors = load_config(f"{BASE}    alerts: {{{field}: 'true'}}\n", None, None, DAY)
    assert config is None and errors
