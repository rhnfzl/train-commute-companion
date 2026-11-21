"""Build and preview a first commute in the owner's private Telegram chat."""

import asyncio
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from pathlib import Path
from secrets import token_urlsafe
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import uuid4

import yaml

from train_commute_companion.advice import is_unrideable
from train_commute_companion.calendar import TZ, WEEKDAYS, is_commute_day, resolve_local, to_utc
from train_commute_companion.model import Connection, Stations
from train_commute_companion.profile import check_lines
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.runtime import Runtime
from train_commute_companion.transfer import FALLBACK_MIN

from .telegram import callback_data

LIFETIME = timedelta(minutes=15)
MANUAL = "This profile file is already in use. Edit it on the Docker host."
OUTBOUND_BUFFER, RETURN_BUFFER = 10, 5


@dataclass(frozen=True, slots=True)
class SetupReply:
    text: str
    markup: dict[str, Any] | None = None
    done: bool = False


@dataclass(slots=True)
class Draft:
    step: str = "home"
    home: str = ""
    work: str = ""
    days: set[int] = field(default_factory=lambda: {1, 2, 3, 4, 5})
    out: time = time(8)
    back: time = time(17)
    lead_out: int | None = None
    lead_back: int | None = None
    delay_min: int = 3
    step_min: int = 3
    trips: tuple[Connection, Connection] | None = None
    day: date | None = None
    document: str | None = None


def _departure(trip: Connection) -> str:
    return trip.legs[0].train.planned_departure.astimezone(TZ).strftime("%H:%M")


def _pick(answer: ProviderAnswer) -> Connection | None:
    q = answer.query
    start = to_utc(q.at)
    eligible = (
        trip
        for trip in answer.connections
        if trip.legs
        and trip.origin == q.origin
        and trip.destination == q.destination
        and trip.legs[0].train.planned_departure.astimezone(TZ).date() == q.at.astimezone(TZ).date()
        and start <= to_utc(trip.legs[0].train.planned_departure) < start + timedelta(hours=1)
        and not is_unrideable(trip, answer, dict.fromkeys(trip.transfer_stations, FALLBACK_MIN))
    )
    return min(
        eligible, key=lambda trip: to_utc(trip.legs[0].train.planned_departure), default=None
    )


