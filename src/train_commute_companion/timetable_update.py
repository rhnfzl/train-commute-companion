"""Describe the profile edit that corresponds to an adopted timetable."""

import hmac
from dataclasses import dataclass
from datetime import date, time
from itertools import pairwise

from train_commute_companion.calendar import MINUTES_PER_DAY, WEEKDAYS, minute_of_day
from train_commute_companion.catalogue import MessageLanguage, message
from train_commute_companion.model import Direction, Profile, direction_key
from train_commute_companion.revisions import baseline_scope, commute_day_for, find_profile
from train_commute_companion.state import BaselineHistory
from train_commute_companion.store import PersistedState


@dataclass(frozen=True, slots=True)
class TimetableEdit:
    profile_id: str
    direction: Direction
    effective_from: date
    weekday: str | None
    departure: time
    windows: tuple[tuple[str | None, tuple[time, time]], ...]

    @property
    def field(self) -> str:
        suffix = "first_train" if self.weekday is None else f"first_train_by_weekday.{self.weekday}"
        return f"{self.direction.value}.{suffix}"

    @property
    def yaml_hint(self) -> str:
        return self.hint("en")

    def hint(self, language: MessageLanguage) -> str:
        lines = [
            message(
                "lookahead.yaml",
                language,
                day=self.effective_from.isoformat(),
                field=self.field,
                time=f"{self.departure:%H:%M}",
                profile=self.profile_id,
            )
        ]
        for weekday, (start, end) in self.windows:
            hours = f'["{start:%H:%M}", "{end:%H:%M}"]'
            if weekday is None:
                lines.append(
                    message(
                        "lookahead.yaml_optional",
                        language,
                        field=f"{self.direction.value}.window",
                        hours=hours,
                    )
                )
            else:
                lines.append(
                    message(
                        "lookahead.yaml_required",
                        language,
                        field=f"{self.direction.value}.by_weekday.{weekday}.window",
                        hours=hours,
                    )
                )
        return "\n".join(lines)


def _shift_clock(value: time, delta: int) -> time:
    minutes = (minute_of_day(value) + delta) % MINUTES_PER_DAY
    return time(minutes // 60, minutes % 60)


def timetable_edit(
    profile: Profile, direction: Direction, history: BaselineHistory, version: int
) -> TimetableEdit | None:
    adopted = next((v for v in history.versions if v.version == version), None)
    if (
        adopted is None
        or not adopted.confirmed
        or profile.travel_plan
        or (adopted.commute_weekday is None and profile.journey(direction).mode != "fixed")
        or baseline_scope(profile, direction, adopted.effective_from) != adopted.commute_weekday
        or adopted != history.latest_confirmed(adopted.commute_weekday)
    ):
        return None
    journey = profile.journey_on(
        direction, commute_day_for(profile, direction, adopted.effective_from)
    )
    legs = adopted.signature.legs
    stations = (profile.origin(direction), *journey.transfers, profile.destination(direction))
    if (
        journey.mode != "fixed"
        or not journey.first_train_set
        or journey.exact_train is not None
        or not legs
        or legs[0].day_offset != 0
        or legs[0].departure.second
        or legs[0].departure.microsecond
        or tuple((leg.board, leg.alight) for leg in legs) != tuple(pairwise(stations))
        or legs[0].departure == journey.first_train
    ):
        return None
    weekday = None if adopted.commute_weekday is None else WEEKDAYS[adopted.commute_weekday - 1]
    configured = profile.journey(direction)
    delta = minute_of_day(legs[0].departure) - minute_of_day(journey.first_train)
    windows: list[tuple[str | None, tuple[time, time]]] = (
        [(None, configured.window)] if weekday is None else []
    )
    windows.extend(
        (day, rule.window)
        for day, rule in configured.by_weekday.items()
        if rule.window is not None
        and (rule.mode or configured.mode) == "fixed"
        and weekday in (None, day)
    )
    return TimetableEdit(
        profile.id,
        direction,
        adopted.effective_from,
        weekday,
        legs[0].departure,
        tuple(
            (day, (_shift_clock(start, delta), _shift_clock(end, delta)))
            for day, (start, end) in windows
        ),
    )


def offered_edit(
    state: PersistedState, profile_id: str, direction: Direction, version: int, recipient: str
) -> tuple[TimetableEdit, str | None] | None:
    profile = find_profile(state.config, profile_id)
    history = state.baselines.get(direction_key(profile_id, direction))
    if state.config is None or profile is None or history is None:
        return None
    edit = timetable_edit(profile, direction, history, version)
    if edit is None:
        return None
    adopted = next(v for v in history.versions if v.version == version)
    if adopted.update_nonce is None:
        return None
    token = None
    if recipient.startswith("ha-mobile:") and profile.alerts.timetable_update:
        message = f"{state.config.revision}|{profile_id}|{direction.value}|{version}|{recipient}"
        token = hmac.digest(adopted.update_nonce.encode(), message.encode(), "sha256").hex()
    return edit, token


def resolve_edit(state: PersistedState, token: str, recipient: str) -> TimetableEdit | None:
    if (
        state.config is None
        or not recipient.startswith("ha-mobile:")
        or len(token) != 64
        or not token.isascii()
    ):
        return None
    for profile in state.config.profiles:
        for direction in Direction:
            history = state.baselines.get(direction_key(profile.id, direction))
            if history is None:
                continue
            scopes = {v.commute_weekday for v in history.versions if v.update_nonce is not None}
            for scope in scopes:
                adopted = history.latest_confirmed(scope)
                if adopted is None:
                    continue
                offer = offered_edit(state, profile.id, direction, adopted.version, recipient)
                if (
                    offer is not None
                    and offer[1] is not None
                    and hmac.compare_digest(token, offer[1])
                ):
                    return offer[0]
    return None
