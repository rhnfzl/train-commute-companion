"""Renders the runtime view for Telegram and MCP, pure: parsing a target (7.8), rendering
`/status` (7.13), the `/test` webhook results and the read-only MCP answers (9 step 5), each
MCP answer built from a per-profile copy of the view by the functions `/status` uses. No I/O."""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from train_commute_companion.calendar import TZ, hhmm
from train_commute_companion.health import BoardHealth
from train_commute_companion.messages import crowd_text
from train_commute_companion.model import (
    Connection,
    Direction,
    LegKind,
    OccurrenceKey,
    Provenance,
    TimeObs,
    direction_key,
    display_name,
    present,
    split_direction_key,
)
from train_commute_companion.recovery import render as render_recovery
from train_commute_companion.recovery_state import RecoveryOffer, SelectedRecovery
from train_commute_companion.vehicle import render_snapshot, snapshot_payload
from train_commute_companion.view import LegView, OccurrenceView, RecoveryPlanView, RuntimeView
from train_commute_companion.webhook import WebhookTestResult

from .telegram import callback_data

LOOKAHEAD_STALE = timedelta(hours=25)  # a daily run missed or failed
TOOLS = ("get_status", "get_next_connections", "get_works_ahead")
REFUSAL_HIDDEN = "details are in Telegram /status and the app log"
# the check probes at most 3 commute days within 10 days (7.10)
NO_WORKS = "No planned works found in the commute days the last check covered."
_TIME = re.compile(r"\d{1,2}:\d{2}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True, slots=True)
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


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _match_profile_token(
    profiles: Collection[tuple[str, str]], token: str
) -> tuple[str, str] | None:
    """By id first, then by name; `profiles` holds (id, name) pairs."""
    return next((p for p in profiles if p[0] == token), None) or next(
        (p for p in profiles if _key(p[1]) == _key(token)), None
    )


def _named_profile(
    profiles: Collection[tuple[str, str]], args: Sequence[str], named: str, names: str
) -> tuple[tuple[str, str], Sequence[str]]:
    """The profile `p=...` names and the arguments without the words of its name."""
    at = args.index(named)
    # a spaced name arrives split by whitespace: take the longest run of words that names a profile
    for end in range(len(args), at, -1):
        profile = _match_profile_token(profiles, " ".join([named[2:], *args[at + 1 : end]]))
        if profile is not None:
            return profile, [a for j, a in enumerate(args) if not at < j < end]
    raise ValueError(f"No profile {named[2:]}. One of: {names}.")


def _time_or_date(token: str) -> time | date | None:
    """None when the token has neither shape; shaped but impossible (25:99) is a refusal."""
    try:
        if _TIME.fullmatch(token):
            return time.fromisoformat(token.zfill(5))
        if _DATE.fullmatch(token):
            return date.fromisoformat(token)
    except ValueError as e:
        raise ValueError(f"I did not understand {token}.") from e
    return None


def parse_target(view: RuntimeView, args: Sequence[str]) -> Target:
    """The command's target; a ValueError whose message is the reply when it names none."""
    profiles = dict.fromkeys((d.profile_id, d.profile_name) for d in view.directions)
    names = ", ".join(n for _, n in profiles)
    profile: tuple[str, str] | None = None
    named = next((a for a in args if a.startswith("p=")), None)
    if named is not None:
        profile, args = _named_profile(profiles, args, named, names)
    direction = service_date = departure = train = None
    whole_day = False
    for i, token in enumerate(args):
        if token == named:
            continue
        when = _time_or_date(token)
        if isinstance(when, time):
            departure = when
        elif isinstance(when, date):
            service_date = when
        elif token.lower() in (d.value for d in Direction):
            direction = Direction(token.lower())
        elif token.lower() == "day":
            whole_day = True
        elif (p := _match_profile_token(profiles, token)) is not None and profile is None:
            profile = p
        elif token.isdigit():
            train = token
        elif i == 0 and named is None:  # a lone unknown first word is taken as a profile
            raise ValueError(f"No profile {token}. One of: {names}.")
        else:
            raise ValueError(f"I did not understand {token}.")
    if profile is None:
        if len(profiles) != 1:
            raise ValueError(f"Which profile? One of: {names}.")
        profile = next(iter(profiles))
    return Target(profile[0], profile[1], direction, service_date, departure, train, whole_day)


def _leg_line(x: LegView, prefix: str = "  ") -> str:
    state = "on time per NS" if x.state is LegKind.OK else x.state  # never read as proven (rule 20)
    line = f"{prefix}{hhmm(x.departure or x.planned_departure)} {x.board} to {x.alight}: {state}"
    if x.departure_delay_min:
        line += f", +{x.departure_delay_min} min"
    if x.departure_platform:
        line += f", platform {x.departure_platform}"
    if x.crowd_forecast is not None:
        line += f", crowd forecast: {x.crowd_forecast} per NS"
    if x.exit_side is not None:
        line += f", exit side: {x.exit_side} per NS"
    for label, notes in (("boarding", x.boarding_notes), ("alighting", x.alighting_notes)):
        if notes:
            line += f", {label} notes per NS: " + "; ".join(notes)
    if x.state in (LegKind.OK, LegKind.DELAYED):
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
    recovering = o.selected_recovery is not None and o.selected_recovery.status == "active"
    state = "recovery active" if recovering else o.direction_state.value
    head = f"{display_name(o.profile_name)} {k.direction.value} {_day(k.service_date)}: {state}"
    if o.not_travelling:
        head += " (not travelling)"
    if o.route_changed:
        head += " (route changed)"
    out = [head]
    if recovering:
        out.append(f"  Original train monitoring state: {o.direction_state.value}.")
        out.append("  Original train legs (separate from selected recovery):")
    out.extend(_leg_line(x) for x in o.legs)
    for t in o.transfers:
        label = "original train change" if recovering else "change"
        line = f"  {label} at {t.station}: {t.state.value}"
        if t.margin_min is not None:
            line += f" ({t.margin_min} min, needs {t.minimum})"
        out.append(line)
    for journey in o.retained:
        if recovering:
            out.append("  Original retained train warnings:")
        out += [_leg_line(x, f"  if you are on the {hhmm(x.planned_departure)}: ") for x in journey]
    if o.next_good is not None and not recovering:
        out.append(
            f"  next good connection: {hhmm(_next_time(o.next_good))} from {o.next_good.legs[0].board.code}"
            + crowd_text(o.next_good.legs)
        )
    if (
        not recovering
        and o.advice is not None
        and o.advice.advised_train is not None
        and not o.advice.withdrawn
    ):
        out.append(f"  earlier train advised: the {hhmm(o.advice.advised_train.planned_departure)}")
    for plan in o.replacement_plans:
        out.append(
            f"  NS replacement plan checked {hhmm(plan.checked_at)}; starting position not confirmed:"
        )
        out.extend("  " + line for line in render_recovery(plan.itinerary).splitlines())
    if o.recovery_offer is not None:
        out.extend("  " + line for line in render_recovery_offer(o.recovery_offer).splitlines())
    if o.selected_recovery is not None:
        out.extend(
            "  " + line for line in render_selected_recovery(o.selected_recovery).splitlines()
        )
    if o.train_context is not None:
        if recovering:
            out.append("  Original train vehicle context (not the selected recovery):")
        out.extend("  " + line for line in render_snapshot(o.train_context).splitlines())
    for disruption in o.disruptions:
        out.append(
            f"  NS disruption context: {disruption.title} Possible route impact; check your journey."
        )
    if warning := _disruption_warning(o):
        out.append(warning)
    out.append(_health_line(o.health))
    return out


def _disruption_warning(occurrence: OccurrenceView) -> str | None:
    if (
        occurrence.disruptions_checked_at is not None
        and occurrence.disruptions_coverage != "complete"
    ):
        return "  NS active-disruption data: unavailable; previous context may be stale."
    return None


def _health_line(h: BoardHealth) -> str:
    if h.fresh:
        return (
            "  NS data: fresh, last poll failed" if h.consecutive_failures else "  NS data: fresh"
        )
    return (
        f"  NS data: not fresh since {hhmm(h.last_ok_at)}"
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
    good while `/status` does not."""
    health = [_health_line(o.health) for o in view.occurrences if not o.resolved]
    return [
        *dict.fromkeys(x for x in health if x != "  NS data: fresh"),
        *dict.fromkeys(
            w for o in view.occurrences if not o.resolved and (w := _disruption_warning(o))
        ),
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
        pid, d = split_direction_key(key)
        if days:
            dates = ", ".join(_day(w.service_date) for w in days)
            lines.append(f"Works ahead: {display_name(names.get(pid, pid))} {d}: {dates}")
    return lines


def _check_lines(view: RuntimeView) -> list[str]:
    lines = []
    for ok in view.lookahead_last_ok.values():
        if ok is None:
            lines.append("Planned-works check: not run yet")
        elif _stale(view, ok):
            lines.append(
                f"Planned-works check: last ok {_day(ok.astimezone(TZ).date())} {hhmm(ok)}"
            )
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


def view_for_profile(view: RuntimeView, profile_id: str) -> RuntimeView:
    """The view as one profile alone would see it; the refusal text echoes rejected values and
    read errors, so it is replaced by a fixed text."""
    keys = {direction_key(profile_id, d) for d in Direction}  # exact keys, never a prefix match
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
        "name": display_name(_name(view, profile_id)),
        "text": render_status(view_for_profile(view, profile_id), confirmed),
    }


def _reported_hm(obs: TimeObs | None) -> str | None:
    if obs is None or obs.provenance is not Provenance.REPORTED or obs.value is None:
        return None  # a planned time is never shown as a reported one (rule 20)
    return hhmm(obs.value)


def _connection(c: Connection) -> dict[str, Any]:
    legs = []
    for leg in c.legs:
        b, a = leg.board, leg.alight
        legs.append(
            {
                "train_number": leg.train.train_number,
                "board": b.code,
                "alight": a.code,
                "planned_departure": hhmm(b.planned_departure or leg.train.planned_departure),
                "departure": _reported_hm(b.departure),
                "planned_arrival": None if a.planned_arrival is None else hhmm(a.planned_arrival),
                "arrival": _reported_hm(a.arrival),
                "platform": b.actual_track or b.planned_track,
                "crowd_forecast": leg.crowd_forecast,
                "boarding_notes": list(b.accessibility_notes),
                "alighting_notes": list(a.accessibility_notes),
                "exit_side": a.exit_side,
            }
        )
    return {"departure": hhmm(_next_time(c)), "from": c.legs[0].board.code, "legs": legs}


def connections_tool(
    view: RuntimeView, confirmed: bool, profile_id: str, direction: Direction
) -> dict[str, Any]:
    v = view_for_profile(view, profile_id)
    occs = sorted(
        (o for o in v.occurrences if not o.resolved and o.key.direction is direction),
        key=lambda o: o.key.service_date,
    )
    journeys = [
        {
            "service_date": o.key.service_date.isoformat(),
            "state": "recovery_active" if _recovery_active(o) else o.direction_state.value,
            "original_train_state": o.direction_state.value,
            "next_good": None
            if o.next_good is None or _recovery_active(o)
            else _connection(o.next_good),
            "replacement_plans": [plan.fields() for plan in o.replacement_plans],
            "recovery_offer": None if o.recovery_offer is None else o.recovery_offer.fields(),
            "selected_recovery": None
            if o.selected_recovery is None
            else o.selected_recovery.fields(v.at),
            "train_context": None if o.train_context is None else snapshot_payload(o.train_context),
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
    v = view_for_profile(view, profile_id)
    lines = _works_ahead(v)
    directions = []
    for d in v.directions:
        key = direction_key(profile_id, d.direction)
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
    try:
        target = parse_target(view, [f"p={profile}"] if profile.strip() else [])
    except ValueError as e:
        return {"error": str(e)}
    pid = target.profile_id
    if tool == "get_status":
        return status_tool(view, confirmed, pid)
    if tool == "get_works_ahead":
        return works_tool(view, confirmed, pid)
    # checked above for get_next_connections
    return connections_tool(view, confirmed, pid, present(direction))


def status_buttons(view: RuntimeView, confirmed: bool = True) -> dict[str, Any] | None:
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
        if o.next_good is not None and not _recovery_active(o):
            # the core names a train by its planned departure; the label shows the reported one
            train = o.next_good.legs[0].train
            hm, num = hhmm(train.planned_departure), train.train_number
            data = callback_data(
                "c", k.profile_id, k.direction.value, day, hm, num
            ) or callback_data("c", k.profile_id, k.direction.value, day, hm)
            if data is not None:
                row.append({"text": f"Take {hhmm(_next_time(o.next_good))}", "callback_data": data})
        if row:
            rows.append(row)
        if confirmed and not o.not_travelling:
            if o.selected_recovery is not None and o.selected_recovery.status == "active":
                rows.extend(recovery_progress_buttons(o.selected_recovery)["inline_keyboard"])
            else:
                for plan in o.replacement_plans:
                    if plan.selectable and re.fullmatch(r"[0-9a-f]{32}", plan.proposal_id):
                        rows.append(
                            [
                                {
                                    "text": "Review recovery starting place",
                                    "callback_data": f"rp|{plan.proposal_id}",
                                }
                            ]
                        )
                if o.recovery_offer is not None:
                    rows.extend(recovery_offer_buttons(o.recovery_offer)["inline_keyboard"])
    return {"inline_keyboard": rows} if rows else None


def recovery_origins(plan: RecoveryPlanView) -> tuple[int, ...]:
    """Only unique physical IDs can be passed unambiguously to the runtime."""
    ids = [step.origin.physical_id for step in plan.itinerary.steps]
    return tuple(i for i, identifier in enumerate(ids) if identifier and ids.count(identifier) == 1)


def _recovery_active(occurrence: OccurrenceView) -> bool:
    selected = occurrence.selected_recovery
    return selected is not None and selected.status == "active"


def recovery_origin_prompt(plan: RecoveryPlanView) -> tuple[str, dict[str, Any]]:
    rows, lines = [], ["Confirm your exact physical starting place. No recovery is selected yet."]
    for i in recovery_origins(plan):
        place = plan.itinerary.steps[i].origin
        lines.append(
            f"{place.name}: {place.latitude:.6f}, {place.longitude:.6f} (physical ID {place.physical_id})"
        )
        rows.append(
            [{"text": f"I am at {place.name}", "callback_data": f"ra|{plan.proposal_id}|{i}"}]
        )
    if not rows:
        lines.append("No unambiguous physical starting place is available. Check the NS planner.")
    return "\n".join(lines), {"inline_keyboard": rows}


def render_recovery_offer(offer: RecoveryOffer) -> str:
    return (
        f"Refreshed recovery offer checked {hhmm(offer.checked_at)}.\n"
        + render_recovery(offer.itinerary)
        + "\nFollow accepts any planned-only times shown above and unverified accessibility and assistance."
        + "\nOnly your explicit boarding and arrival confirmations change progress."
    )


def recovery_offer_buttons(offer: RecoveryOffer) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Follow this recovery (accept limits above)",
                    "callback_data": f"rf|{offer.id}",
                }
            ]
        ]
    }


def render_selected_recovery(selected: SelectedRecovery) -> str:
    lines = [f"Recovery {selected.status}. Progress uses only your explicit confirmations."]
    last = next(
        (action for action in reversed(selected.confirmations) if action.place is not None), None
    )
    place, at = (
        (last.place, last.at) if last is not None else (selected.origin.place, selected.origin.at)
    )
    assert place is not None
    lines.append(f"Last confirmed place: {place.name} at {hhmm(at)}.")
    if selected.next_action is not None:
        step = selected.itinerary.steps[selected.index]
        lines.append(
            f"Current step: {step.mode} {step.operator or ''} {step.service_reference or ''}: {step.origin.name} to {step.destination.name}."
        )
        lines.append(f"Next confirmation: {selected.next_action.replace('_', ' ')}.")
    if selected.monitoring_error:
        lines.append(
            "Recovery data refresh failed. Previous times may be stale; manual confirmations remain available."
        )
    if selected.review_required:
        lines.append("NS changed the recovery route. Review the planner before continuing.")
    lines.append("Accessibility and assistance availability are not verified.")
    return "\n".join(lines)


def recovery_progress_buttons(selected: SelectedRecovery) -> dict[str, Any]:
    rows = []
    action = selected.next_action
    if action is not None:
        step = selected.itinerary.steps[selected.index]
        label = (
            f"I boarded {step.operator or ''} {step.mode} {step.service_reference or ''}".strip()
            if action == "boarded"
            else f"I reached {step.destination.name}"
        )
        code = {"boarded": "b", "arrived": "a", "walk_arrived": "w"}[action]
        data = callback_data("rc", selected.id, str(selected.revision), str(selected.index), code)
        if data is not None:
            rows.append([{"text": label, "callback_data": data}])
        stop = callback_data("rc", selected.id, str(selected.revision), str(selected.index), "s")
        if stop is not None:
            rows.append([{"text": "Stop recovery", "callback_data": stop}])
    return {"inline_keyboard": rows}


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


@dataclass(frozen=True, slots=True)
class ArrivalInput:
    key: OccurrenceKey
    destination: str
    at: datetime
    original_planned_arrival: datetime | None = None


def parse_arrival(args: Sequence[str]) -> ArrivalInput:
    """Read an explicit destination and full arrival instant, never a default current time."""
    usage = (
        "Usage: /arrived PROFILE outbound|return SERVICE-DATE DESTINATION "
        "YYYY-MM-DDTHH:MM+01:00 [planned=YYYY-MM-DDTHH:MM+01:00]. "
        "Use the actual timezone offset, +02:00 during summer time."
    )
    if len(args) not in (5, 6):
        raise ValueError(usage)
    try:
        pid, direction, day, destination, raw = args[:5]
        key = OccurrenceKey(pid, Direction(direction), date.fromisoformat(day))
        at = datetime.fromisoformat(raw)
        planned = None
        if len(args) == 6:
            if not args[5].startswith("planned="):
                raise ValueError
            planned = datetime.fromisoformat(args[5][8:])
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in (at, planned)
        ):
            raise ValueError
        return ArrivalInput(key, destination, at, planned)
    except ValueError as error:
        raise ValueError(usage) from error
