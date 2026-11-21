"""Profile delay preferences change news, while journey protection stays active."""

import json
from dataclasses import replace
from datetime import time

import pytest
from builders import make_leg
from lifecycle_fixtures import L2, PROFILES, ans, at, conn, ctx, journey, occ, poll, poll_answers
from runtime_rig import L1, PROFILE, Rig, bump_mtime
from test_delivery import ob

from train_commute_companion.classify import classify_leg, current_run
from train_commute_companion.delivery import revalidate
from train_commute_companion.ledger import LiveLedger, PollMemory
from train_commute_companion.lifecycle import delayed_event, keep_stated, next_stated_delay
from train_commute_companion.model import (
    AlertSettings,
    Config,
    EventKind,
    LegKind,
    LegState,
    Severity,
    ToldDelay,
    held,
)
from train_commute_companion.news import all_good, forget_ended, news_of
from train_commute_companion.profile import parse_profiles
from train_commute_companion.serde import decode, dumps, loads
from train_commute_companion.state import mark_sent
from train_commute_companion.store import PersistedState


def context(delay_min=10, step=3, now="07:50", limit=None):
    c = ctx(at(now), transfers="[]")
    return replace(
        c,
        profile=replace(
            c.profile,
            alerts=replace(
                c.profile.alerts,
                delay_min=delay_min,
                delay_step_min=step,
                latest_acceptable_arrival=limit,
            ),
        ),
    )


def leg(minutes=0, **kwargs):
    return make_leg("AAA", "RTD", "08:12", "09:24", delay_dep=minutes, delay_arr=minutes, **kwargs)


@pytest.mark.parametrize("minutes,kind", [(7, LegKind.OK), (9, LegKind.OK), (10, LegKind.DELAYED)])
def test_threshold_changes_classification_and_merged_run(minutes, kind):
    g = leg(minutes)
    assert classify_leg(g, "RTD", delay_min=10).kind is kind
    run = current_run([ans("AAA", conn(g))], g.train, "AAA", "RTD", delay_min=10)
    assert run is not None and run[1].kind is kind


def test_profiles_do_not_share_delay_thresholds():
    s = occ(journey([leg()]))
    a = ans("AAA", conn(leg(7)))
    assert EventKind.DELAYED not in [e.kind for e in poll(s, context(10), a).events]
    assert EventKind.DELAYED in [e.kind for e in poll(s, context(3), a).events]
    assert EventKind.DELAYED not in [e.kind for e in poll(s, context(10), a).events]


@pytest.mark.parametrize(
    "now,minimum,minutes,sends",
    [
        ("07:00", 3, 4, False),
        ("07:00", 3, 5, True),
        ("07:00", 10, 7, False),
        ("07:00", 10, 9, False),
        ("07:00", 10, 10, True),
        ("07:50", 10, 9, False),
        ("07:50", 10, 10, True),
    ],
)
def test_quiet_and_active_thresholds(now, minimum, minutes, sends):
    s = occ(journey([leg()]))
    events = poll(s, context(minimum, now=now), ans("AAA", conn(leg(minutes)), when=now)).events
    assert (EventKind.DELAYED in [e.kind for e in events]) is sends


def test_repeat_step_and_recovery_use_profile_settings():
    c = context(step=5)
    s = occ(journey([leg()]))
    for minutes, sends in [
        (10, True),
        (14, False),
        (15, True),
        (11, False),
        (10, True),
        (1, False),
    ]:
        t = poll(s, c, ans("AAA", conn(leg(minutes))))
        forget_ended(t.state.alert_keys, t.events)
        events = news_of(t.state.alert_keys, t.events, c.now)
        assert (EventKind.DELAYED in [e.kind for e in events]) is sends
        if minutes == 1:
            assert EventKind.BACK_TO_NORMAL in [e.kind for e in t.events]
        s = t.state
        for event in events:
            mark_sent(s.alert_keys, event.key, event.severity)
    assert next_stated_delay(None, 9, delay_min=10, delay_step_min=5) is None
    assert next_stated_delay(10, 14, delay_min=10, delay_step_min=5) == 10
    assert next_stated_delay(10, 15, delay_min=10, delay_step_min=5) == 15
    assert next_stated_delay(10, 1, delay_min=10, delay_step_min=5) == 0


