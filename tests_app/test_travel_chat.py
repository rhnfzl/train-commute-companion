import json
from datetime import UTC, datetime

import httpx
from test_travel_actions import Plans


def tap(chat, reply, text):
    buttons = [button for row in reply.markup["inline_keyboard"] for button in row]
    data = next(button["callback_data"] for button in buttons if button["text"] == text)
    return chat.button(data)


def test_guided_monthly_commute_can_end_after_a_trip_count():
    from tcc_app.travel_chat import TravelChat

    chat = TravelChat(Plans())
    tap(chat, chat.command("/plan"), "New plan")
    chat.text("Rotterdam Centraal")
    chat.text("Utrecht Centraal")
    tap(chat, chat.text("2026-11-24"), "Monthly")
    tap(chat, chat.text("1 Mon"), "After N trips")
    assert "couldn't use" in chat.text("0").text
    tap(chat, chat.text("3"), "No skips")
    assert chat.draft["plan"]["monthly"] == {"ordinal": 1, "weekday": 1}
    assert chat.draft["plan"]["end_after"] == 3
    assert "end_date" not in chat.draft["plan"]


def test_guided_temporary_commute_reviews_scope_and_offers_undo():
    from tcc_app.travel_chat import TravelChat

    class Planner(Plans):
        def preview_travel_plan(self, change):
            self.change = change
            if change["action"] == "create" and change.get("conflict_policy") != "replace":
                return {
                    "status": "conflict",
                    "text": "Your usual commute overlaps on November 24 and December 8.",
                }
            return super().preview_travel_plan(change)

    planner = Planner()
    chat = TravelChat(planner)
    tap(chat, chat.command("/plan"), "New plan")
    chat.text("Rotterdam Centraal")
    chat.text("Utrecht Centraal")
    reply = chat.text("2026-11-24")
    tap(chat, reply, "Repeating")
    chat.text("Tue")
    chat.text("1")
    chat.text("2026-12-08")
    reply = chat.text("2026-12-01")
    tap(chat, reply, "Exact train")
    reply = chat.text("08:12 835")
    reply = tap(chat, reply, "Next day")
    tap(chat, reply, "Exact train")
    reply = chat.text("00:20")
    assert "November 24" in reply.text
    assert planner.changes == 0
    reply = tap(chat, reply, "Replace overlapping trips")
    assert planner.change == {
        "action": "create",
        "conflict_policy": "replace",
        "plan": {
            "origin": "Rotterdam Centraal",
            "destination": "Utrecht Centraal",
            "start_date": "2026-11-24",
            "end_date": "2026-12-08",
            "weekdays": [2],
            "interval_weeks": 1,
            "skip_dates": ["2026-12-01"],
            "outbound": {"mode": "exact", "departure": "08:12", "train_number": "835"},
            "return": {"mode": "exact", "departure": "00:20", "day_offset": 1},
        },
    }
    ready = reply
    reply = tap(chat, ready, "Save plan")
    assert planner.changes == 1
    assert tap(chat, ready, "Save plan").done is False
    tap(chat, reply, "Undo")
    assert planner.changes == 2


def test_guided_dates_use_amsterdam_and_decline_stale_buttons():
    from tcc_app.travel_chat import TravelChat

    planner = Plans()
    planner.at = datetime(2026, 11, 21, 23, 30, tzinfo=UTC)
    chat = TravelChat(planner)
    tap(chat, chat.command("/plan"), "New plan")
    chat.text("RTD")
    chat.text("UT")
    date_buttons = chat.text("today")
    mode = tap(chat, date_buttons, "Once")
    assert tap(chat, date_buttons, "Repeating").done is False
    tap(chat, mode, "Exact train")
    reply = chat.text("08:12")
    tap(chat, reply, "No return")
    assert chat.draft["plan"]["start_date"] == "2026-11-22"


