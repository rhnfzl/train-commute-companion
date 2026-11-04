"""Alert text shared by more than one caller (7.9, 11.1). Pure: every fact comes in as an argument."""

from collections.abc import Sequence

from train_commute_companion.calendar import hhmm
from train_commute_companion.model import Leg

CHECK_APP = "check the NS app"


def _when(leg: Leg) -> str:
    d = leg.board.departure
    return hhmm((d and (d.value or d.planned)) or leg.train.planned_departure)


def _changes(legs: Sequence[Leg]) -> str:
    """Every transfer station in order: a three-leg journey names both changes."""
    names = [x.alight.name for x in legs[:-1]]
    return f", change at {' then '.join(names)}" if names else ""


def all_good_text(legs: Sequence[Leg]) -> str:
    first, last = legs[0], legs[-1]
    when = _when(first)
    text = f"your {when} to {last.alight.name} is on time per NS"
    text += _changes(legs)
    track = first.board.actual_track or first.board.planned_track
    return text + (f", platform {track}." if track else ".")


def no_journey_text(name: str) -> str:
    return f"no journey of {name} is being followed now"


def data_unclear_text(label: str, station: str, copies: Sequence[str]) -> str:
    text = f"NS data unclear for {label or 'your train'}{' at ' + station if station else ''}."
    text += "".join(f" Copy {i}: {c}." for i, c in enumerate(copies, 1))
    return text + " Check the NS app or the station boards."