@pytest.mark.parametrize("now", ["07:00", "07:50"])
def test_cancellation_and_arrival_limit_bypass_thresholds(now):
    s = occ(journey([leg()]))
    c = context(10, step=10, now=now, limit=time(9, 24))
    events = poll(s, c, ans("AAA", conn(leg(1)), when=now)).events
    assert any(e.kind is EventKind.DELAYED and e.severity is Severity.LOUD for e in events)
    events = poll(s, c, ans("AAA", conn(leg(cancelled=True)), when=now)).events
    assert any(e.kind is EventKind.CANCELLED and e.severity is Severity.LOUD for e in events)


def test_arrival_limit_remains_protected_when_uncertain_boarding_makes_it_silent():
    c = context(10, step=10, limit=time(9, 24))
    g = leg(1)
    s = occ(journey([g]))
    event = delayed_event(c, s.lock.legs[0], classify_leg(g, "RTD", 10), last=True)
    assert event.severity is Severity.LOUD
    transition = keep_stated(s, (held(event),), delay_min=10, delay_step_min=10)
    assert len(transition.events) == 1
    event = loads(type(event), dumps(transition.events[0]))
    assert event.kind is EventKind.DELAYED and event.severity is Severity.SILENT
    config = Config(1, (c.profile,))
    stored = PersistedState(config, {1: config}, {}, {}, {}, (), False)
    ledger = LiveLedger(lambda: stored, lambda: stored, PollMemory(), lambda: at("08:00"))
    assert ledger.notification_enabled(replace(ob(EventKind.DELAYED), event=event))


def test_serialization_preserves_settings_and_loads_old_defaults():
    assert decode(AlertSettings, {}).delay_min == 3
    assert decode(AlertSettings, {}).delay_step_min == 3
    settings = replace(AlertSettings(), delay_min=10, delay_step_min=5)
    assert loads(AlertSettings, dumps(settings)) == settings
    assert decode(ToldDelay, {"slot": "delay", "said": "7"}).previous_min is None
    delay = ToldDelay("delay", "13", 10)
    assert loads(ToldDelay, dumps(delay)) == delay


@pytest.mark.parametrize("field", ["delay_min", "delay_step_min"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, 3.0, "3", None, 1441])
def test_invalid_yaml_minutes_are_clear_errors(field, value):
    text = PROFILES.replace("UT_MIN", "4").replace("TRANSFERS", "[]")
    profiles, errors = parse_profiles(
        text + f"    alerts: {{{field}: {json.dumps(value)}}}\n", None
    )
    assert not profiles
    assert any(
        e.path.endswith(f"alerts.{field}") and "1" in e.message and "1440" in e.message
        for e in errors
    )


def test_yaml_minutes_parse():
    text = PROFILES.replace("UT_MIN", "4").replace("TRANSFERS", "[]")
    profiles, errors = parse_profiles(
        text + "    alerts: {delay_min: 10, delay_step_min: 5}\n", None
    )
    assert not errors
    assert (profiles[0].alerts.delay_min, profiles[0].alerts.delay_step_min) == (10, 5)


@pytest.mark.parametrize(
    "minimum,minutes,sends", [(1, 0, False), (1, 1, True), (2, 1, False), (2, 2, True)]
)
def test_low_threshold_first_alert(minimum, minutes, sends):
    s = occ(journey([leg()]))
    events = poll(s, context(minimum), ans("AAA", conn(leg(minutes)))).events
    assert (EventKind.DELAYED in [e.kind for e in events]) is sends


