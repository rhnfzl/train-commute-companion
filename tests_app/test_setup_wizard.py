import stat
from dataclasses import replace
from datetime import timedelta

import pytest
from app_rig import CHAT, STRANGER, TOKEN, FakeTelegram, msg
from builders import make_connection, make_leg
from runtime_rig import FakeClock, Provider, StationsProvider, at, usual_on
from tcc_app.bot import ShellState, save_state
from tcc_app.main import Settings, build

from train_commute_companion.model import Direction, Provenance, Stations


@pytest.fixture
async def setup(tmp_path):
    telegram = FakeTelegram()
    clock = FakeClock(at("07:55"))
    day = clock.now().date() + timedelta(days=1)
    provider = Provider(
        clock,
        [
            usual_on(day),
            make_connection([make_leg("RTD", "AAA", "17:52", "18:35", number="7952", day=day)]),
        ],
    )
    stations = Stations({"AAA": ("Home Station",), "UT": ("Utrecht",), "RTD": ("Work Station",)})
    path = tmp_path / "profiles.yaml"
    save_state(tmp_path / "telegram.json", ShellState(bot_id="123", claimed_for=f"123:{CHAT}"))
    app = build(
        Settings("key", TOKEN, None, None, path, tmp_path),
        clock=clock,
        provider=StationsProvider(provider, stations),
        client=telegram.client(),
    )
    await app.rt.start()

    async def send(text, *, chat=CHAT, sender=CHAT, kind="private"):
        update = msg(app, text)
        update["message"]["chat"] = {"id": chat, "type": kind}
        update["message"]["from"] = {"id": sender}
        await app.bot.handle(update)

    async def tap(prefix, *, source=None, sender=CHAT):
        source = source or telegram.sent[-1]
        data = next(
            item["callback_data"]
            for row in source["reply_markup"]["inline_keyboard"]
            for item in row
            if item["text"].startswith(prefix)
        )
        await app.bot.handle(
            {
                "callback_query": {
                    "id": "tap",
                    "data": data,
                    "from": {"id": sender},
                    "message": {
                        **source,
                        "chat": {"id": CHAT, "type": "private"},
                        "message_id": telegram.sent.index(source) + 1,
                    },
                }
            }
        )

    try:
        yield app, provider, telegram, path, send, tap
    finally:
        await app.bot.close()
        await app.rt.stop_weather()
        await app.rt.stop_works_digest()
        await app.rt.recovery_idle()
        await telegram.close()


async def draft(send, tap):
    await send("/setup")
    await send("Home Station")
    await tap("Home Station")
    await send("rtd")
    await tap("Work Station")


async def test_confirmed_private_claim_can_save_a_checked_preview(setup):
    app, provider, telegram, path, send, tap = setup
    assert app.rt.config is None and not path.exists() and not app.bot.confirmed
    assert app.rt.known_stations is not None
    await draft(send, tap)
    await tap("Mon")
    await tap("Set times")
    await tap("08:00")
    await send("17:00")
    preview = telegram.sent[-1]["text"]
    assert "08:12" in preview and "change at UT" in preview
    assert "17:52" in preview and "direct" in preview and "Data: NS" in preview
    assert len(provider.calls) == 2 and not path.exists()
    await tap("Check-in and delay settings")
    await send("35 auto 8 4")
    await tap("Save commute")
    assert "Commute saved and loaded." in telegram.sent[-1]["text"]
    profile = app.rt.config.profiles[0]
    assert profile.days == frozenset({2, 3, 4, 5})
    assert profile.paused_on(app.rt.now().date())
    assert not profile.paused_on(app.rt.now().date() + timedelta(days=1))
    assert profile.journey(Direction.OUTBOUND).active_lead_min == 35
    assert profile.alerts.delay_min == 8 and profile.alerts.delay_step_min == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not app.bot.confirmed
    assert len(provider.calls) == 2
    assert telegram.edits[-1]["reply_markup"] == {"inline_keyboard": []}


