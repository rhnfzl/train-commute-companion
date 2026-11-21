"""Private Telegram callbacks follow a real recovery without inferred progress."""

import pytest
from app_rig import CHAT, STRANGER, TOKEN, FakeTelegram
from recovery_sample import NOW, answer
from runtime_rig import Rig
from tcc_app.bot import Bot
from tcc_app.commands import (
    connections_tool,
    recovery_progress_buttons,
    render_status,
    status_buttons,
)
from tcc_app.telegram import TelegramApi

from train_commute_companion.providers.base import ServerError

PROFILE = """profiles:
  - id: p1
    name: office
    days: [sat]
    home_station: UT
    work_station: RTD
    outbound: {first_train: '11:58', transfers: [], buffer_min: 5, window: ['10:30', '16:00']}
    return: {first_train: '19:00', buffer_min: 5}
"""


def tap(data, uid=1, chat=CHAT):
    return {"callback_query": {"id": f"q{uid}", "data": data, "message": {"chat": {"id": chat}}}}


def occurrence(rig):
    return next(o for o in rig.rt.view().occurrences if o.key.direction.value == "outbound")


def buttons(tg):
    return [
        b for row in tg.sent[-1].get("reply_markup", {}).get("inline_keyboard", []) for b in row
    ]


@pytest.fixture
async def app(tmp_path):
    rig = Rig(tmp_path)
    rig.clock.t = NOW
    rig.write(text=PROFILE)
    rig.provider.world = list(answer().connections)
    await rig.rt.start()
    tg = FakeTelegram()
    bot = Bot(TelegramApi(TOKEN, tg.client()), rig.rt, CHAT, tmp_path / "telegram.json")
    bot.state.confirmed_for = f"123:{CHAT}"
    yield rig, tg, bot
    await bot.close()
    await tg.close()


async def offered(app):
    rig, tg, bot = app
    plan = occurrence(rig).replacement_plans[0]
    await bot.handle(tap(f"rp|{plan.proposal_id}"))
    assert occurrence(rig).selected_recovery is None
    assert "exact physical starting place" in tg.sent[-1]["text"]
    bus_index = next(i for i, s in enumerate(plan.itinerary.steps) if s.mode == "BUS")
    assert any(b["callback_data"] == f"ra|{plan.proposal_id}|{bus_index}" for b in buttons(tg))
    await bot.handle(tap(f"ra|{plan.proposal_id}|{bus_index}", 2))
    assert occurrence(rig).selected_recovery is None
    assert "planned-only times" in tg.sent[-1]["text"]
    assert "unverified accessibility" in tg.sent[-1]["text"]
    return buttons(tg)[0]["callback_data"]


async def start(app):
    follow = await offered(app)
    await app[2].handle(tap(follow, 3))
    return follow


def on_status(data, markup, uid):
    message = {"chat": {"id": CHAT}, "message_id": 9, "reply_markup": markup}
    return {"callback_query": {"id": f"q{uid}", "data": data, "message": message}}


def without(markup, data):
    rows = [[b for b in row if b["callback_data"] != data] for row in markup["inline_keyboard"]]
    return {
        "chat_id": CHAT,
        "message_id": 9,
        "reply_markup": {"inline_keyboard": [r for r in rows if r]},
    }


async def test_follow_on_status_removes_only_its_own_button(app):
    rig, tg, bot = app
    follow = await offered(app)
    markup = status_buttons(rig.rt.view())
    tags = [b["callback_data"].split("|")[0] for row in markup["inline_keyboard"] for b in row]
    assert "l" in tags and "s" in tags and "rp" in tags and tags.count("rf") == 1
    await bot.handle(on_status(follow, markup, 10))
    assert occurrence(rig).selected_recovery is not None
    assert tg.edits == [without(markup, follow)]  # journey and other recovery buttons stay


async def test_recovery_step_on_status_removes_only_its_own_button(app):
    rig, tg, bot = app
    await start(app)
    selected = occurrence(rig).selected_recovery
    rig.clock.t = max(rig.clock.t, selected.itinerary.steps[selected.index].departure)
    markup = status_buttons(rig.rt.view())
    [boarded], [stop] = recovery_progress_buttons(selected)["inline_keyboard"]
    tags = [b["callback_data"].split("|")[0] for row in markup["inline_keyboard"] for b in row]
    assert tags[:2] == ["l", "s"] and boarded in markup["inline_keyboard"][-2]
    await bot.handle(on_status(boarded["callback_data"], markup, 10))
    assert occurrence(rig).selected_recovery.revision == selected.revision + 1
    expected = without(markup, boarded["callback_data"])
    assert tg.edits == [expected] and [stop] in expected["reply_markup"]["inline_keyboard"]