@pytest.mark.parametrize("minimum,minutes", [(1, 1), (10, 7)])
def test_ignoring_a_delay_never_claims_the_train_is_on_time(minimum, minutes):
    s = occ(journey([leg()]))
    answer = ans("AAA", conn(leg(minutes)))
    assert all_good(s, poll_answers(s, answer), context(minimum, now="08:00")) is None


@pytest.mark.parametrize(
    "minutes,kind", [(5, EventKind.TRANSFER_AT_RISK), (9, EventKind.TRANSFER_BROKEN)]
)
def test_transfer_protection_uses_actual_times_below_the_threshold(minutes, kind):
    c = ctx(at("08:25"))
    c = replace(
        c,
        profile=replace(
            c.profile,
            alerts=replace(
                c.profile.alerts,
                delay_min=1440,
                delay_step_min=1440,
            ),
        ),
    )
    incoming = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=minutes)
    events = poll(
        occ(journey([L1, L2], (LegState.RIDING,))), c, ans("AAA", conn(incoming, L2), when="08:25")
    ).events
    assert any(e.kind is kind and e.severity is Severity.LOUD for e in events)


async def test_held_delay_drops_after_a_profile_edit_and_restart(tmp_path):
    rig = Rig(tmp_path)
    text = PROFILE.replace("NAME", "office_a").replace(
        '    return: {first_train: "17:52"',
        '    alerts: {quiet_hours: ["07:51", "07:58"]}\n    return: {first_train: "17:52"',
        1,
    )
    rig.write(text=text)
    await rig.rt.start()
    await rig.rt.poll_once()
    rig.clock.set("07:52")
    late = make_leg("UT", "RTD", "08:50", "09:24", number="5678", delay_dep=7, delay_arr=7)
    rig.provider.world = [conn(L1, late)]
    await rig.rt.poll_once()
    assert any(o.event.kind is EventKind.DELAYED for o in rig.rt.state.obligations)
    rig.write(text=text.replace("quiet_hours:", "delay_min: 10, delay_step_min: 5, quiet_hours:"))
    bump_mtime(rig.path)
    rig.clock.set("07:54")
    await rig.rt.poll_once()
    await rig.restart()
    rig.clock.set("07:59")
    await rig.rt.watchdog_once()
    assert EventKind.DELAYED not in [e.kind for e in rig.rt.events_log]
    assert not any(o.event.kind is EventKind.DELAYED for o in rig.rt.state.obligations)
    assert rig.rt.state.config.profiles[0].alerts.delay_min == 10


@pytest.mark.parametrize(
    "initial,updated,minutes,previous",
    [
        (3, 10, 7, None),
        (3, 10, 7, 4),
        (10, 10, 13, 10),
    ],
)
def test_held_delay_rechecks_current_minimum_and_step(initial, updated, minutes, previous):
    c = context(initial)
    s = occ(journey([leg()]))
    if previous is not None:
        s = poll(s, c, ans("AAA", conn(leg(previous)))).state
    event = next(
        e for e in poll(s, c, ans("AAA", conn(leg(minutes)))).events if e.kind is EventKind.DELAYED
    )
    profile = replace(c.profile, alerts=replace(c.profile.alerts, quiet_hours=(time(7), time(10))))
    config = Config(1, (profile,))
    stored = PersistedState(config, {1: config}, {}, {}, {}, (), False)
    ledger = LiveLedger(lambda: stored, lambda: stored, PollMemory(), lambda: at("08:00"))
    obligation = replace(ob(EventKind.DELAYED, Severity.SILENT), event=event, occurrence=None)
    assert ledger.notification_enabled(obligation)
    assert revalidate(obligation, ledger, at("08:00")) == "hold"
    step_after = 3 if previous == 4 else 5
    p = replace(
        profile, alerts=replace(profile.alerts, delay_min=updated, delay_step_min=step_after)
    )
    stored = replace(stored, config=Config(2, (p,)))
    assert not ledger.notification_enabled(obligation)
    assert revalidate(obligation, ledger, at("08:00")) == "drop"
    assert ledger.notification_enabled(
        replace(obligation, event=replace(event, severity=Severity.LOUD))
    )