async def test_natural_language_uses_saved_defaults_and_keeps_the_clarification(tmp_path):
    from tcc_app.travel_actions import TravelActions
    from tcc_app.travel_chat import TravelChat
    from tcc_app.travel_language import LanguageSettings, TravelLanguage
    from tcc_app.travel_preferences import TravelPreferences

    class Planner(Plans):
        def preview_travel_plan(self, change):
            self.change = change
            return super().preview_travel_plan(change)

    planner = Planner()
    preferences = TravelPreferences(tmp_path / "preferences.json", planner.now)
    requests = []

    def agent(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={"question": "Which day?"})
        return httpx.Response(
            200,
            json={
                "change": {
                    "action": "create",
                    "preference_name": "Utrecht office",
                    "plan": {"start_date": "2026-11-22", "outbound": {}, "return": {}},
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(agent)) as client:
        language = TravelLanguage(
            LanguageSettings("agent", httpx.URL("http://test.local/planner")),
            client,
        )
        access = TravelActions(
            planner, {}, tmp_path / "access.json", preferences, language_identity="fixture"
        )
        chat = TravelChat(planner, preferences, language, access)
        remember = chat.memory_command("/remember", "Utrecht office | RTD | UT | 09:00 | leave")
        assert preferences.list()["preferences"] == []
        tap(chat, remember, "Remember")
        question = await chat.interpret("Plan a Utrecht office trip and back")
        assert question.text == "Which day?"
        reply = await chat.interpret("Tomorrow")
        assert "saved travel preference" in reply.text
        assert planner.changes == 0
        tap(chat, reply, "Always allow")
        assert planner.changes == 1
        assert planner.change["plan"]["outbound"] == {"mode": "window", "arrive_by": "09:00"}
        assert planner.change["plan"]["return"] == {"mode": "on_leave"}
        assert requests[-1]["context"]["conversation"][0]["text"] == (
            "Plan a Utrecht office trip and back"
        )
        await chat.interpret("Same again next week")
        assert planner.changes == 2


async def test_language_provider_cannot_cancel_a_plan_by_default(tmp_path):
    from tcc_app.travel_actions import TravelActions
    from tcc_app.travel_chat import TravelChat
    from tcc_app.travel_language import LanguageSettings, TravelLanguage
    from tcc_app.travel_preferences import TravelPreferences

    planner = Plans()
    prefs = TravelPreferences(tmp_path / "preferences.json", planner.now)
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={"change": {"action": "cancel", "plan_id": "travel-existing"}},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        language = TravelLanguage(
            LanguageSettings("agent", httpx.URL("https://example.test")), client
        )
        access = TravelActions(
            planner, {}, tmp_path / "access.json", prefs, language_identity="fixture"
        )
        chat = TravelChat(planner, prefs, language, access)
        reply = await chat.interpret("What is my Utrecht plan?")
        assert planner.changes == 0
        tap(chat, reply, "Deny")
        assert planner.changes == 0


def test_guided_plan_can_reuse_a_saved_preference_with_no_ai(tmp_path):
    from tcc_app.travel_chat import TravelChat
    from tcc_app.travel_preferences import TravelPreferences

    class Planner(Plans):
        def preview_travel_plan(self, change):
            self.change = change
            return super().preview_travel_plan(change)

    planner = Planner()
    chat = TravelChat(planner, TravelPreferences(tmp_path / "preferences.json", planner.now))
    remember = chat.memory_command("/remember", "Utrecht office | RTD | UT | 09:00 | leave")
    tap(chat, remember, "Remember")
    presets = tap(chat, chat.command("/plan"), "Use a saved preference")
    tap(chat, presets, "Utrecht office")
    dates = chat.text("tomorrow")
    timing = tap(chat, dates, "Once")
    back = tap(chat, timing, "Use saved arrival by 09:00")
    preview = tap(chat, back, "Use saved return when I leave")
    tap(chat, preview, "Save plan")
    assert planner.changes == 1
    assert planner.change["plan"] == {
        "name": "Utrecht office",
        "origin": "RTD",
        "destination": "UT",
        "start_date": "2026-11-22",
        "outbound": {"mode": "window", "arrive_by": "09:00"},
        "return": {"mode": "on_leave"},
    }
