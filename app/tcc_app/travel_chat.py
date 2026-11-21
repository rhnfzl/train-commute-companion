"""Guided travel planning for the private Telegram chat."""

import re
import secrets
from collections import deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from train_commute_companion.calendar import TZ
from train_commute_companion.recurrence import MAX_TRIPS

from .telegram import callback_data
from .travel_actions import LANGUAGE_SOURCE, TravelActions, TravelRuntime
from .travel_language import LanguageError, TravelLanguage
from .travel_preferences import TravelPreferences


@dataclass(frozen=True, slots=True)
class TravelReply:
    text: str
    markup: dict[str, Any] | None = None
    done: bool = False


def buttons(*rows: Sequence[tuple[str, str]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": label, "callback_data": data}
                for label, data in row
                if callback_data(*data.split("|")) is not None
            ]
            for row in rows
        ]
    }


def plan_receipt(reply: Mapping[str, Any]) -> TravelReply:
    undo = reply.get("undo_id")
    markup = buttons((("Undo", f"tp|undo|{undo}"),)) if isinstance(undo, str) else None
    return TravelReply(reply["text"], markup, reply["status"] not in ("retry", "invalid"))


WEEKDAYS = {day: i for i, day in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"), 1)}
TEXT_STEPS = {
    "origin": "Which station are you leaving from? Enter the station name or code.",
    "destination": "Which station are you travelling to?",
    "start_date": "When does this plan start? Enter today, tomorrow, or YYYY-MM-DD.",
    "weekdays": "Which days? Enter names such as Tue Thu.",
    "interval_weeks": "Repeat every how many weeks? Enter 1 for weekly or 2 for every other week.",
    "monthly": "Which weekday of each month? Enter a number from 1 to 5 and a day, such as 1 Mon.",
    "end_date": "Enter a last date (YYYY-MM-DD), or choose Ongoing or After N trips.",
    "end_after": f"How many trips should this plan include? Enter 1 to {MAX_TRIPS}.",
    "skip_dates": "Enter dates to skip, separated by spaces, or tap No skips.",
}
DRAFT_TTL = timedelta(hours=1)
PAGE_SIZE = 6
CHANGE_SCHEMA = {
    "create_or_edit": {
        "action": "create or edit (edit requires a listed plan_id)",
        "preference_name": "optional exact saved travel preference name",
        "plan": {
            "origin": "station code or name",
            "destination": "station code or name",
            "name": "optional short label",
            "start_date": "YYYY-MM-DD in Europe/Amsterdam",
            "end_date": "optional YYYY-MM-DD or null (ongoing recurrence)",
            "end_after": "optional number of trips, instead of an end_date",
            "weekdays": "optional ISO weekday numbers 1 to 7 for weekly recurrence",
            "interval_weeks": "optional integer, 1 for weekly, 2 for every other week",
            "monthly": "optional {ordinal: 1 to 5, weekday: ISO number 1 to 7}, instead of weekdays",
            "skip_dates": "optional array of dates to skip",
            "profile_id": "optional existing profile ID to inherit preferences from",
            "outbound": (
                "{mode: exact, departure: HH:MM, train_number?: string} or "
                "{mode: window or on_leave, earliest?: HH:MM, latest?: HH:MM, arrive_by?: HH:MM}"
            ),
            "return": (
                "omit for no return or use the outbound shape. Specify day_offset:1 for "
                "next-day return. An empty object requests the saved return preference."
            ),
        },
    },
    "cancel": {"action": "cancel", "plan_id": "listed ID"},
    "list_plans": {"action": "list"},
    "list_preferences": {"action": "list_preferences"},
    "save_preference": {
        "action": "save_preference",
        "preference": "{name, origin, destination, arrival_by?: HH:MM, return_on_leave?: boolean}",
    },
    "forget_preference": {"action": "forget_preference", "name": "exact saved name"},
}


@dataclass(frozen=True, slots=True)
class MemoryReview:
    id: str
    change: dict[str, Any]
    token: str
    expires: datetime
    suggestion: bool


class TravelChat:
    def __init__(
        self,
        rt: TravelRuntime,
        preferences: TravelPreferences | None = None,
        language: TravelLanguage | None = None,
        access: TravelActions | None = None,
    ) -> None:
        self.rt = rt
        self._preferences, self._language = preferences, language
        self._access = access
        self.draft: dict[str, Any] = {}
        self._step = ""
        self._nonce = ""
        self._until = self.rt.now()
        self._review = ""
        self._trip_mode = ""
        self._return_offset = 0
        self._memory: MemoryReview | None = None
        self._conversation: deque[dict[str, str]] = deque(maxlen=4)
        self._followup_until = self.rt.now()
        self._plan_saved = False
        self._language_plan = False
        self._preset: dict[str, Any] = {}
        self._presets: list[dict[str, Any]] = []

    @property
    def active(self) -> bool:
        return self._step != "" and self.rt.now() < self._until

    def _ask(
        self, step: str, text: str, *choices: tuple[str, str], close: str = "Close"
    ) -> TravelReply:
        self._step = step
        self._until = self.rt.now() + DRAFT_TTL
        self._nonce = secrets.token_hex(12)
        rows = [[(label, f"tp|{self._nonce}|{value}")] for label, value in choices]
        rows.append([(close, f"tp|{self._nonce}|close")])
        return TravelReply(text, buttons(*rows), done=True)

    def _ask_text(self, step: str) -> TravelReply:
        choices = {
            "end_date": (("Ongoing", "ongoing"), ("After N trips", "count")),
            "skip_dates": (("No skips", "none"),),
        }
        return self._ask(step, TEXT_STEPS[step], *choices.get(step, ()))

    def command(self, word: str) -> TravelReply:
        if word == "/plans":
            return self._list()
        self._step = ""
        self._conversation.clear()
        rows = [[("New plan", "tp|new"), ("Saved plans", "tp|list")]]
        if self._saved_preferences():
            rows.append([("Use a saved preference", "tp|presets")])
        return TravelReply(
            "Plan train travel for one date or a repeating schedule.",
            buttons(*rows),
        )

    def _list(self, page: int = 0) -> TravelReply:
        plans: list[dict[str, Any]] = self.rt.list_travel_plans()["plans"]
        if not plans:
            return TravelReply(
                "You have no saved travel plans.", buttons((("New plan", "tp|new"),))
            )
        page = min(page, (len(plans) - 1) // PAGE_SIZE)
        first = page * PAGE_SIZE
        rows: list[list[tuple[str, str]]] = []
        for plan in plans[first : first + PAGE_SIZE]:
            plan_id = plan.get("plan_id", plan.get("id"))
            label = plan.get("name") or plan.get("destination") or "Trip"
            origin = plan.get("origin")
            if origin and plan.get("destination") == label:
                label = f"{origin} to {label}"
            rows.append([(str(label)[:36], f"tp|open|{plan_id}")])
        pages = []
        if page:
            pages.append(("Previous", f"tp|page|{page - 1}"))
        if first + PAGE_SIZE < len(plans):
            pages.append(("Next", f"tp|page|{page + 1}"))
        if pages:
            rows.append(pages)
        rows.append([("New plan", "tp|new")])
        return TravelReply("Saved plans. Choose one to view or edit it.", buttons(*rows))

    def _lookup(self, plan_id: str) -> dict[str, Any] | None:
        return next(
            (
                plan
                for plan in self.rt.list_travel_plans()["plans"]
                if plan.get("plan_id", plan.get("id")) == plan_id
            ),
            None,
        )

    def _open(self, plan_id: str) -> TravelReply:
        plan = self._lookup(plan_id)
        if plan is None:
            return TravelReply("That plan is no longer saved.")
        text = plan.get("text") or (
            f"{plan.get('name') or 'Travel plan'}\n"
            f"{plan.get('origin')} to {plan.get('destination')}\n"
            f"Starts {plan.get('start_date')}"
        )
        return TravelReply(
            text,
            buttons((("Edit", f"tp|edit|{plan_id}"), ("Cancel plan", f"tp|cancel|{plan_id}"))),
        )

    def _new(self, plan_id: str | None = None) -> TravelReply:
        old = self._lookup(plan_id) if plan_id else None
        if plan_id and old is None:
            return TravelReply("That plan is no longer saved.")
        self.draft = {"action": "edit" if plan_id else "create", "plan": {}}
        self._preset = {}
        if plan_id and old:
            self.draft["plan_id"] = plan_id
            for key in ("name", "profile_id"):
                if old.get(key):
                    self.draft["plan"][key] = old[key]
        return self._ask_text("origin")

    def _day(self, value: str) -> str:
        today = self.rt.now().astimezone(TZ).date()
        if value.casefold() in ("today", "tomorrow"):
            return (today + timedelta(days=int(value.casefold() == "tomorrow"))).isoformat()
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value).isoformat()

    def _mode(self) -> TravelReply:
        preferred = self._preset.get("arrival_by")
        defaults = [(f"Use saved arrival by {preferred}", "pref")] if preferred else []
        return self._ask(
            "mode",
            "How should I pick the outbound train?",
            *defaults,
            ("Exact train", "exact"),
            ("Time window", "window"),
            ("Arrival deadline", "deadline"),
            ("When I leave", "on_leave"),
        )

    def _return_day(self) -> TravelReply:
        defaults = (
            [("Use saved return when I leave", "pref")]
            if self._preset.get("return_on_leave") is True
            else []
        )
        return self._ask(
            "return_day",
            "Do you want to follow a return train?",
            *defaults,
            ("No return", "none"),
            ("Same day", "same"),
            ("Next day", "next"),
        )

    def _trip(self, mode: str, *, returns: bool) -> TravelReply:
        self._trip_mode = mode
        return self._ask(
            "return_trip" if returns else "outbound_trip",
            {
                "exact": "Enter the planned departure, like 08:12. Add its train number if known.",
                "window": "Enter earliest and latest departure, like 07:00 08:15. "
                "You can add an arrival deadline as a third time.",
                "deadline": "When do you need to arrive? Enter HH:MM.",
                "on_leave": "Enter an optional departure window, like 16:00 22:00, "
                "or tap Any time.",
            }[mode],
            *([("Any time", "any")] if mode == "on_leave" else []),
        )

    @staticmethod
    def _time(value: str) -> str:
        if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
            raise ValueError
        return time.fromisoformat(value).isoformat(timespec="minutes")

    def _parse_trip(self, value: str) -> dict[str, Any]:
        args = value.split()
        mode = self._trip_mode
        if mode == "exact" and 1 <= len(args) <= 2:
            return {
                "mode": mode,
                "departure": self._time(args[0]),
                **({"train_number": args[1]} if len(args) == 2 else {}),
            }
        if mode == "deadline" and len(args) == 1:
            return {"mode": "window", "arrive_by": self._time(args[0])}
        if mode == "on_leave" and value == "any":
            return {"mode": mode}
        if mode in ("window", "on_leave") and 2 <= len(args) <= 3:
            trip = {"mode": mode, "earliest": self._time(args[0]), "latest": self._time(args[1])}
            if len(args) == 3:
                trip["arrive_by"] = self._time(args[2])
            return trip
        raise ValueError

    def text(self, value: str) -> TravelReply | None:
        if not self.active:
            return None
        value = value.strip()
        plan = self.draft.get("plan", {})
        step = self._step
        if step == "conflict" and value.casefold() in ("replace", "keep", "keep both"):
            return self._answer("replace" if value.casefold() == "replace" else "keep")
        try:
            if step in ("origin", "destination"):
                if not 1 <= len(value) <= 120 or any(not ch.isprintable() for ch in value):
                    raise ValueError
                plan[step] = value
                return self._ask_text("destination" if step == "origin" else "start_date")
            if step == "start_date":
                plan[step] = self._day(value)
                return self._ask(
                    "repeat",
                    "When should this plan repeat?",
                    ("Once", "once"),
                    ("Repeating", "repeat"),
                    ("Monthly", "monthly"),
                )
            if step == "weekdays":
                tokens = value.replace(",", " ").lower().split()
                if not tokens or any(token[:3] not in WEEKDAYS for token in tokens):
                    raise ValueError
                plan[step] = sorted({WEEKDAYS[token[:3]] for token in tokens})
                return self._ask_text("interval_weeks")
            if step == "interval_weeks":
                plan[step] = int(value)
                if not 1 <= plan[step] <= 52:
                    raise ValueError
                return self._ask_text("end_date")
            if step == "monthly":
                match = re.fullmatch(r"([1-5])\s+(\w+)", value.casefold())
                if match is None or match[2][:3] not in WEEKDAYS:
                    raise ValueError
                plan[step] = {"ordinal": int(match[1]), "weekday": WEEKDAYS[match[2][:3]]}
                return self._ask_text("end_date")
            if step == "end_after":
                count = int(value)
                if not 1 <= count <= MAX_TRIPS:
                    raise ValueError
                plan[step] = count
                return self._ask_text("skip_dates")
            if step == "end_date":
                plan[step] = None if value.casefold() == "ongoing" else self._day(value)
                return self._ask_text("skip_dates")
            if step == "skip_dates":
                tokens = value.replace(",", " ").split()
                plan[step] = [] if value.casefold() == "none" else [self._day(v) for v in tokens]
                return self._mode()
            if step in ("outbound_trip", "return_trip"):
                trip = self._parse_trip(value)
                if step == "outbound_trip":
                    plan["outbound"] = trip
                    return self._return_day()
                plan["return"] = {**trip, "day_offset": self._return_offset}
                return self.propose(self.draft)
        except ValueError:
            return TravelReply("I couldn't use that answer. Try again or tap Close.")
        return TravelReply("Choose one of the buttons on the last message.")

    def _language_policy(self) -> str:
        return self._access.language_policy() if self._access else "ask"

    def propose(
        self, change: dict[str, Any], *, automatic: bool = False, language: bool = False
    ) -> TravelReply:
        self.draft = deepcopy(change)
        self._language_plan = language
        if language and self._language_policy() == "deny":
            self._step = ""
            return TravelReply("Telegram language can read plans. Changes are blocked in /agents.")
        review = self.rt.preview_travel_plan(self.draft)
        if review["status"] == "conflict":
            choices = [("Replace overlapping trips", "replace")]
            if review.get("can_keep", True):
                choices.append(("Keep both", "keep"))
            return self._ask("conflict", review["text"], *choices)
        if review["status"] != "ready":
            self._step = ""
            return TravelReply(review["text"], done=True)
        self._review = review["review_token"]
        if automatic or language and self._language_policy() == "always":
            return self._save(secrets.token_hex(24))
        if language:
            return self._ask(
                "save",
                f"Telegram language proposes this change.\n{review['text']}",
                ("Allow once", "save"),
                ("Always allow", "always"),
                close="Deny",
            )
        return self._ask("save", review["text"], ("Save plan", "save"))

    def _save(self, request_id: str, *, always: bool = False) -> TravelReply:
        if self._language_plan and self._language_policy() == "deny":
            self._step = ""
            return TravelReply("Telegram language changes are blocked in /agents.", done=True)
        result = self.rt.save_travel_plan(self.draft, self._review, request_id)
        self._plan_saved = result.get("status") == "saved" and self.draft.get("action") in (
            "create",
            "edit",
        )
        if self._language_plan and always and result["status"] == "saved" and self._access:
            policy = self._access.set_policy(LANGUAGE_SOURCE, "always")
            result = {**result, "text": f"{result['text']}\n{policy['text']}"}
        reply = plan_receipt(result)
        if reply.done:
            self._step = ""
        return reply

    def button(self, data: str) -> TravelReply:
        tag, *parts = data.split("|")
        if tag == "tm" and len(parts) == 2:
            return self._answer_memory(*parts)
        if parts == ["new"]:
            return self._new()
        if parts == ["list"]:
            return self._list()
        if parts == ["presets"]:
            self._presets = self._saved_preferences()
            return self._ask(
                "preset",
                "Which saved travel preference should I use?",
                *((item["name"], str(index)) for index, item in enumerate(self._presets)),
            )
        if len(parts) == 2:
            key, value = parts
            if key == "open":
                return self._open(value)
            if key == "page" and re.fullmatch(r"[0-9]{1,5}", value):
                return self._list(int(value))
            if key == "edit":
                return self._new(value)
            if key == "cancel":
                return self.propose({"action": "cancel", "plan_id": value})
            if key == "undo":
                return self.propose({"action": "undo", "undo_id": value}, automatic=True)
            if self.active and key == self._nonce:
                return self._answer(value)
        return TravelReply("This button is no longer active. Open /plan to start again.")

    def _answer(self, value: str) -> TravelReply:
        step = self._step
        if value == "close":
            self._step = ""
            return TravelReply("Closed. No change saved.", done=True)
        if step == "repeat":
            if value == "repeat":
                return self._ask_text("weekdays")
            if value == "monthly":
                return self._ask_text("monthly")
            if value == "once":
                return self._mode()
        if step == "end_date" and value == "count":
            return self._ask_text("end_after")
        if step == "preset" and re.fullmatch(r"[0-9]{1,6}", value):
            index = int(value)
            if index < len(self._presets):
                self._preset = self._presets[index]
                self._presets = []
                self.draft = {
                    "action": "create",
                    "plan": {key: self._preset[key] for key in ("name", "origin", "destination")},
                }
                return self._ask_text("start_date")
        if step in ("end_date", "skip_dates", "outbound_trip", "return_trip"):
            reply = self.text(value)
            assert reply is not None
            return reply
        if step in ("mode", "return_mode") and value in ("exact", "window", "deadline", "on_leave"):
            return self._trip(value, returns=step == "return_mode")
        if step == "mode" and value == "pref" and self._preset.get("arrival_by"):
            self.draft["plan"]["outbound"] = {
                "mode": "window",
                "arrive_by": self._preset["arrival_by"],
            }
            return self._return_day()
        if step == "return_day" and value == "pref" and self._preset.get("return_on_leave"):
            self.draft["plan"]["return"] = {"mode": "on_leave"}
            return self.propose(self.draft)
        if step == "return_day" and value in ("none", "same", "next"):
            if value == "none":
                return self.propose(self.draft)
            self._return_offset = int(value == "next")
            return self._ask(
                "return_mode",
                "How should I pick the return train?",
                ("Exact train", "exact"),
                ("Time window", "window"),
                ("When I leave", "on_leave"),
            )
        if step == "save" and (value == "save" or value == "always" and self._language_plan):
            return self._save(self._nonce, always=value == "always")
        if step == "conflict" and value in ("replace", "keep"):
            return self.propose(
                {**self.draft, "conflict_policy": value}, language=self._language_plan
            )
        return TravelReply("Choose one of the buttons on the last message.")

    def _saved_preferences(self) -> list[dict[str, Any]]:
        prefs = self._preferences
        if prefs is None:
            return []
        items: list[dict[str, Any]] = prefs.list().get("preferences", [])
        return items

    async def interpret(self, text: str) -> TravelReply | None:
        language, prefs = self._language, self._preferences
        if language is None or prefs is None:
            return None
        now = self.rt.now().astimezone(TZ)
        if now >= self._followup_until:
            self._conversation.clear()
        plans = self.rt.list_travel_plans().get("plans", [])
        refs = (
            "plan_id",
            "name",
            "origin",
            "destination",
            "start_date",
            "end_date",
            "end_after",
            "weekdays",
            "interval_weeks",
            "monthly",
            "skip_dates",
            "outbound",
            "return",
            "profile_id",
        )
        try:
            intent = await language.interpret(
                text,
                context={
                    "today": now.date().isoformat(),
                    "timezone": str(TZ),
                    "conversation": list(self._conversation),
                    "preferences": prefs.list().get("preferences", [])[:12],
                    "plans": [{k: p[k] for k in refs if k in p} for p in plans[-8:]],
                },
                change_schema=CHANGE_SCHEMA,
            )
        except LanguageError as error:
            return TravelReply(str(error))
        if intent.question is not None:
            self._conversation.extend(
                (
                    {"role": "traveller", "text": text},
                    {"role": "assistant", "text": intent.question},
                )
            )
            self._followup_until = now + timedelta(minutes=15)
            return TravelReply(intent.question)
        self._conversation.clear()
        assert intent.change is not None
        change = intent.change
        action = change.get("action")
        if action == "list":
            return self._list()
        if action == "list_preferences":
            return self.memory_command("/preferences", "")
        if self._language_policy() == "deny":
            return TravelReply("Telegram language changes are blocked in /agents.")
        if action == "save_preference":
            return self._review_memory({"action": "save", "preference": change.get("preference")})
        if action == "forget_preference":
            return self._review_memory({"action": "forget", "name": change.get("name")})
        before = {
            k: v for k, v in change.items() if k not in ("conflict_policy", "preference_name")
        }
        with_defaults = prefs.with_defaults(
            {k: v for k, v in change.items() if k != "conflict_policy"}
        )
        reply = self.propose(with_defaults, language=True)
        if with_defaults != before:
            return TravelReply(
                f"{reply.text}\nI used a saved travel preference.",
                reply.markup,
                reply.done,
            )
        return reply

    def memory_command(self, command: str, value: str) -> TravelReply:
        if self._preferences is None:
            return TravelReply("Travel preferences are unavailable.")
        if command == "/forgetpref":
            return self._review_memory({"action": "forget", "name": value.strip()})
        if command == "/remember":
            parts = [part.strip() for part in value.split("|")]
            if len(parts) != 5 or parts[4].lower() not in ("leave", "-"):
                return TravelReply(
                    "Use /remember NAME | ORIGIN | DESTINATION | HH:MM or - | leave or -"
                )
            name, origin, destination, arrival, back = parts
            preference: dict[str, Any] = {
                "name": name,
                "origin": origin,
                "destination": destination,
            }
            if arrival != "-":
                preference["arrival_by"] = arrival
            if back.lower() == "leave":
                preference["return_on_leave"] = True
            return self._review_memory({"action": "save", "preference": preference})
        result = self._preferences.list()
        if result.get("status") != "ok":
            return TravelReply(result["text"])
        lines = []
        for item in result["preferences"]:
            line = f"{item['name']}: {item['origin']} to {item['destination']}"
            if "arrival_by" in item:
                line += f", arrive by {item['arrival_by']}"
            if "return_on_leave" in item:
                line += (
                    ", return when you leave"
                    if item["return_on_leave"]
                    else ", choose return ahead"
                )
            lines.append(line)
        return TravelReply("\n".join(lines) or "You have no saved travel preferences.")

    def _review_memory(self, change: dict[str, Any], suggestion: str | None = None) -> TravelReply:
        assert self._preferences is not None
        preview = self._preferences.preview(change)
        if preview["status"] != "ready":
            return TravelReply(preview["text"])
        key = secrets.token_hex(12)
        self._memory = MemoryReview(
            key,
            change,
            preview["review_token"],
            self.rt.now() + timedelta(minutes=10),
            suggestion is not None,
        )
        label = "Forget" if change["action"] == "forget" else "Remember"
        return TravelReply(
            suggestion or preview["text"],
            buttons(
                ((label, f"tm|{key}|save"), ("No thanks", f"tm|{key}|no")),
            ),
        )

    def _answer_memory(self, key: str, choice: str) -> TravelReply:
        prefs, review = self._preferences, self._memory
        if prefs is None or review is None or review.id != key or self.rt.now() >= review.expires:
            return TravelReply("That preference prompt has expired.")
        if choice == "no":
            self._memory = None
            if review.suggestion:
                prefs.dismiss(review.change)
            return TravelReply("Preference left as it was.", done=True)
        if choice != "save":
            return TravelReply("Choose one of the buttons on the preference prompt.")
        reply = plan_receipt(prefs.save(review.change, review.token, review.id))
        if reply.done:
            self._memory = None
        return reply

    def memory_suggestion(self, *, check: bool = False) -> TravelReply | None:
        saved, self._plan_saved = self._plan_saved, False
        if not (saved or check) or self._preferences is None:
            return None
        if self._memory is not None and self.rt.now() < self._memory.expires:
            return None
        suggestion = self._preferences.suggest(self.rt.list_travel_plans()["plans"])
        if suggestion is None:
            return None
        return self._review_memory(suggestion["change"], suggestion["text"])

    def suggestion_delivered(self) -> None:
        review, prefs = self._memory, self._preferences
        if prefs is not None and review is not None and review.suggestion:
            prefs.dismiss(review.change)