async def test_bus_walk_metro_manual_journey_duplicate_and_stale_callbacks(app):
    rig, tg, bot = app
    follow = await start(app)
    selected = occurrence(rig).selected_recovery
    assert selected.planned_times_acknowledged and selected.accessibility_unknown_acknowledged
    assert selected.itinerary.steps[0].mode == "BUS"
    rig.clock.jump(180)
    await rig.rt.watchdog_once()
    assert occurrence(rig).selected_recovery.index == 0
    uid, kinds = 10, []
    while (selected := occurrence(rig).selected_recovery).status == "active":
        data = buttons(tg)[0]["callback_data"]
        assert len(data.encode()) <= 64
        step = selected.itinerary.steps[selected.index]
        kinds.append(selected.next_action)
        rig.clock.t = max(
            rig.clock.t, step.departure if selected.next_action == "boarded" else step.arrival
        )
        await bot.handle(tap(data, uid))
        after = occurrence(rig).selected_recovery
        assert after.revision == selected.revision + 1
        await bot.handle(tap(data, uid))
        assert occurrence(rig).selected_recovery == after
        await bot.handle(tap(data, uid + 100))
        assert "button is old" in tg.sent[-1]["text"]
        assert occurrence(rig).selected_recovery == after
        # Restore current controls after the deliberate stale reply.
        await bot._reply("Current recovery", recovery_progress_buttons(after))
        uid += 1
    assert kinds == [
        "boarded",
        "arrived",
        "walk_arrived",
        "boarded",
        "arrived",
        "boarded",
        "arrived",
    ]
    assert occurrence(rig).selected_recovery.status == "completed"
    await bot.handle(tap(follow, 999))
    assert "button is old" in tg.sent[-1]["text"]


async def test_foreign_unconfirmed_and_old_offer_never_select(app):
    rig, tg, bot = app
    plan = occurrence(rig).replacement_plans[0]
    await bot.handle(tap(f"rp|{plan.proposal_id}", chat=STRANGER))
    assert not tg.sent and not tg.answered
    bot.state.confirmed_for = None
    await bot.handle(tap(f"rp|{plan.proposal_id}"))
    assert "/test" in tg.sent[-1]["text"]
    assert occurrence(rig).recovery_offer is None
    assert not any(
        b["callback_data"].startswith("rp|")
        for row in status_buttons(rig.rt.view(), False)["inline_keyboard"]
        for b in row
    )
    bot.state.confirmed_for = f"123:{CHAT}"
    await bot.handle(tap("rf|" + "0" * 24))
    assert occurrence(rig).selected_recovery is None
    assert "button is old" in tg.sent[-1]["text"]


async def test_recovery_button_is_named_and_removed_once_used(app):
    rig, tg, bot = app
    plan = occurrence(rig).replacement_plans[0]
    markup = status_buttons(rig.rt.view())
    message = {"chat": {"id": CHAT}, "message_id": 7, "reply_markup": markup}
    await bot.handle({"callback_query": {"id": "q1", "data": "rf|" + "0" * 24, "message": message}})
    assert tg.sent[-1]["text"] == "Recovery: That recovery button is old; open /status again."
    assert tg.edits == []
    await bot.handle(
        {"callback_query": {"id": "q2", "data": f"rp|{plan.proposal_id}", "message": message}}
    )
    assert tg.sent[-1]["text"].startswith("Review recovery starting place: ")
    assert tg.edits == []  # only a prompt: the journey buttons on /status stay
    origin = next(b for b in buttons(tg) if b["callback_data"].startswith("ra|"))
    prompt = {"chat": {"id": CHAT}, "message_id": 8, "reply_markup": tg.sent[-1]["reply_markup"]}
    await bot.handle({"callback_query": {"id": "q3", "data": origin["callback_data"],
                                         "message": prompt}})  # fmt: skip
    assert tg.sent[-1]["text"].startswith(origin["text"] + ": ")
    assert tg.edits == [{"chat_id": CHAT, "message_id": 8, "reply_markup": {"inline_keyboard": []}}]


