from dataclasses import replace

import pytest
from builders import make_connection, make_leg
from lifecycle_fixtures import DIRECT, ans, at, ctx
from runtime_rig import DAY, PROFILE, Rig

from train_commute_companion.calendar import hhmm
from train_commute_companion.model import EventKind, Severity
from train_commute_companion.notify import NotificationPaused
from train_commute_companion.runtime import Runtime
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.wake_alarm import earlier_wake, observe_wake

USUAL = make_connection([make_leg("AAA", "RTD", "08:12", "09:24")])
EARLY = make_connection([make_leg("AAA", "RTD", "07:42", "08:54", number="1111")])
STOPPED = replace(USUAL, status="CANCELLED")


def option(usual, earlier, *, when="06:18"):
    time = at(when)
    return earlier_wake(
        ctx(time, DIRECT),
        ans("AAA", *usual, when=when),
        ans("AAA", *earlier, when=when),
        at("06:50"),
    )


def test_two_matching_fresh_samples_advance_the_alarm_once_it_is_due():
    first = option([STOPPED], [EARLY])
    assert first.reason == "cancelled"
    assert hhmm(first.at) == "06:20"
    observed = observe_wake(None, first, at("06:18"))
    assert not observed.ready(at("06:20"))
    assert observe_wake(observed, first, at("06:18")) == observed
    confirmed = observe_wake(observed, option([STOPPED], [EARLY], when="06:20"), at("06:20"))
    assert not confirmed.ready(at("06:19:59"))
    assert confirmed.ready(at("06:20"))
    assert not confirmed.ready(at("06:22"))
    assert observe_wake(confirmed, None, at("06:20:30")) is None


@pytest.mark.parametrize("usual", [[], [USUAL], [USUAL, STOPPED]])
def test_no_alarm_on_missing_healthy_or_conflicting_usual_train(usual):
    assert option(usual, [EARLY]) is None


def test_conflicting_status_for_earlier_train_cannot_wake_anyone():
    assert option([STOPPED], [replace(EARLY, status="CANCELLED"), EARLY]) is None


def test_conflicting_status_across_both_queries_cannot_wake_anyone():
    assert option([STOPPED], [USUAL, EARLY]) is None
    assert option([STOPPED, replace(EARLY, status="CANCELLED")], [EARLY]) is None


def test_conflicting_boarding_stop_in_another_route_cannot_wake_anyone():
    first = make_leg("AAA", "UT", "07:42", "08:02", number="1111")
    onward = make_leg("UT", "RTD", "08:12", "08:54", number="4321")
    cancelled_boarding = replace(first, stops=(replace(first.board, cancelled=True), first.alight))
    assert option([STOPPED, make_connection([cancelled_boarding, onward])], [EARLY]) is None
    assert option([STOPPED, make_connection([first, replace(onward, cancelled=True)])], [EARLY])


def test_reported_delay_must_make_earlier_train_worth_taking():
    delayed = make_connection(
        [make_leg("AAA", "RTD", "08:12", "09:24", number="1234", delay_arr=13)]
    )
    slow = make_connection([make_leg("AAA", "RTD", "07:42", "09:36", number="1111")])
    assert option([delayed], [EARLY]).reason == "delayed"
    assert option([delayed], [slow]) is None


async def start_runtime(rig, destination):
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        wake_destination=lambda: destination,
    )
    await rig.rt.start()


def wake_profile(*, armed=True):
    text = PROFILE.replace("transfers: [UT]", "transfers: []", 1)
    return (
        text.replace("    name:", '    alerts: {wake_at: "06:50"}\n    name:', 1) if armed else text
    )


async def test_runtime_wakes_once_after_confirmation_and_reserves_across_restart(tmp_path):
    rig = Rig(tmp_path, now="06:18")
    rig.write(text=wake_profile())
    rig.provider.world = [STOPPED, EARLY]
    await start_runtime(rig, "ha-mobile:phone")
    calls = len(rig.provider.calls)
    await rig.rt.wake_once()
    assert len(rig.provider.calls) == calls + 2
    assert not [e for e in rig.notifier.attempts if e.kind is EventKind.WAKE_ALARM]
    rig.clock.set("06:20")
    await rig.rt.wake_once()
    (event,) = [e for e in rig.notifier.attempts if e.kind is EventKind.WAKE_ALARM]
    assert event.severity is Severity.LOUD
    assert "07:42" in event.text and "cancelled" in event.text
    assert rig.rt.state.wake_alarm_dates == {"p1": DAY}
    p = rig.rt.config.profiles[0]
    assert hhmm(dated_schedule(p, event.direction, DAY, None).first_departure) == "08:12"
    await start_runtime(rig, "ha-mobile:phone")
    rig.clock.set("06:21")
    await rig.rt.wake_once()
    assert [e for e in rig.notifier.attempts if e.kind is EventKind.WAKE_ALARM] == [event]


