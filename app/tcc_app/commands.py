"""Telegram commands, pure: parsing a target (7.8) and rendering `/status` (7.13). No I/O."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from train_commute_companion.calendar import TZ
from train_commute_companion.model import Connection, Direction
from train_commute_companion.view import LegView, OccurrenceView, RuntimeView

from .telegram import callback_data

LOOKAHEAD_STALE = timedelta(hours=25)  # a daily run missed or failed
_TIME = re.compile(r"\d{1,2}:\d{2}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Target:
    profile_id: str
    profile_name: str
    direction: Direction | None = None
    service_date: date | None = None
    departure: time | None = None
    train: str | None = None
    whole_day: bool = False


def _key(s: str) -> str:
    return s.strip().lower().replace(" ", "_")


def _pretty(name: str) -> str:
    return name.replace("_", " ").title()


def _hm(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%H:%M")


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def parse_target(view: RuntimeView, args: Sequence[str]) -> Target | str:
    profiles = dict.fromkeys((d.profile_id, d.profile_name) for d in view.directions)
    names = ", ".join(n for _, n in profiles)

    def find(token: str) -> tuple[str, str] | None:
        return next((p for p in profiles if p[0] == token), None) or next(
            (p for p in profiles if _key(p[1]) == _key(token)), None
        )

    profile = None
    named = next((a for a in args if a.startswith("p=")), None)
    if named is not None:
        at = args.index(named)
        # a spaced name arrives split by whitespace: take the longest run of words that names a profile
        for end in range(len(args), at, -1):
            profile = find(" ".join([named[2:], *args[at + 1 : end]]))
            if profile is not None:
                args = [a for j, a in enumerate(args) if not at < j < end]
                break
        if profile is None:
            return f"No profile {named[2:]}. One of: {names}."
    direction = service_date = departure = train = None
    whole_day = False
    for i, token in enumerate(args):
        if token == named:
            continue
        try:
            if _TIME.fullmatch(token):
                departure = time.fromisoformat(token.zfill(5))
            elif _DATE.fullmatch(token):
                service_date = date.fromisoformat(token)
            elif token.lower() in (d.value for d in Direction):
                direction = Direction(token.lower())
            elif token.lower() == "day":
                whole_day = True
            elif (p := find(token)) is not None and profile is None:
                profile = p
            elif token.isdigit():
                train = token
            elif i == 0 and named is None:  # a lone unknown first word is taken as a profile
                return f"No profile {token}. One of: {names}."
            else:
                return f"I did not understand {token}."
        except ValueError:  # 25:99
            return f"I did not understand {token}."
    if profile is None:
        if len(profiles) != 1:
            return f"Which profile? One of: {names}."
        profile = next(iter(profiles))
    return Target(profile[0], profile[1], direction, service_date, departure, train, whole_day)


def _leg_line(x: LegView, prefix: str = "  ") -> str:
    line = f"{prefix}{_hm(x.departure or x.planned_departure)} {x.board} to {x.alight}: {x.state}"
    if x.departure_delay_min:
        line += f", +{x.departure_delay_min} min"
    if x.departure_platform:
        line += f", platform {x.departure_platform}"
    return line


def _next_time(c: Connection) -> datetime:
    b = c.legs[0].board
    reported = b.departure.value if b.departure is not None else None
    return reported or b.planned_departure or c.legs[0].train.planned_departure


def _occurrence(o: OccurrenceView) -> list[str]:
    k = o.key
    head = f"{_pretty(o.profile_name)} {k.direction.value} {_day(k.service_date)}: {o.direction_state.value}"
    if o.not_travelling:
        head += " (not travelling)"
    if o.route_changed:
        head += " (route changed)"
    out = [head, *(_leg_line(x) for x in o.legs)]
    for t in o.transfers:
        line = f"  change at {t.station}: {t.state.value}"
        if t.margin_min is not None:
            line += f" ({t.margin_min} min, needs {t.minimum})"
        out.append(line)
    for journey in o.retained:
        out += [_leg_line(x, f"  if you are on the {_hm(x.planned_departure)}: ") for x in journey]
    if o.next_good is not None:
        out.append(
            f"  next good connection: {_hm(_next_time(o.next_good))} from {o.next_good.legs[0].board.code}"
        )
    if o.advice is not None and o.advice.advised_train is not None and not o.advice.withdrawn:
        out.append(f"  earlier train advised: the {_hm(o.advice.advised_train.planned_departure)}")
    h = o.health
    if h.fresh:
        out.append("  NS data: fresh")
    else:
        out.append(
            f"  NS data: not fresh since {_hm(h.last_ok_at)}"
            if h.last_ok_at
            else "  NS data: not fresh"
        )
    return out


def render_status(view: RuntimeView, confirmed: bool) -> str:
    pending = [o for o in view.occurrences if not o.resolved]
    names = {(d.profile_id): d.profile_name for d in view.directions}
    lines = [x for o in pending for x in _occurrence(o)]
    if view.refusal:
        lines.append(f"Profile file refused: {view.refusal}")
    if view.key_rejected:
        lines.append("Your NS key was rejected.")
    if not view.store_ok:
        lines.append("State cannot be saved; retrying.")
    if view.undelivered:
        lines.append(f"{view.undelivered} alerts not delivered yet.")
    lines += [m.text for m in view.moved]
    for key, days in view.works.items():
        pid, _, d = key.partition("|")
        if days:
            dates = ", ".join(_day(w.service_date) for w in days)
            lines.append(f"Works ahead: {_pretty(names.get(pid, pid))} {d}: {dates}")
    for ok in view.lookahead_last_ok.values():
        if ok is None:
            lines.append("Planned-works check: not run yet")
        elif view.at.astimezone(UTC) - ok.astimezone(UTC) > LOOKAHEAD_STALE:
            lines.append(f"Planned-works check: last ok {_day(ok.astimezone(TZ).date())} {_hm(ok)}")
    if not confirmed:
        lines.append("Delivery not confirmed yet: send /test.")
    if not pending:
        lines.append("No journey is being followed now.")
    lines.append("Data: NS")
    return "\n".join(lines)


def status_buttons(view: RuntimeView) -> dict[str, Any] | None:
    rows = []
    for o in view.occurrences:
        if o.resolved:
            continue
        k = o.key
        day = k.service_date.isoformat()
        row = [
            {"text": label, "callback_data": data}
            for label, tag in (("I'm leaving", "l"), ("Not travelling", "s"))
            if (data := callback_data(tag, k.profile_id, k.direction.value, day)) is not None
        ]
        if o.next_good is not None:
            # the core names a train by its planned departure; the label shows the reported one
            train = o.next_good.legs[0].train
            hm, num = _hm(train.planned_departure), train.train_number
            data = callback_data(
                "c", k.profile_id, k.direction.value, day, hm, num
            ) or callback_data("c", k.profile_id, k.direction.value, day, hm)
            if data is not None:
                row.append({"text": f"Take {_hm(_next_time(o.next_good))}", "callback_data": data})
        if row:
            rows.append(row)
    return {"inline_keyboard": rows} if rows else None


def command_word(text: str) -> str:
    words = text.split()
    return words[0].lower().split("@", 1)[0] if words else ""
