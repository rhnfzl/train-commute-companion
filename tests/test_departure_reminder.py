import httpx
import pytest
from builders import ONE_TRANSFER, make_connection, make_leg
from runtime_rig import DAY, PROFILE, Rig, at
from test_travel_plans import save
from test_weather_runtime import forecast_for

from train_commute_companion.location import LocationEvent
from train_commute_companion.model import EventKind, alert_facts
from train_commute_companion.providers.weather import WeatherProvider

KIND = EventKind.DEPARTURE_REMINDER


async def configured(tmp_path, alerts=""):
    rig = Rig(tmp_path)
    rig.write(
        text=PROFILE.replace(
            "    name:", f"    location: {{source: telegram}}\n{alerts}    name:", 1
        )
    )
    await rig.rt.start()
    await rig.run_until("08:00")
    return rig


async def presence(rig, hhmm, kind="report"):
    rig.clock.set(hhmm)
    await rig.rt.report_location("p1", LocationEvent("home", kind, at(hhmm)))


def reminders(rig):
    return [e.text for e in rig.notifier.attempts if e.kind is KIND]


async def test_reminds_once_without_a_new_query_or_a_train_change(tmp_path):
    rig = await configured(tmp_path)
    await presence(rig, "08:00")
    await rig.rt.poll_once()
    planned = rig.occ().lock
    requests = len(rig.provider.calls)
    await rig.rt.watchdog_once()
    assert not reminders(rig)
    rig.clock.set("08:02:30")
    await rig.rt.watchdog_once()
    assert reminders(rig) == ["Name: Leave now for the 08:12 train."]
    assert rig.occ().lock == planned
    assert len(rig.provider.calls) == requests
    await rig.restart()
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    assert len(reminders(rig)) == 1


@pytest.mark.parametrize("reason", ["left", "no_presence", "stale_board", "disabled"])
async def test_reminder_requires_current_presence_and_a_running_train(tmp_path, reason):
    alerts = "    alerts: {location_leave_now: false}\n" if reason == "disabled" else ""
    rig = await configured(tmp_path, alerts)
    if reason != "no_presence":
        await presence(rig, "08:00")
    if reason == "left":
        await presence(rig, "08:01", "leave")
    if reason == "stale_board":
        rig.clock.set("08:03:59")
    else:
        rig.clock.set("08:02:30")
        await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    assert not reminders(rig)


async def test_weather_reminder_uses_cached_forecast_and_keeps_the_same_train(tmp_path):
    rig = await configured(tmp_path)
    rig.provider.positions = {"AAA": (52.1, 5.1)}
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, json=forecast_for(rig.clock.now()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        rig.rt._weather = WeatherProvider(client, rig.clock.now)
        await rig.rt.poll_once()
        await rig.rt.weather_idle()
        await presence(rig, "08:00:30")
        lock = rig.occ().lock
        saved = len(requests)
        await rig.rt.watchdog_once()
        assert reminders(rig) == [
            "Name: Leave now for the 08:12 train. Allow 2 extra minutes for forecast rain."
        ]
        assert saved == len(requests) == 1
        assert rig.occ().lock == lock


async def test_retry_drops_departure_reminder_after_leaving_home(tmp_path):
    rig = await configured(tmp_path)
    await presence(rig, "08:00")
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: e.kind is KIND
    rig.clock.set("08:02:10")
    await rig.rt.watchdog_once()
    assert len(reminders(rig)) == 1
    rig.notifier.fails = lambda e: False
    await presence(rig, "08:02:30", "leave")
    await rig.rt.watchdog_once()
    assert len(reminders(rig)) == 1


async def test_departure_reminder_retry_preserves_the_original_language(tmp_path):
    rig = await configured(tmp_path)
    await presence(rig, "08:00")
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: e.kind is KIND
    rig.clock.set("08:02:10")
    await rig.rt.watchdog_once()
    rig.write(
        text=rig.path.read_text().replace(
            "    location:", "    alerts: {language: nl}\n    location:", 1
        )
    )
    rig.notifier.fails = lambda e: False
    assert await rig.rt.reload() is None
    await rig.rt.watchdog_once()
    first, retry = [e for e in rig.notifier.attempts if e.kind is KIND]
    assert retry.key == first.key
    assert first.text == retry.text == "Name: Leave now for the 08:12 train."
    assert alert_facts(retry).language == "en"


@pytest.mark.parametrize("restart", [False, True])
async def test_location_source_change_requires_new_presence(tmp_path, restart):
    rig = await configured(tmp_path)
    await presence(rig, "08:00")
    rig.write(
        text=rig.path.read_text().replace("source: telegram", "source: ha, entity: person.other")
    )
    assert await rig.rt.reload() is None
    if restart:
        await rig.restart()
    rig.clock.set("08:02:30")
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    assert not reminders(rig)
    await presence(rig, "08:03")
    await rig.rt.watchdog_once()
    assert len(reminders(rig)) == 1


async def test_reenabled_location_does_not_restore_old_presence(tmp_path):
    rig = await configured(tmp_path)
    await presence(rig, "08:00")
    enabled = rig.path.read_text()
    rig.write(text=enabled.replace("source: telegram", "source: telegram, enabled: false"))
    assert await rig.rt.reload() is None
    rig.write(text=enabled)
    assert await rig.rt.reload() is None
    rig.clock.set("08:02:30")
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    assert not reminders(rig)


@pytest.mark.parametrize("disable_location", [False, True])
async def test_flexible_trip_respects_current_source_opt_out(tmp_path, disable_location):
    rig = Rig(tmp_path, now="07:00")
    text = PROFILE.replace("    name:", "    location: {source: telegram}\n    name:", 1)
    rig.write(text=text)
    await rig.rt.start()
    change = {
        "action": "create",
        "plan": {
            "name": "Offsite Visit",
            "profile_id": "p1",
            "origin": "AAA",
            "destination": "RTD",
            "start_date": DAY.isoformat(),
            "outbound": {"mode": "exact", "departure": "08:12"},
        },
        "conflict_policy": "replace",
    }
    pid = save(rig.rt, change, "departure-optout")["plan_id"]
    await rig.run_until("08:00")
    await rig.rt.report_location(pid, LocationEvent("home", "report", at("08:00")))
    tracked = next(p for p in rig.rt.view().profiles if p.id == pid)
    assert tracked.location is not None
    if disable_location:
        text = text.replace("source: telegram", "source: telegram, enabled: false")
    else:
        text = text.replace("    name:", "    alerts: {location_leave_now: false}\n    name:", 1)
    rig.write(text=text)
    assert await rig.rt.reload() is None
    tracked = next(p for p in rig.rt.view().profiles if p.id == pid)
    assert (tracked.location is None) if disable_location else not tracked.alerts.location_leave_now
    rig.clock.set("08:02:30")
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    assert not reminders(rig)


async def test_reminder_does_not_suggest_a_journey_with_a_cancelled_transfer(tmp_path):
    rig = await configured(tmp_path)
    await presence(rig, "08:00")
    rig.provider.world = [
        make_connection(
            [
                ONE_TRANSFER[0].legs[0],
                make_leg("UT", "RTD", "08:50", "09:24", number="5678", cancelled=True),
            ]
        )
    ]
    rig.clock.set("08:02:30")
    await rig.rt.poll_once()
    assert any(e.kind is EventKind.CANCELLED for e in rig.notifier.attempts)
    await rig.rt.watchdog_once()
    assert not reminders(rig)
