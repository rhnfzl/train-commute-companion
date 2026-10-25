"""User-visible alert text (7.9, 11.1). Pure: every fact comes in as an argument."""

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from train_commute_companion.calendar import TZ
from train_commute_companion.model import Connection, EventKind, Leg, Stop

if TYPE_CHECKING:  # runtime import would cycle once lifecycle emits through messages
    from train_commute_companion.lifecycle import Ctx

CHECK_APP = "check the NS app"


def _hhmm(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%H:%M")


def _when(leg: Leg) -> str:
    d = leg.board.departure
    return _hhmm((d and (d.value or d.planned)) or leg.train.planned_departure)


def _name(leg: Leg) -> str:
    return f"{leg.board.name} to {leg.alight.name}"


def _changes(legs: Sequence[Leg]) -> str:
    """Every transfer station in order: a three-leg journey names both changes."""
    names = [x.alight.name for x in legs[:-1]]
    return f", change at {' then '.join(names)}" if names else ""


def _next(rep: object) -> str:
    if not isinstance(rep, Connection):
        return f"No good connection found, {CHECK_APP}."
    first = rep.legs[0]
    when = _when(first)
    track = first.board.actual_track or first.board.planned_track
    text = f"Next good connection: {when}"
    text += f", platform {track}" if track else ""
    return text + _changes(rep.legs) + "."


def _legs(legs: Sequence[Leg]) -> str:
    first, last = legs[0], legs[-1]
    when = _when(first)
    text = f"your {when} to {last.alight.name} is on time per NS"
    text += _changes(legs)
    track = first.board.actual_track or first.board.planned_track
    return text + (f", platform {track}." if track else ".")


def render(kind: EventKind, ctx: "Ctx", **facts: object) -> str:
    """Starts with the title-cased profile name and names legs by station names."""
    return f"{ctx.profile.name.replace('_', ' ').title()}: {body(kind, **facts)}"


def body(kind: EventKind, **facts: object) -> str:
    leg = facts.get("leg")
    name = _name(leg) if isinstance(leg, Leg) else str(leg or "your")
    stop = facts.get("stop")
    stop_name = stop.name if isinstance(stop, Stop) else str(stop or facts.get("station") or "")
    station = str(facts.get("station") or stop_name)
    legs = facts.get("legs")
    legs = legs if isinstance(legs, Sequence) else None
    nxt = _next(facts.get("replacement"))
    train = f"your {name} train"
    text = {
        EventKind.CANCELLED: f"{train} is cancelled. {nxt}",
        EventKind.TRANSFER_BROKEN: f"you will miss your change at {station}. {nxt}",
        EventKind.TRANSFER_AT_RISK: f"your change at {station} is tight, {CHECK_APP}.",
        EventKind.MISSED_FIRST_TRAIN: f"you missed {train}. {nxt}",
        EventKind.MISSED_TRANSFER: f"you missed your change at {station}. {nxt}",
        EventKind.LEAVE_NOW: f"leave now: {nxt}",
        EventKind.PLATFORM_CHANGE: (
            f"{train} now leaves from platform {facts.get('platform')} (was {facts.get('was')})."
        ),
        EventKind.CUT_SHORT: f"{train} runs only to {stop_name}, change there. {nxt}",
        EventKind.ROUTE_CHANGED: f"{train} goes a different way. {nxt}",
        EventKind.CANNOT_SEE_TRAIN: (
            f"cannot see {train} in NS data ({facts.get('reason', 'no fresh answer')}), {CHECK_APP}."
        ),
        EventKind.CAN_SEE_TRAIN_AGAIN: f"{train} is visible in NS data again.",
        EventKind.KEY_REJECTED: "NS rejected the API key, fix it to keep getting alerts.",
        EventKind.NO_CONNECTION: f"no connection found, {CHECK_APP}.",
        EventKind.MISSING: f"{train} is not in NS data, {CHECK_APP}.",
        EventKind.STOPPED_FOLLOWING: (
            f"NS showed {train} arriving at {facts.get('arrival')}; I stopped following,"
            f" {CHECK_APP} if you are not there."
            if facts.get("arrival")
            else f"I stopped following your journey, {CHECK_APP}."
        ),
        EventKind.PROFILE_REFUSED: f"profile change refused: {facts.get('reason', 'invalid')}.",
        EventKind.STORAGE_FAILED: "could not save state, alerts may repeat or be lost.",
        EventKind.DELAYED: f"{train} is delayed {facts.get('delay')} min, now {facts.get('time')}.",
        EventKind.ALL_GOOD: _legs(legs) if legs else "",
        EventKind.NEARLY_ON_TIME: f"{train} is nearly on time, leaves {facts.get('time')}.",
        EventKind.BACK_TO_NORMAL: f"{train} is back to normal, on time per NS.",
        EventKind.RESTORED: f"{train} runs again as planned, on time per NS.",
        EventKind.WORKS_AHEAD: f"planned works {facts.get('day', 'ahead')} affect your journey.",
        EventKind.TIMETABLE_ADOPTED: "new timetable adopted for your usual journey.",
        EventKind.EARLIER_TRAIN_WITHDRAWN: "the earlier train I suggested no longer helps.",
        EventKind.OFFLINE: f"NS data is unavailable, {CHECK_APP}.",
        EventKind.CHOOSE_REFUSED: f"cannot follow that connection: {facts.get('reason', 'refused')}.",
        EventKind.NEXT_CONNECTION: nxt,
    }[kind]
    return text
