"""Renders the runtime view for Telegram and MCP, pure: parsing a target (7.8), rendering
`/status` (7.13), the `/test` webhook results and the read-only MCP answers (9 step 5), each
MCP answer built from a per-profile copy of the view by the functions `/status` uses. No I/O."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from train_commute_companion.calendar import TZ
from train_commute_companion.health import BoardHealth
from train_commute_companion.model import Connection, Direction, Provenance, TimeObs
from train_commute_companion.view import LegView, OccurrenceView, RuntimeView
from train_commute_companion.webhook import WebhookTestResult

from .telegram import callback_data

LOOKAHEAD_STALE = timedelta(hours=25)  # a daily run missed or failed
TOOLS = ("get_status", "get_next_connections", "get_works_ahead")
REFUSAL_HIDDEN = "details are in Telegram /status and the app log"
# the check probes at most 3 commute days within 10 days (7.10)
NO_WORKS = "No planned works found in the commute days the last check covered."
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
    state = "on time per NS" if x.state == "ok" else x.state  # never read as proven (rule 20)
    line = f"{prefix}{_hm(x.departure or x.planned_departure)} {x.board} to {x.alight}: {state}"
    if x.departure_delay_min:
        line += f", +{x.departure_delay_min} min"
    if x.departure_platform:
        line += f", platform {x.departure_platform}"
    if x.state in ("ok", "delayed"):
        if x.departure is None:
            line += ", no reported departure time"
        if x.arrival is None:
            line += ", no reported arrival time"
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
    out.append(_health_line(o.health))
    return out


def _health_line(h: BoardHealth) -> str:
    if h.fresh:
        return (
            "  NS data: fresh, last poll failed" if h.consecutive_failures else "  NS data: fresh"
        )
    return (
        f"  NS data: not fresh since {_hm(h.last_ok_at)}"
        if h.last_ok_at
        else "  NS data: not fresh"
    )


def _warnings(view: RuntimeView) -> list[str]:
    lines = []
    if view.refusal:
        lines.append(f"Profile file refused: {view.refusal}")
    if view.key_rejected:
        lines.append("Your NS key was rejected.")
    if not view.store_ok:
        lines.append("State cannot be saved; retrying.")
    if view.undelivered:
        lines.append(f"{view.undelivered} alerts not delivered yet.")
    return lines


def warning_lines(view: RuntimeView, confirmed: bool) -> list[str]:
    """Every warning `/status` shows for this view, in its wording, so no MCP tool reads as all
    good while `/status` does not (review focus 3)."""
    health = [_health_line(o.health) for o in view.occurrences if not o.resolved]
    return [
        *dict.fromkeys(x for x in health if x != "  NS data: fresh"),
        *_warnings(view),
        *(m.text for m in view.moved),
        *_check_lines(view),
        *([] if confirmed else ["Delivery not confirmed yet: send /test."]),
    ]


def _stale(view: RuntimeView, ok: datetime) -> bool:
    return view.at.astimezone(UTC) - ok.astimezone(UTC) > LOOKAHEAD_STALE


def works_lines(view: RuntimeView) -> list[str]:
    return _works_ahead(view) + _check_lines(view)


def _works_ahead(view: RuntimeView) -> list[str]:
    names = {d.profile_id: d.profile_name for d in view.directions}
    lines = []
    for key, days in view.works.items():
        pid, _, d = key.rpartition("|")  # a profile id may contain "|"; the direction never does
        if days:
            dates = ", ".join(_day(w.service_date) for w in days)
            lines.append(f"Works ahead: {_pretty(names.get(pid, pid))} {d}: {dates}")
    return lines


def _check_lines(view: RuntimeView) -> list[str]:
    lines = []
    for ok in view.lookahead_last_ok.values():
        if ok is None:
            lines.append("Planned-works check: not run yet")
        elif _stale(view, ok):
            lines.append(f"Planned-works check: last ok {_day(ok.astimezone(TZ).date())} {_hm(ok)}")
    return lines


def render_status(view: RuntimeView, confirmed: bool) -> str:
    pending = [o for o in view.occurrences if not o.resolved]
    lines = [x for o in pending for x in _occurrence(o)]
    lines += _warnings(view)
    lines += [m.text for m in view.moved]
    lines += works_lines(view)
    if not confirmed:
        lines.append("Delivery not confirmed yet: send /test.")
    if not pending:
        lines.append("No journey is being followed now.")
    lines.append("Data: NS")
    return "\n".join(lines)


def for_profile(view: RuntimeView, profile_id: str) -> RuntimeView:
    """The view as one profile alone would see it; the refusal text echoes rejected values and
    read errors, so it is replaced by a fixed text."""
    keys = {f"{profile_id}|{d.value}" for d in Direction}  # exact keys, never a prefix match
    return replace(
        view,
        profiles=tuple(p for p in view.profiles if p.id == profile_id),
        directions=tuple(d for d in view.directions if d.profile_id == profile_id),
        occurrences=tuple(o for o in view.occurrences if o.key.profile_id == profile_id),
        moved=tuple(m for m in view.moved if m.profile_id == profile_id),
        works={k: v for k, v in view.works.items() if k in keys},
        lookahead_last_ok={k: v for k, v in view.lookahead_last_ok.items() if k in keys},
        refusal=REFUSAL_HIDDEN if view.refusal else None,
    )


def _name(view: RuntimeView, profile_id: str) -> str:
    return next(d.profile_name for d in view.directions if d.profile_id == profile_id)


def status_tool(view: RuntimeView, confirmed: bool, profile_id: str) -> dict[str, Any]:
    return {
        "profile": profile_id,
        "name": _pretty(_name(view, profile_id)),
        "text": render_status(for_profile(view, profile_id), confirmed),
    }


def _reported_hm(obs: TimeObs | None) -> str | None:
    if obs is None or obs.provenance is not Provenance.REPORTED or obs.value is None:
        return None  # a planned time is never shown as a reported one (rule 20)
    return _hm(obs.value)


def _connection(c: Connection) -> dict[str, Any]:
    legs = []
    for leg in c.legs:
        b, a = leg.board, leg.alight
        legs.append(
            {
                "train_number": leg.train.train_number,
                "board": b.code,
                "alight": a.code,
                "planned_departure": _hm(b.planned_departure or leg.train.planned_departure),
                "departure": _reported_hm(b.departure),
                "planned_arrival": None if a.planned_arrival is None else _hm(a.planned_arrival),
                "arrival": _reported_hm(a.arrival),
                "platform": b.actual_track or b.planned_track,
            }
        )
    return {"departure": _hm(_next_time(c)), "from": c.legs[0].board.code, "legs": legs}


def connections_tool(
    view: RuntimeView, confirmed: bool, profile_id: str, direction: Direction
) -> dict[str, Any]:
    v = for_profile(view, profile_id)
    occs = sorted(
        (o for o in v.occurrences if not o.resolved and o.key.direction is direction),
        key=lambda o: o.key.service_date,
    )
    journeys = [
        {
            "service_date": o.key.service_date.isoformat(),
            "state": o.direction_state.value,
            "next_good": None if o.next_good is None else _connection(o.next_good),
        }
        for o in occs
    ]
    lines = [x for o in occs for x in _occurrence(o)] or ["No journey is being followed now."]
    lines += [x for x in warning_lines(v, confirmed) if x not in lines]  # health shown once
    lines.append("Data: NS")
    return {
        "profile": profile_id,
        "direction": direction.value,
        "journeys": journeys,
        "text": "\n".join(lines),
    }


def works_tool(view: RuntimeView, confirmed: bool, profile_id: str) -> dict[str, Any]:
    v = for_profile(view, profile_id)
    lines = _works_ahead(v)
    directions = []
    for d in v.directions:
        key = f"{profile_id}|{d.direction.value}"
        ok = v.lookahead_last_ok.get(key)
        if key not in v.lookahead_last_ok:  # a removed profile still running: never checked
            lines.append("Planned-works check: not run yet")
        directions.append(
            {
                "direction": d.direction.value,
                "days": [
                    {"date": w.service_date.isoformat(), "leg": w.leg_index}
                    for w in v.works.get(key, ())
                ],
                "last_ok": None if ok is None else ok.astimezone(TZ).isoformat(),
                "stale": ok is None or _stale(v, ok),
            }
        )
    if not any(x["stale"] or x["days"] for x in directions):
        lines.append(NO_WORKS)
    lines += [*warning_lines(v, confirmed), "Data: NS"]
    return {"profile": profile_id, "directions": directions, "text": "\n".join(lines)}


def answer(view: RuntimeView, confirmed: bool, request: object) -> dict[str, Any]:
    """One MCP request, validated at the trust boundary; every error is a fixed text or
    `parse_target`'s own, which may repeat only the caller's own profile text."""
    if not isinstance(request, dict) or not isinstance(profile := request.get("profile"), str):
        return {"error": "Bad request."}
    tool = request.get("tool")
    if not isinstance(tool, str) or tool not in TOOLS:
        return {"error": "Unknown tool."}
    direction = next((d for d in Direction if d.value == request.get("direction")), None)
    if tool == "get_next_connections" and direction is None:
        return {"error": "Direction must be outbound or return."}
    target = parse_target(view, [f"p={profile}"] if profile.strip() else [])
    if isinstance(target, str):
        return {"error": target}
    pid = target.profile_id
    if tool == "get_status":
        return status_tool(view, confirmed, pid)
    if tool == "get_works_ahead":
        return works_tool(view, confirmed, pid)
    assert direction is not None  # checked above for get_next_connections
    return connections_tool(view, confirmed, pid, direction)


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


def render_webhook_tests(results: Sequence[WebhookTestResult]) -> str:
    return "\n".join(
        f"Webhook {r.host}: accepted the loud and the silent test."
        if r.ok
        else f"Webhook {r.summary()}: not accepted. Check the receiver, its secret and its token."
        for r in results
    )
