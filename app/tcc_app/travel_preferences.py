"""Keep confirmed travel defaults in the companion's own state."""

import hashlib
import json
import logging
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .travel_actions import result, save_private_json

log = logging.getLogger(__name__)
_REVIEW_TTL = timedelta(minutes=10)
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_REQUEST_ID = re.compile(r"[a-zA-Z0-9._:-]{1,96}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _text(value: object) -> str:
    if isinstance(value, str):
        clean = value.strip()
        if 0 < len(clean) <= 80 and clean.isprintable():
            return clean
    raise ValueError("Use 1 to 80 printable characters for a preference name or station.")


def _preference(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() - {
        "name",
        "origin",
        "destination",
        "arrival_by",
        "return_on_leave",
    }:
        raise ValueError("Supply a name, origin, destination, and timing preferences.")
    name, origin, destination = (_text(value.get(k)) for k in ("name", "origin", "destination"))
    if origin.casefold() == destination.casefold():
        raise ValueError("Choose different origin and destination stations.")
    clean: dict[str, Any] = {"name": name, "origin": origin, "destination": destination}
    if "arrival_by" in value:
        arrival = value["arrival_by"]
        if not isinstance(arrival, str) or not _CLOCK.fullmatch(arrival):
            raise ValueError("Use HH:MM for the arrival time.")
        clean["arrival_by"] = arrival
    if "return_on_leave" in value:
        leave = value["return_on_leave"]
        if not isinstance(leave, bool):
            raise ValueError("Set return_on_leave to true or false.")
        clean["return_on_leave"] = leave
    if "arrival_by" not in clean and "return_on_leave" not in clean:
        raise ValueError("Provide an arrival target or a return preference.")
    return clean


def _change(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        if value.get("action") == "save" and value.keys() == {"action", "preference"}:
            return {"action": "save", "preference": _preference(value["preference"])}
        if value.get("action") == "forget" and value.keys() == {"action", "name"}:
            return {"action": "forget", "name": _text(value["name"]).casefold()}
    raise ValueError("Choose to save or forget a travel preference.")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _route(value: Mapping[str, Any]) -> tuple[str, str]:
    return _text(value.get("origin")).casefold(), _text(value.get("destination")).casefold()


def _pattern(preference: Mapping[str, Any]) -> str:
    return _digest(
        (_route(preference), preference.get("arrival_by"), preference.get("return_on_leave"))
    )


def _describe(preference: Mapping[str, Any]) -> str:
    parts = []
    if arrival := preference.get("arrival_by"):
        parts.append(f"Arrive by {arrival}.")
    if "return_on_leave" in preference:
        parts.append(
            "Pick the return when you leave."
            if preference["return_on_leave"]
            else "Choose the return before leaving."
        )
    return " ".join(parts)


class TravelPreferences:
    def __init__(self, path: Path, now: Callable[[], datetime]) -> None:
        self._path, self._now = path, now
        self._preferences: dict[str, dict[str, Any]] = {}
        self._receipts: dict[str, str] = {}
        self._offered: set[str] = set()
        self._reviews: dict[str, tuple[str, datetime]] = {}
        self._safe = True
        try:
            data = json.loads(path.read_text())
            preferences = data["preferences"]
            receipts = data["receipts"]
            offered = data["offered"]
            if not isinstance(preferences, dict) or not isinstance(receipts, dict):
                raise TypeError
            clean = {key: _preference(value) for key, value in preferences.items()}
            if any(key != value["name"].casefold() for key, value in clean.items()):
                raise ValueError
            if any(
                not _REQUEST_ID.fullmatch(key)
                or not isinstance(value, str)
                or not _DIGEST.fullmatch(value)
                for key, value in receipts.items()
            ):
                raise ValueError
            if not isinstance(offered, list) or any(
                not isinstance(item, str) or not _DIGEST.fullmatch(item) for item in offered
            ):
                raise ValueError
            self._preferences, self._receipts, self._offered = clean, receipts, set(offered)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, LookupError, TypeError, RecursionError):
            log.warning("Travel preferences are unreadable. Preference changes are disabled.")
            self._safe = False

    def list(self) -> dict[str, Any]:
        if not self._safe:
            return result("retry", "Travel preferences are unavailable.")
        return {
            **result("ok", "Saved travel preferences."),
            "preferences": [dict(self._preferences[key]) for key in sorted(self._preferences)],
        }

    def preview(self, change: object) -> dict[str, Any]:
        if not self._safe:
            return result("retry", "Travel preferences are unavailable.")
        try:
            clean = _change(change)
        except ValueError as error:
            return result("invalid", str(error))
        if clean["action"] == "forget":
            current = self._preferences.get(clean["name"])
            if current is None:
                return result("invalid", "There is no saved preference by that name.")
            text = f"Forget {current['name']}?"
        else:
            new = clean["preference"]
            verb = (
                "Replace the preference"
                if new["name"].casefold() in self._preferences
                else "Remember"
            )
            text = (
                f"{verb} {new['name']} for trips from {new['origin']} to {new['destination']}? "
                f"{_describe(new)}"
            )
        now = self._now()
        self._reviews = {key: review for key, review in self._reviews.items() if review[1] > now}
        if len(self._reviews) >= 32:
            self._reviews.pop(next(iter(self._reviews)))
        token = secrets.token_urlsafe(18)
        self._reviews[token] = (_digest(clean), now + _REVIEW_TTL)
        return {**result("ready", text), "review_token": token}

    def receipt(self, request_id: str) -> dict[str, Any] | None:
        if request_id in self._receipts:
            return result("saved", "Travel preference change saved.")
        return None

    def _write(
        self,
        preferences: dict[str, dict[str, Any]],
        receipts: dict[str, str],
        offered: set[str],
    ) -> bool:
        if not save_private_json(
            self._path,
            {"preferences": preferences, "receipts": receipts, "offered": sorted(offered)},
        ):
            return False
        self._preferences, self._receipts, self._offered = preferences, receipts, offered
        return True

    def save(self, change: object, review_token: str, request_id: str) -> dict[str, Any]:
        if not self._safe:
            return result("retry", "Travel preferences are unavailable.")
        if not _REQUEST_ID.fullmatch(request_id):
            return result("invalid", "Use a valid preference request ID.")
        try:
            clean = _change(change)
        except ValueError as error:
            return result("invalid", str(error))
        fingerprint = _digest(clean)
        if previous := self._receipts.get(request_id):
            if previous == fingerprint:
                return result("saved", "Travel preference change saved.")
            return result("invalid", "This request ID already names a different preference change.")
        review = self._reviews.get(review_token)
        if review is None or review[0] != fingerprint or review[1] <= self._now():
            return result("expired", "Review the preference again before saving.")
        preferences = dict(self._preferences)
        offered = self._offered
        if clean["action"] == "forget":
            removed = preferences.pop(clean["name"])
            offered = offered | {_pattern(removed)}
            text = f"Forgot {removed['name']}."
        else:
            new = clean["preference"]
            preferences[new["name"].casefold()] = new
            text = f"Saved {new['name']}. {_describe(new)}"
        if not self._write(preferences, {**self._receipts, request_id: fingerprint}, offered):
            return result("retry", "The preference could not be saved. Try again.")
        # The state a person reviewed changes after any successful preference write.
        self._reviews.clear()
        return result("saved", text)

    def with_defaults(
        self, change: dict[str, Any], *, profile_origin: str | None = None
    ) -> dict[str, Any]:
        cleaned = dict(change)
        plan = cleaned.get("plan")
        if cleaned.get("action") not in ("create", "edit") or not isinstance(plan, dict):
            return cleaned
        name = cleaned.pop("preference_name", None)
        origin, destination = plan.get("origin"), plan.get("destination")
        if name is not None:
            preference = (
                self._preferences.get(name.strip().casefold()) if isinstance(name, str) else None
            )
            if preference is None or any(
                value is not None
                and (
                    not isinstance(value, str)
                    or value.strip().casefold() != preference[key].casefold()
                )
                for key, value in (("origin", origin), ("destination", destination))
            ):
                return cleaned
        else:
            origin = plan.get("origin", profile_origin)
            if not isinstance(origin, str) or not isinstance(destination, str):
                return cleaned
            route = origin.strip().casefold(), destination.strip().casefold()
            matches = [item for item in self._preferences.values() if _route(item) == route]
            if len(matches) != 1:
                return cleaned
            preference = matches[0]
        filled = {"origin": preference["origin"], "destination": preference["destination"], **plan}
        outbound, back = filled.get("outbound", {}), filled.get("return")
        if (
            isinstance(outbound, dict)
            and outbound.get("mode") != "exact"
            and "arrive_by" not in outbound
            and "departure" not in outbound
            and "train_number" not in outbound
            and "arrival_by" in preference
        ):
            filled["outbound"] = {
                "mode": "window",
                **outbound,
                "arrive_by": preference["arrival_by"],
            }
        if (
            isinstance(back, dict)
            and "mode" not in back
            and "departure" not in back
            and "train_number" not in back
            and preference.get("return_on_leave") is True
        ):
            filled["return"] = {**back, "mode": "on_leave"}
        return {**cleaned, "plan": filled}

    def dismiss(self, change: object) -> bool:
        """Record a shown or dismissed offer without saving a preference."""
        if not self._safe:
            return False
        try:
            clean = _change(change)
        except ValueError:
            return False
        if clean["action"] != "save":
            return False
        fingerprint = _pattern(clean["preference"])
        return fingerprint in self._offered or self._write(
            self._preferences, self._receipts, self._offered | {fingerprint}
        )

    def suggest(self, plans: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        if not self._safe:
            return None
        saved_routes = {_route(preference) for preference in self._preferences.values()}
        dated = []
        for plan in plans:
            try:
                day = date.fromisoformat(plan["start_date"])
                route = _route(plan)
                plan_id = _text(plan.get("plan_id"))
            except (KeyError, TypeError, ValueError):
                continue
            if route not in saved_routes:
                dated.append((day, route, plan_id, plan))
        recent: dict[tuple[str, str], list[tuple[date, str, Mapping[str, Any]]]] = {}
        for day, route, plan_id, plan in sorted(dated, key=lambda entry: entry[0], reverse=True):
            entries = recent.setdefault(route, [])
            if len(entries) < 3 and all(
                day != old_day and plan_id != old_id for old_day, old_id, _ in entries
            ):
                entries.append((day, plan_id, plan))
        for entries in recent.values():
            if len(entries) < 3:
                continue
            choices = [plan for _, _, plan in entries]
            outbound = [plan.get("outbound") for plan in choices]
            returns = [plan.get("return") for plan in choices]
            origin, destination = (_text(choices[0][key]) for key in ("origin", "destination"))
            preference: dict[str, Any] = {
                "name": f"{origin} to {destination}",
                "origin": origin,
                "destination": destination,
            }
            target = outbound[0].get("arrive_by") if isinstance(outbound[0], dict) else None
            if (
                isinstance(target, str)
                and _CLOCK.fullmatch(target)
                and all(
                    isinstance(leg, dict) and leg.get("arrive_by") == target for leg in outbound
                )
            ):
                preference["arrival_by"] = target
            if all(isinstance(leg, dict) and leg.get("mode") == "on_leave" for leg in returns):
                preference["return_on_leave"] = True
            try:
                clean = _preference(preference)
            except ValueError:
                continue
            if clean["name"].casefold() in self._preferences or _pattern(clean) in self._offered:
                continue
            return {
                "change": {"action": "save", "preference": clean},
                "text": (
                    f"I noticed a pattern in your last three saved plans from {origin} to "
                    f"{destination}. {_describe(clean)} Remember this for future trips?"
                ),
            }
        return None