@pytest.mark.parametrize("destination, armed", [("ha-mobile:phone", False), (None, True)])
async def test_runtime_uses_no_wake_requests_without_both_opt_ins(tmp_path, destination, armed):
    rig = Rig(tmp_path, now="06:18")
    rig.write(text=wake_profile(armed=armed))
    rig.provider.world = [STOPPED, EARLY]
    await start_runtime(rig, destination)
    calls = len(rig.provider.calls)
    await rig.rt.wake_once()
    assert len(rig.provider.calls) == calls


async def test_second_sample_that_recovers_cancels_the_wake(tmp_path):
    rig = Rig(tmp_path, now="06:18")
    rig.write(text=wake_profile())
    rig.provider.world = [STOPPED, EARLY]
    await start_runtime(rig, "ha-mobile:phone")
    await rig.rt.wake_once()
    rig.clock.set("06:20")
    rig.provider.world = [USUAL, EARLY]
    await rig.rt.wake_once()
    assert not any(e.kind is EventKind.WAKE_ALARM for e in rig.notifier.attempts)


@pytest.mark.parametrize(
    "update, expected",
    [
        ('wake_at: "06:50", checked_alternatives: false', True),
        ('wake_at: "06:50", partner_eta: true, partner_phone: partner', True),
        ('wake_at: "06:51"', False),
    ],
)
async def test_only_relevant_profile_edits_reset_wake_evidence(tmp_path, update, expected):
    rig = Rig(tmp_path, now="06:18")
    rig.write(text=wake_profile())
    rig.provider.world = [STOPPED, EARLY]
    await start_runtime(rig, "ha-mobile:phone")
    await rig.rt.wake_once()
    rig.clock.set("06:19")
    rig.write(text=wake_profile().replace('wake_at: "06:50"', update))
    assert await rig.rt.reload() is None
    rig.clock.set("06:20")
    await rig.rt.wake_once()
    assert any(e.kind is EventKind.WAKE_ALARM for e in rig.notifier.attempts) is expected


async def test_cached_wake_cannot_ring_after_catch_deadline(tmp_path):
    rig = Rig(tmp_path, now="07:29")
    rig.write(text=wake_profile().replace('wake_at: "06:50"', 'wake_at: "08:02"'))
    rig.provider.world = [STOPPED, EARLY]
    await start_runtime(rig, "ha-mobile:phone")
    await rig.rt.wake_once()
    rig.clock.set("07:31")
    await rig.rt.wake_once()
    calls = len(rig.provider.calls)

    rig.clock.set("07:32:30")
    await rig.rt.wake_once()

    assert len(rig.provider.calls) == calls
    assert not any(e.kind is EventKind.WAKE_ALARM for e in rig.notifier.attempts)
    assert not rig.rt.state.wake_alarm_dates


@pytest.mark.parametrize("revoked", ["receiver", "profile"])
async def test_revocation_mid_probe_stops_the_second_request(tmp_path, revoked):
    rig = Rig(tmp_path, now="06:18")
    rig.write(text=wake_profile())
    rig.provider.world = [STOPPED, EARLY]
    await start_runtime(rig, "ha-mobile:phone")

    def revoke(_):
        if revoked == "receiver":
            rig.rt._wake_destination = lambda: None
        else:
            rig.rt._excluded_profiles = lambda: frozenset({"p1"})

    rig.provider.hook = revoke
    count = len(rig.provider.calls)
    await rig.rt.wake_once()
    assert len(rig.provider.calls) == count + 1
    assert not rig.rt.state.wake_alarm_dates


@pytest.mark.parametrize("storage_failed", [False, True])
async def test_known_refusal_rolls_back_reservation_and_allows_in_window_retry(
    tmp_path, monkeypatch, storage_failed
):
    rig = Rig(tmp_path, now="06:18")
    rig.write(text=wake_profile())
    rig.provider.world = [STOPPED, EARLY]
    await start_runtime(rig, "ha-mobile:phone")
    await rig.rt.wake_once()
    real_send = rig.notifier.send

    async def paused(_):
        rig.store.fail = storage_failed
        raise NotificationPaused

    monkeypatch.setattr(rig.notifier, "send", paused)
    rig.clock.set("06:20")
    await rig.rt.wake_once()
    assert bool(rig.store.load().wake_alarm_dates) is storage_failed

    monkeypatch.setattr(rig.notifier, "send", real_send)
    rig.clock.set("06:20:30")
    rig.store.fail = False
    await rig.rt.watchdog_once()
    await rig.rt._wake_refresh.idle()
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.WAKE_ALARM]) == 1