def _write_new(path: Path, content: str) -> None:
    temp: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=path.parent, mode="w", encoding="utf-8", delete=False
        ) as handle:
            temp = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Another editor can create the file while the preview is open.
        os.link(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _document(draft: Draft, out: Connection, back: Connection, today: date, first_day: date) -> str:
    trips: dict[str, dict[str, Any]] = {}
    for label, trip, buffer, lead in (
        ("outbound", out, OUTBOUND_BUFFER, draft.lead_out),
        ("return", back, RETURN_BUFFER, draft.lead_back),
    ):
        trips[label] = {
            "first_train": _departure(trip),
            "transfers": list(trip.transfer_stations),
            "buffer_min": buffer,
        }
        if lead is not None:
            trips[label]["active_lead_min"] = lead
    return yaml.safe_dump(
        {
            "profiles": [
                {
                    "id": uuid4().hex,
                    "name": "commute",
                    "days": [WEEKDAYS[i - 1] for i in sorted(draft.days)],
                    "skip_public_holidays": True,
                    "pauses": [
                        {
                            "start": today.isoformat(),
                            "end": (first_day - timedelta(days=1)).isoformat(),
                        }
                    ]
                    if today < first_day
                    else [],
                    "home_station": draft.home,
                    "work_station": draft.work,
                    "transfer_min": {
                        station: FALLBACK_MIN
                        for station in (*out.transfer_stations, *back.transfer_stations)
                    },
                    **trips,
                    "alerts": {
                        "delay_min": draft.delay_min,
                        "delay_step_min": draft.step_min,
                    },
                }
            ]
        },
        sort_keys=False,
    )


class SetupWizard:
    def __init__(self, runtime: Runtime, path: Path) -> None:
        self._rt, self._path = runtime, path
        self._lock = asyncio.Lock()
        self._draft: Draft | None = None
        self._nonce = ""
        self._choices: set[str] = set()
        self._opened = runtime.now()

    @property
    def pending(self) -> bool:
        return self._draft is not None

    def _active(self) -> bool:
        return self.pending and to_utc(self._rt.now()) - to_utc(self._opened) < LIFETIME

    async def begin(self) -> SetupReply:
        async with self._lock:
            if self._path.exists() or self._path.is_symlink() or self._rt.config:
                self._draft = None
                return SetupReply(MANUAL)
            if not self._rt.known_stations:
                return SetupReply("The NS station list is unavailable. Try /setup again shortly.")
            self._draft = Draft()
            self._opened = self._rt.now()
            return self._screen("Type your home station name or NS station code.")

    async def cancel(self) -> SetupReply:
        async with self._lock:
            self._draft = None
            return SetupReply("Setup closed. Send /setup if you want to start again.")

    def _screen(
        self, text: str, choices: Sequence[tuple[str, str]] = (), *, done: bool = False
    ) -> SetupReply:
        self._nonce = token_urlsafe(8)
        self._choices.clear()
        rows: list[list[dict[str, str]]] = []
        for label, value in choices:
            data = callback_data("setup", self._nonce, value)
            if data is not None:
                rows.append([{"text": label, "callback_data": data}])
                self._choices.add(value)
        return SetupReply(text, {"inline_keyboard": rows} if rows else None, done)

    async def button(self, nonce: str, value: str) -> SetupReply:
        async with self._lock:
            if not self._active() or nonce != self._nonce or value not in self._choices:
                return SetupReply("This setup button has expired. Use the latest prompt or /setup.")
            return await self._advance(value, clicked=True)

    async def text(self, text: str) -> SetupReply:
        async with self._lock:
            if not self._active():
                return SetupReply("The setup session has ended. Send /setup or /cancel_setup.")
            return await self._advance(text.strip(), clicked=False)

    def _stations(self) -> Stations:
        stations = self._rt.known_stations
        return (
            stations
            if isinstance(stations, Stations)
            else Stations({c: (c,) for c in stations or ()})
        )

    def _station(self, draft: Draft, value: str, clicked: bool) -> SetupReply:
        if len(value) > 128:
            return SetupReply("Type a shorter station name or code.")
        stations = self._stations()
        choices = stations.lookup(value.upper()) or stations.lookup(value)
        if clicked and len(choices) == 1:
            station = choices[0]
            if draft.step == "home":
                draft.home, draft.step = station, "work"
                return self._screen("Type your work station name or NS station code.", done=True)
            if station == draft.home:
                return SetupReply("Pick a different station for work.")
            draft.work, draft.step = station, "days"
            return self._days(draft, done=True)
        if not choices and (suggested := stations.closest(value)):
            choices = stations.lookup(suggested)
        if not choices:
            return SetupReply("No matching NS station. Type a station name or code.")
        return self._screen(
            "Choose the station.",
            [(f"{stations.name(c)} ({c})", c) for c in choices[:8]],
        )

    def _days(self, draft: Draft, *, done: bool) -> SetupReply:
        choices = [
            (f"{day.title()} {'on' if i in draft.days else 'off'}", str(i))
            for i, day in enumerate(WEEKDAYS, 1)
        ]
        return self._screen(
            "Choose your commute days. Or try the sample schedule, weekdays at 08:00 and 17:00.",
            [*choices, ("Set times", "next"), ("Make this mine", "sample")],
            done=done,
        )

    def _time(self, direction: str) -> SetupReply:
        options = ("07:00", "08:00", "09:00") if direction == "out" else ("16:00", "17:00", "18:00")
        return self._screen(
            f"Choose the {'outbound' if direction == 'out' else 'return'} departure or type HH:MM.",
            [(t, t) for t in options],
            done=True,
        )

    async def _advance(self, value: str, *, clicked: bool) -> SetupReply:
        draft = self._draft
        assert draft is not None
        if draft.step in {"home", "work"}:
            return self._station(draft, value, clicked)
        if draft.step == "days":
            if not clicked:
                return SetupReply("Use the day's button to turn it on or off.")
            if value == "sample":
                draft.days = {1, 2, 3, 4, 5}
                return await self._probe(draft)
            if value == "next":
                if not draft.days:
                    return SetupReply("Choose at least one commute day.")
                draft.step = "out"
                return self._time("out")
            draft.days.symmetric_difference_update({int(value)})
            return self._days(draft, done=True)
        if draft.step in {"out", "back"}:
            if re.fullmatch(r"(?:0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]", value) is None:
                return SetupReply("Type a departure time in HH:MM, for example 08:15.")
            when = time.fromisoformat(value)
            if draft.step == "out":
                draft.out, draft.step = when, "back"
                return self._time("back")
            if when <= draft.out:
                return SetupReply("Choose a return time after your outbound time.")
            draft.back = when
            return await self._probe(draft)
        if draft.step == "retry" and clicked:
            if value == "times":
                draft.step = "out"
                return self._time("out")
            return await self._probe(draft)
        if draft.step == "tune" and not clicked:
            return self._tune(draft, value)
        if draft.step == "review" and clicked:
            if value == "tune":
                draft.step = "tune"
                return self._screen(
                    "Type four values in minutes: outbound check-in, return check-in, "
                    "first delay, repeat delay. Use auto for the default check-in.\n"
                    "Example: auto auto 3 3.",
                    done=True,
                )
            return await self._save(draft)
        return SetupReply("Use the buttons in the latest setup message.")

    async def _probe(self, draft: Draft) -> SetupReply:
        today = self._rt.now().astimezone(TZ).date()
        days = frozenset(draft.days)
        day = today + timedelta(days=1)
        while not is_commute_day(day, days, frozenset(), True):
            day += timedelta(days=1)
        answers = await self._rt.preview_setup(
            Query(draft.home, draft.work, resolve_local(day, draft.out, edge="start")),
            Query(draft.work, draft.home, resolve_local(day, draft.back, edge="start")),
        )
        out, back = (_pick(a) for a in answers) if len(answers) == 2 else (None, None)
        if out is None or back is None:
            return self._retry(
                draft,
                "NS did not return a usable train for both trips within an hour of your times.",
            )
        alight = out.legs[-1].alight
        reported = None if alight.arrival is None else alight.arrival.value
        arrivals = [
            to_utc(value) for value in (reported, alight.planned_arrival) if value is not None
        ]
        if not arrivals:
            return self._retry(draft, "NS did not report the outbound arrival time.")
        board = back.legs[0].board
        reported = None if board.departure is None else board.departure.value
        departures = [to_utc(back.legs[0].train.planned_departure)]
        if reported is not None:
            departures.append(to_utc(reported))
        if min(departures) <= max(arrivals):
            return self._retry(
                draft, "Choose a return train that leaves after the outbound arrival."
            )
        draft.day, draft.trips = day, (out, back)
        return self._review(draft)

    def _retry(self, draft: Draft, note: str) -> SetupReply:
        draft.step = "retry"
        return self._screen(
            note, [("Retry preview", "retry"), ("Change times", "times")], done=True
        )

    def _tune(self, draft: Draft, value: str) -> SetupReply:
        if not re.fullmatch(
            r"(?:auto|[0-9]{1,4}) (?:auto|[0-9]{1,4}) [0-9]{1,4} [0-9]{1,4}", value
        ):
            return SetupReply("Type four values, for example auto auto 3 3.")
        a, b, delay, step = value.split()
        lead_out, lead_back = (None if v == "auto" else int(v) for v in (a, b))
        d, s = int(delay), int(step)
        if (lead_out is not None and lead_out < OUTBOUND_BUFFER) or (
            lead_back is not None and lead_back < RETURN_BUFFER
        ):
            return SetupReply("Set check-in to at least 10 minutes outbound and 5 minutes return.")
        if not 1 <= d <= 1440 or not 1 <= s <= 1440:
            return SetupReply("Set each delay threshold from 1 to 1440 minutes.")
        draft.lead_out, draft.lead_back, draft.delay_min, draft.step_min = lead_out, lead_back, d, s
        return self._review(draft)

    def _review(self, draft: Draft) -> SetupReply:
        assert draft.trips is not None and draft.day is not None
        out, back = draft.trips
        today = self._rt.now().astimezone(TZ).date()
        text = _document(draft, out, back, today, draft.day)
        cfg, problems = check_lines(text, self._rt.known_stations, today)
        if cfg is None or any(not message.startswith("warning: ") for message in problems):
            return SetupReply("Check your setup settings.\n" + "\n".join(problems))
        draft.document, draft.step = text, "review"
        lines = [f"Next commute: {draft.day:%a %d %b}"]
        for trip in (out, back):
            board = trip.legs[0].board
            platform = board.actual_track or board.planned_track
            route = f"{board.name} to {trip.legs[-1].alight.name}"
            changes = ", ".join(leg.alight.name for leg in trip.legs[:-1])
            lines.append(
                f"{_departure(trip)} {route}"
                + (f", platform {platform}" if platform else "")
                + (f", change at {changes}" if changes else ", direct")
            )
        lines.append("Data: NS")
        return self._screen(
            "\n".join([*lines, *problems]),
            [("Save commute", "save"), ("Check-in and delay settings", "tune")],
            done=True,
        )

    async def _save(self, draft: Draft) -> SetupReply:
        assert draft.document is not None and draft.trips is not None
        if self._rt.config:
            return SetupReply(MANUAL)
        departure = to_utc(draft.trips[0].legs[0].train.planned_departure)
        if to_utc(self._rt.now()) >= departure - timedelta(minutes=OUTBOUND_BUFFER):
            return await self._probe(draft)
        try:
            await asyncio.to_thread(_write_new, self._path, draft.document)
        except FileExistsError:
            return SetupReply(MANUAL)
        except OSError:
            return SetupReply("Could not save the profile. Check the Docker data volume and retry.")
        self._draft = None
        refusal = await self._rt.reload()
        if refusal is not None:
            return SetupReply(f"Profile file saved. Reload issue: {refusal.text}", done=True)
        return SetupReply("Commute saved and loaded. Use /status to see it.", done=True)
