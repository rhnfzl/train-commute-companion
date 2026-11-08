"""Alert text shared by more than one caller (7.9, 11.1). Pure: every fact comes in as an argument."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto

from train_commute_companion.calendar import hhmm
from train_commute_companion.model import Leg

ATTRIBUTION = "Data: NS"
CHECK_APP = "check the NS app"


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


def _changes(legs: Sequence[Leg]) -> str:
    """Every transfer station in order: a three-leg journey names both changes."""
    names = [x.alight.name for x in legs[:-1]]
    return f", change at {' then '.join(names)}" if names else ""


def crowd_text(legs: Sequence[Leg]) -> str:
    if not any(x.crowd_forecast is not None for x in legs):
        return ""
    if len(legs) == 1:
        return f", crowd forecast: {legs[0].crowd_forecast} per NS"
    forecasts = [f"{x.board.code}-{x.alight.code} {x.crowd_forecast or 'unknown'}" for x in legs]
    return f", crowd forecasts per NS: {', '.join(forecasts)}"


def all_good_text(legs: Sequence[Leg]) -> str:
    first, last = legs[0], legs[-1]
    when = _when(first)
    text = f"your {when} to {last.alight.name} is on time per NS"
    text += _changes(legs)
    track = first.board.actual_track or first.board.planned_track
    text += f", platform {track}" if track else ""
    return text + crowd_text(legs) + "."


def no_journey_text(name: str) -> str:
    return f"no journey of {name} is being followed now"


def no_journey(name: str) -> Refused:
    return Refused(no_journey_text(name), RefusalKind.NO_JOURNEY)


def data_unclear_text(label: str, station: str, copies: Sequence[str]) -> str:
    text = f"NS data unclear for {label or 'your train'}{' at ' + station if station else ''}."
    text += "".join(f" Copy {i}: {c}." for i, c in enumerate(copies, 1))
    return text + " Check the NS app or the station boards."
