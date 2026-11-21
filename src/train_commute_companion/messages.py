"""Alert text shared by more than one caller (7.9, 11.1). Pure: every fact comes in as an argument."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto

from train_commute_companion.calendar import hhmm, to_utc
from train_commute_companion.catalogue import MessageLanguage, message
from train_commute_companion.model import Leg, Provenance

ATTRIBUTION = "Data: NS"
CHECK_APP = "check the NS app"
ON_TIME_MAX_MIN = 1  # a departure delay up to this is on time (7.5)


_STATION_NUMBER = re.compile(r"\b\d{7}\b")  # a UIC station number, as NS gives some stops


def name_stations(text: str, labels: Mapping[str, str]) -> str:
    """`text` with each station number the catalogue knows shown by its name: NS gives some
    stops by number only, and a message never shows one."""
    return _STATION_NUMBER.sub(lambda m: labels.get(m[0], m[0]), text)


class RefusalKind(Enum):
    NO_JOURNEY = auto()  # no journey is followed to act on
    INVALID = auto()  # the journey is there, what was asked of it cannot be done
    RETRY = auto()  # NS or a concurrent change got in the way: the same action may land next time


@dataclass(frozen=True, slots=True)
class Refused:
    """Why a user action was not written: `text` for the user, `kind` for what a shell does."""

    text: str
    kind: RefusalKind = RefusalKind.INVALID


def _when(leg: Leg) -> str:
    d = leg.board.departure
    return hhmm((d and (d.value or d.planned)) or leg.train.planned_departure)


def _changes(legs: Sequence[Leg], language: MessageLanguage = "en") -> str:
    """Every transfer station in order: a three-leg journey names both changes."""
    names = [x.alight.name for x in legs[:-1]]
    return (
        message("shared.change_at", language, stations=message("shared.then", language).join(names))
        if names
        else ""
    )


def crowd_text(legs: Sequence[Leg], language: MessageLanguage = "en") -> str:
    if not any(x.crowd_forecast is not None for x in legs):
        return ""
    if len(legs) == 1:
        return message("leg.crowd", language, level=str(legs[0].crowd_forecast))
    unknown = message("shared.unknown", language)
    forecasts = [f"{x.board.code}-{x.alight.code} {x.crowd_forecast or unknown}" for x in legs]
    return message("shared.crowd", language, forecasts=", ".join(forecasts))


def leave_by(planned: datetime, expected: datetime | None, buffer_min: int) -> datetime:
    """Leave in time for the earlier departure, including the configured travel time."""
    departure = min(to_utc(planned), to_utc(expected)) if expected is not None else to_utc(planned)
    return departure - timedelta(minutes=buffer_min)


def leave_by_text(leg: Leg, buffer_min: int | None, language: MessageLanguage = "en") -> str:
    if buffer_min is None:
        return ""
    obs = leg.board.departure
    expected = obs.value if obs is not None and obs.provenance is Provenance.REPORTED else None
    t = leave_by(leg.train.planned_departure, expected, buffer_min)
    return " " + message("leave_by", language, time=hhmm(t))


def all_good_text(
    legs: Sequence[Leg], buffer_min: int | None = None, language: MessageLanguage = "en"
) -> str:
    first, last = legs[0], legs[-1]
    text = message("checkin.all_good", language, time=_when(first), station=last.alight.name)
    text += _changes(legs, language)
    text += _track(first, language)
    leave = leave_by_text(first, buffer_min, language)
    return text + crowd_text(legs, language) + "." + (leave + "." if leave else "")


def _track(leg: Leg, language: MessageLanguage = "en") -> str:
    track = leg.board.actual_track or leg.board.planned_track
    return message("leg.platform", language, track=track) if track else ""


def status_text(legs: Sequence[Leg], late: int | None, language: MessageLanguage = "en") -> str:
    """The train followed now, for a user at its station (Q1 2026-11-17, 7.9):
    '07:55 to Blerick, platform 6, on time per NS, arrives 08:55'. Named by its planned
    departure, so a late train reads '07:55 ... 1 min late', never '07:56'. `late`: the
    departure delay NS reports; None (no reported time) states no timing."""
    first, last = legs[0], legs[-1]
    text = message(
        "train.destination",
        language,
        time=hhmm(first.train.planned_departure),
        station=last.alight.name,
    )
    text += _changes(legs, language) + _track(first, language)
    if late is not None:
        text += ", " + (
            message("leg.ok", language)
            if late <= ON_TIME_MAX_MIN
            else message("train.minutes_late", language, minutes=late)
        )
    arr = last.alight.arrival
    t = (arr.value or arr.planned if arr else None) or last.alight.planned_arrival
    text += message("train.arrives", language, time=hhmm(t)) if t else ""
    return text + crowd_text(legs, language)  # the ledger strips the crowd note as a suffix


def no_journey_text(name: str) -> str:
    return f"no journey of {name} is being followed now"


def no_journey(name: str) -> Refused:
    return Refused(no_journey_text(name), RefusalKind.NO_JOURNEY)


def data_unclear_text(
    label: str, station: str, copies: Sequence[str], language: MessageLanguage = "en"
) -> str:
    place = message("unclear.station", language, station=station) if station else ""
    train = label or message("unclear.train", language)
    text = message("unclear.heading", language, train=train, station=place)
    text += "".join(
        message("unclear.copy", language, number=i, details=c) for i, c in enumerate(copies, 1)
    )
    return text + message("unclear.check", language)
