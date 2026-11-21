import pytest
from builders import make_connection, make_leg
from ha_rig import jump, setup_tcc

pytestmark = pytest.mark.usefixtures("tcc_freezer")

WAKE_PROFILE = """
profiles:
  - id: p1
    name: Office
    days: [mon, tue, wed, thu, fri]
    home_station: AAA
    work_station: RTD
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
    alerts: {wake_at: "06:50"}
"""


@pytest.mark.parametrize("style", ["critical", "time_sensitive"])
async def test_wake_uses_the_configured_critical_mobile_notification(hass, style):
    world = [
        make_connection(
            [make_leg("AAA", "RTD", "08:12", "09:24", cancelled=True)], status="CANCELLED"
        ),
        make_connection([make_leg("AAA", "RTD", "07:42", "08:54", number="1111")]),
    ]
    h = await setup_tcc(hass, WAKE_PROFILE, now="06:18", world=world, options={"loud_style": style})
    await h.host.run(lambda rt: rt.wake_once())
    assert not any("Wake now" in call.data["message"] for call in h.pushes)
    await jump(h, "2026-10-27 06:20")
    await h.host.run(lambda rt: rt.wake_once())
    alarms = [call.data for call in h.pushes if "Wake now" in call.data["message"]]
    if style == "time_sensitive":
        assert not alarms
        return
    (alarm,) = alarms
    assert alarm["data"]["push"]["sound"]["critical"] == 1
    assert alarm["data"]["channel"] == "alarm_stream"
    assert alarm["data"]["ttl"] == 0


@pytest.mark.parametrize(
    "setting, blocked, enabled",
    [
        ("notifications_enabled_entity", "off", "on"),
        ("notifications_quiet_entity", "on", "off"),
    ],
)
async def test_wake_pause_and_quiet_skip_requests_until_restored(hass, setting, blocked, enabled):
    control = "input_boolean.wake_control"
    world = [
        make_connection(
            [make_leg("AAA", "RTD", "08:12", "09:24", cancelled=True)], status="CANCELLED"
        ),
        make_connection([make_leg("AAA", "RTD", "07:42", "08:54", number="1111")]),
    ]
    hass.states.async_set(control, enabled)
    h = await setup_tcc(
        hass,
        WAKE_PROFILE,
        now="06:18",
        world=world,
        options={"loud_style": "critical", setting: control},
    )
    await h.host.run(lambda rt: rt.wake_once())
    hass.states.async_set(control, blocked)
    await jump(h, "2026-10-27 06:20")
    calls = len(h.provider.calls)
    await h.host.run(lambda rt: rt.wake_once())
    assert len(h.provider.calls) == calls
    assert not await h.host.run(lambda rt: rt.state.wake_alarm_dates)
    assert not any("Wake now" in call.data["message"] for call in h.pushes)

    hass.states.async_set(control, enabled)
    await jump(h, "2026-10-27 06:20:30")
    await h.host.run(lambda rt: rt.wake_once())
    assert sum("Wake now" in call.data["message"] for call in h.pushes) == 1