async def test_arrival_setup_previews_and_saves_the_same_window(setup):
    app, provider, telegram, path, send, tap = setup
    day = app.rt.now().date() + timedelta(days=1)
    provider.world.extend(
        make_connection([leg])
        for leg in (
            make_leg(
                "AAA", "RTD", "08:00", "08:48", number="101", day=day, provenance=Provenance.PLANNED
            ),
            make_leg("AAA", "RTD", "08:15", "08:48", number="102", day=day, delay_arr=4),
            make_leg("AAA", "RTD", "08:21", "08:49", number="103", day=day, cancelled=True),
        )
    )
    await draft(send, tap)
    await tap("Arrive by a set time")
    await send("00:00")
    assert "Choose an arrival after midnight" in telegram.sent[-1]["text"]
    await send("09:00")
    await tap("10 minutes")
    await send("17:00")
    preview = telegram.sent[-1]["text"]
    assert "Arrive by 09:00, including 10 minutes" in preview
    assert "08:00 AAA to RTD" in preview and "08:15" not in preview
    assert not path.exists()
    outbound, returning = provider.calls
    assert outbound.search_for_arrival and outbound.at.strftime("%H:%M") == "08:50"
    assert not returning.search_for_arrival
    await tap("Save commute")
    assert app.rt.config is not None
    journey = app.rt.config.profiles[0].journey(Direction.OUTBOUND)
    assert journey.pick == "arrive_by" and not journey.first_train_set
    assert journey.arrive_by.strftime("%H:%M") == "09:00"
    assert journey.walk_after_min == 10 and not journey.transfers
    assert [when.strftime("%H:%M") for when in journey.window] == ["06:50", "09:00"]


async def test_setup_cannot_reuse_buttons_or_replace_a_file_created_after_preview(setup):
    _, _, telegram, path, send, tap = setup
    await send("/setup", chat=STRANGER, sender=STRANGER)
    assert not telegram.sent
    await send("/setup", kind="group")
    assert "private setup link" in telegram.sent[-1]["text"]
    await draft(send, tap)
    days = telegram.sent[-1]
    await tap("Make this mine", sender=STRANGER)
    assert "no longer valid" in telegram.sent[-1]["text"]
    await tap("Make this mine", source=days)
    preview = telegram.sent[-1]
    assert "Save commute" in str(preview["reply_markup"])
    await tap("Make this mine", source=days)
    assert "expired" in telegram.sent[-1]["text"]

    path.write_text("manually written")
    await tap("Save commute", source=preview)
    assert "already in use" in telegram.sent[-1]["text"]
    assert path.read_text() == "manually written"


async def test_incomplete_preview_offers_retry_without_writing_any_profile(setup):
    _, provider, telegram, path, send, tap = setup
    provider.world.pop()
    await draft(send, tap)
    await tap("Make this mine")
    assert "did not return a usable train" in telegram.sent[-1]["text"]
    assert "Retry preview" in str(telegram.sent[-1]["reply_markup"])
    assert not path.exists()


async def test_setup_text_stays_local_and_cancel_retires_the_draft(setup, monkeypatch):
    app, provider, telegram, path, send, tap = setup
    app.bot.state.confirmed_for = f"123:{CHAT}"

    async def unexpected_travel_request(text):
        pytest.fail("Setup text went to the travel interpreter")

    monkeypatch.setattr(app.bot._travel_chat, "interpret", unexpected_travel_request)
    await send("/setup")
    await send("Home Station", sender=STRANGER)
    await send("Home Station")
    options = telegram.sent[-1]
    provider.clock.jump(16)
    await send("Home Station")
    assert "session has ended" in telegram.sent[-1]["text"]
    await send("/cancel_setup")
    assert "Setup closed." in telegram.sent[-1]["text"]
    await tap("Home Station", source=options)
    assert "expired" in telegram.sent[-1]["text"] and not path.exists()


@pytest.mark.parametrize("invalid", ["short_transfer", "return_before_arrival"])
async def test_preview_rejects_the_trips_that_the_saved_profile_cannot_follow(setup, invalid):
    app, provider, telegram, path, send, tap = setup
    day = app.rt.now().date() + timedelta(days=1)
    if invalid == "short_transfer":
        incoming = replace(provider.world[0].legs[0], transfer_time_to_next=3)
        outgoing = make_leg("UT", "RTD", "08:44", "09:24", number="5678", day=day)
        provider.world[0] = make_connection([incoming, outgoing])
    else:
        provider.world[0] = make_connection(
            [make_leg("AAA", "RTD", "08:12", "18:15", number="1234", day=day)]
        )
    await draft(send, tap)
    await tap("Make this mine")
    buttons = str(telegram.sent[-1]["reply_markup"])
    assert "Change times" in buttons and "Save commute" not in buttons
    assert not path.exists()