async def test_stop_and_read_only_status_show_actual_progress(app):
    rig, tg, bot = app
    await start(app)
    selected = occurrence(rig).selected_recovery
    text = render_status(rig.rt.view(), True)
    assert "Recovery active" in text and "Last confirmed place" in text
    result = connections_tool(rig.rt.view(), True, "p1", selected.key.direction)
    assert "explicit confirmations" in str(result)
    stop = buttons(tg)[1]["callback_data"]
    await bot.handle(tap(stop, 50))
    assert occurrence(rig).selected_recovery.status == "stopped"
    assert "Recovery stopped" in tg.sent[-1]["text"]


@pytest.mark.parametrize("suffix", ["-1", "9999", "1.0", "true", "0|extra"])
async def test_invalid_origin_callback_does_not_refresh_or_select(app, suffix):
    rig, tg, bot = app
    plan = occurrence(rig).replacement_plans[0]
    await bot.handle(tap(f"ra|{plan.proposal_id}|{suffix}"))
    assert "button is old" in tg.sent[-1]["text"]
    assert occurrence(rig).recovery_offer is None
    assert occurrence(rig).selected_recovery is None


async def test_offer_expiry_and_durable_save_refusal_keep_selection_absent(app):
    rig, tg, bot = app
    plan = occurrence(rig).replacement_plans[0]
    bus_index = next(i for i, step in enumerate(plan.itinerary.steps) if step.mode == "BUS")
    await bot.handle(tap(f"ra|{plan.proposal_id}|{bus_index}"))
    follow = buttons(tg)[0]["callback_data"]
    rig.clock.jump(301)
    await bot.handle(tap(follow, 2))
    assert occurrence(rig).selected_recovery is None
    assert "button is old" in tg.sent[-1]["text"]
    rig.clock.t = NOW
    await bot.handle(tap(f"ra|{plan.proposal_id}|{bus_index}", 3))
    follow = buttons(tg)[0]["callback_data"]
    rig.store.fail = True
    await bot.handle(tap(follow, 4))
    assert occurrence(rig).selected_recovery is None
    assert "saved" in tg.sent[-1]["text"]


async def test_failed_refresh_keeps_manual_actual_confirmation_available(app):
    rig, tg, bot = app
    await start(app)
    data = buttons(tg)[0]["callback_data"]
    rig.provider.error = lambda query: ServerError("unavailable")
    # A real actual boarding confirmation is valid despite old source data.
    rig.clock.jump(301)
    await rig.rt.poll_once()
    await rig.rt.recovery_idle()
    assert occurrence(rig).selected_recovery.monitoring_error is not None
    await bot.handle(tap(data, 10))
    selected = occurrence(rig).selected_recovery
    assert selected.revision == 1 and selected.index == 0
    assert selected.confirmations[0].id == "q10"
    assert selected.confirmations[0].at == rig.clock.now()


async def test_original_done_is_labeled_separately_while_recovery_remains_active(app, tmp_path):
    from dataclasses import replace

    from vehicle_rig import make_rig

    from train_commute_companion.model import DirectionState

    rig, _tg, _bot = app
    await start(app)
    (tmp_path / "vehicle").mkdir()
    vehicle = make_rig(tmp_path / "vehicle")
    await vehicle.rt.start()
    await vehicle.rt.vehicle_context_idle()
    context = next(
        o.train_context for o in vehicle.rt.view().occurrences if o.train_context is not None
    )
    current = occurrence(rig)
    original = replace(
        current,
        direction_state=DirectionState.DONE,
        next_good=answer().connections[0],
        retained=(current.legs,),
        train_context=context,
    )
    shown = replace(rig.rt.view(), occurrences=(original,))
    text = render_status(shown, True)
    assert ": recovery active" in text
    assert "Original train monitoring state: done" in text
    assert "Original train legs (separate from selected recovery)" in text
    assert "Original retained train warnings" in text
    assert "Original train vehicle context (not the selected recovery)" in text
    assert "next good connection" not in text and "earlier train advised" not in text
    assert "Last confirmed place" in text
    result = connections_tool(shown, True, "p1", original.key.direction)
    assert result["journeys"][0]["state"] == "recovery_active"
    assert result["journeys"][0]["original_train_state"] == "done"
    assert result["journeys"][0]["next_good"] is None
    assert not any(
        b["text"].startswith("Take ")
        for row in status_buttons(shown)["inline_keyboard"]
        for b in row
    )
