"""Renders the runtime view for Telegram and MCP, pure: parsing a target (7.8), rendering
`/status` (7.13), the `/test` webhook results and the read-only MCP answers (9 step 5), each
MCP answer built from a per-profile copy of the view by the functions `/status` uses. No I/O."""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from train_commute_companion.calendar import TZ, hhmm
from train_commute_companion.catalogue import (
    MessageLanguage,
    common_language,
    message,
    message_date,
)
from train_commute_companion.health import BoardHealth
from train_commute_companion.messages import Refused, crowd_text
from train_commute_companion.model import (
    Config,
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
from train_commute_companion.providers.weather import weather_text
from train_commute_companion.recovery import render as render_recovery
from train_commute_companion.recovery_state import RecoveryOffer, SelectedRecovery
from train_commute_companion.serde import encode
from train_commute_companion.vehicle import render_snapshot, snapshot_payload
from train_commute_companion.view import LegView, OccurrenceView, RecoveryPlanView, RuntimeView
from train_commute_companion.webhook import WebhookTestResult

from .telegram import callback_data, map_buttons

if TYPE_CHECKING:
    from train_commute_companion.runtime import Runtime

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
    return message_date(d, "en")


def shared_language(view: RuntimeView) -> MessageLanguage:
    return common_language(p.alerts.language for p in view.profiles)


def profile_language(view: RuntimeView, profile_id: str) -> MessageLanguage:
    return next((p.alerts.language for p in view.profiles if p.id == profile_id), "en")


def _match_profile_token(
    profiles: Collection[tuple[str, str]], token: str
) -> tuple[str, str] | None:
    """By id first, then by name; `profiles` holds (id, name) pairs."""
    return next((p for p in profiles if p[0] == token), None) or next(
        (p for p in profiles if _key(p[1]) == _key(token)), None
    )


def _named_profile(
    profiles: Collection[tuple[str, str]],
    args: Sequence[str],
    named: str,
    names: str,
    language: MessageLanguage,
) -> tuple[tuple[str, str], Sequence[str]]:
    """The profile `p=...` names and the arguments without the words of its name."""
    at = args.index(named)
    # a spaced name arrives split by whitespace: take the longest run of words that names a profile
    for end in range(len(args), at, -1):
        profile = _match_profile_token(profiles, " ".join([named[2:], *args[at + 1 : end]]))
        if profile is not None:
            return profile, [a for j, a in enumerate(args) if not at < j < end]
    raise ValueError(message("command.profile_missing", language, name=named[2:], names=names))


def _time_or_date(token: str, language: MessageLanguage) -> time | date | None:
    """None when the token has neither shape; shaped but impossible (25:99) is a refusal."""
    try:
        if _TIME.fullmatch(token):
            return time.fromisoformat(token.zfill(5))
        if _DATE.fullmatch(token):
            return date.fromisoformat(token)
    except ValueError as e:
        raise ValueError(message("command.unknown_input", language, input=token)) from e
    return None


def parse_target(view: RuntimeView, args: Sequence[str]) -> Target:
    """The command's target; a ValueError whose message is the reply when it names none."""
    profiles = dict.fromkeys((d.profile_id, d.profile_name) for d in view.directions)
    language = shared_language(view)
    names = ", ".join(n for _, n in profiles)
    profile: tuple[str, str] | None = None
    named = next((a for a in args if a.startswith("p=")), None)
    if named is not None:
        profile, args = _named_profile(profiles, args, named, names, language)
        language = profile_language(view, profile[0])
    direction = service_date = departure = train = None
    whole_day = False
    for i, token in enumerate(args):
        if token == named:
            continue
        when = _time_or_date(token, language)
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
            language = profile_language(view, p[0])
        elif token.isdigit():
            train = token
        elif i == 0 and named is None:  # a lone unknown first word is taken as a profile
            raise ValueError(message("command.profile_missing", language, name=token, names=names))
        else:
            raise ValueError(message("command.unknown_input", language, input=token))
    if profile is None:
        if len(profiles) != 1:
            raise ValueError(message("command.profile_which", language, names=names))
        profile = next(iter(profiles))
    return Target(profile[0], profile[1], direction, service_date, departure, train, whole_day)


def _leg_line(
    x: LegView, language: MessageLanguage, prefix: str = "  ", *, for_pin: bool = False
) -> str:
    line = prefix + message(
        "leg.line",
        language,
        time=hhmm(x.departure or x.planned_departure),
        board=x.board,
        alight=x.alight,
        state=message(f"leg.{x.state.value}", language),
    )
    if x.departure_delay_min:
        line += f", +{x.departure_delay_min} min"
    if x.departure_platform:
        line += message("leg.platform", language, track=x.departure_platform)
    if for_pin:
        line += message("leg.number", language, number=x.train_number)
        if x.arrival is not None:
            line += message("leg.reported_arrival", language, time=hhmm(x.arrival))
        elif x.planned_arrival is not None:
            line += message("leg.planned_arrival", language, time=hhmm(x.planned_arrival))
        if x.arrival_delay_min:
            line += f" (+{x.arrival_delay_min} min)"
        if x.arrival_platform:
            line += message("leg.arrival_platform", language, track=x.arrival_platform)
    if x.crowd_forecast is not None:
        line += message("leg.crowd", language, level=x.crowd_forecast)
    if x.exit_side is not None:
        line += message("leg.exit_side", language, side=x.exit_side)
    for label, notes in (("boarding", x.boarding_notes), ("alighting", x.alighting_notes)):
        if notes:
            line += message(f"leg.{label}_notes", language, notes="; ".join(notes))
    if x.state in (LegKind.OK, LegKind.DELAYED):
        if x.departure is None:
            line += message("leg.no_departure", language)
        if x.arrival is None:
            line += message("leg.no_arrival", language)
    return line


def _next_time(c: Connection) -> datetime:
    b = c.legs[0].board
    reported = b.departure.value if b.departure is not None else None
    return reported or b.planned_departure or c.legs[0].train.planned_departure


def _occurrence(
    o: OccurrenceView, *, for_pin: bool = False, language: MessageLanguage = "en"
) -> list[str]:
    k = o.key
    recovering = o.selected_recovery is not None and o.selected_recovery.status == "active"
    state = message(
        "journey.recovering" if recovering else f"journey.{o.direction_state.value}", language
    )
    direction = message(f"direction.{k.direction.value}", language)
    head = f"{display_name(o.profile_name)} {direction} {message_date(k.service_date, language)}: {state}"
    if o.not_travelling:
        head += message("journey.not_travelling", language)
    if o.route_changed:
        head += message("journey.route_changed", language)
    out = [head]
    if o.arrive_by is not None and o.station_arrival_deadline is not None:
        target = message(
            "arrival.target",
            language,
            target=hhmm(o.arrive_by),
            deadline=hhmm(o.station_arrival_deadline),
        )
        out.append(f"  {target}")
        if o.arrival_target_status in ("late", "unknown", "no_train"):
            out.append("  " + message(f"arrival.{o.arrival_target_status}", language))
    if (leave := o.format_leave_by(language)) is not None:
        out.append(f"  {leave}.")
    if recovering:
        state = message(f"journey.{o.direction_state.value}", language)
        out.append(message("recovery.original_state", language, state=state))
        out.append(message("recovery.original_legs", language))
    out.extend(_leg_line(x, language, for_pin=for_pin) for x in o.legs)
    for t in o.transfers:
        label = message("transfer.original_change" if recovering else "transfer.change", language)
        line = "  " + message(
            "transfer.line",
            language,
            label=label,
            station=t.station,
            state=message(f"transfer.{t.state.value}", language),
        )
        if t.margin_min is not None:
            line += message("transfer.margin", language, margin=t.margin_min, minimum=t.minimum)
        out.append(line)
    for journey in o.retained:
        if recovering:
            out.append(message("recovery.original_retained", language))
        out.extend(
            _leg_line(
                x,
                language,
                message("status.retained_leg", language, time=hhmm(x.planned_departure)),
                for_pin=for_pin,
            )
            for x in journey
        )
    if o.next_good is not None and not recovering:
        leg = o.next_good.legs[0]
        number = message("leg.number", language, number=leg.train.train_number) if for_pin else ""
        track = leg.board.actual_track or leg.board.planned_track
        if for_pin and track:
            number += message("leg.platform", language, track=track)
        out.append(
            message(
                "status.next_connection",
                language,
                time=hhmm(_next_time(o.next_good)),
                station=leg.board.code,
                details=number,
            )
            + crowd_text(o.next_good.legs, language)
        )
    if (
        not recovering
        and o.advice is not None
        and o.advice.advised_train is not None
        and not o.advice.withdrawn
    ):
        train = o.advice.advised_train
        number = message("leg.number", language, number=train.train_number) if for_pin else ""
        out.append(
            message(
                "status.earlier_train",
                language,
                time=hhmm(train.planned_departure),
                details=number,
            )
        )
    for plan in o.replacement_plans:
        checked = (
            "" if for_pin else message("recovery.checked", language, time=hhmm(plan.checked_at))
        )
        out.append(message("recovery.plan", language, checked=checked))
        out.extend("  " + line for line in render_recovery(plan.itinerary, language).splitlines())
    if o.recovery_offer is not None:
        out.extend(
            "  " + line
            for line in render_recovery_offer(
                o.recovery_offer, language, for_pin=for_pin
            ).splitlines()
        )
    if o.selected_recovery is not None:
        out.extend(
            "  " + line
            for line in render_selected_recovery(
                o.selected_recovery, language, for_pin=for_pin
            ).splitlines()
        )
    if not for_pin and o.train_context is not None:
        if recovering:
            out.append(message("recovery.original_vehicle", language))
        out.extend("  " + line for line in render_snapshot(o.train_context).splitlines())
    if not for_pin:
        out.extend("  " + weather_text(w, language) for w in o.weather)
    for disruption in o.disruptions:
        out.append(message("status.disruption", language, title=disruption.title))
    if warning := _disruption_warning(o, language):
        out.append(warning)
    out.append(_health_line(o.health, for_pin=for_pin, language=language))
    return out


def _disruption_warning(occurrence: OccurrenceView, language: MessageLanguage = "en") -> str | None:
    if (
        occurrence.disruptions_checked_at is not None
        and occurrence.disruptions_coverage != "complete"
    ):
        return message("status.disruptions_unknown", language)
    return None


def _health_line(h: BoardHealth, *, for_pin: bool = False, language: MessageLanguage = "en") -> str:
    if h.fresh:
        key = "status.health_poll_failed" if h.consecutive_failures else "status.health_ok"
        return message(key, language)
    return (
        message("status.health_stale_since", language, time=hhmm(h.last_ok_at))
        if h.last_ok_at and not for_pin
        else message("status.health_stale", language)
    )


def _warnings(view: RuntimeView, *, for_pin: bool = False) -> list[str]:
    language = shared_language(view)
    lines = []
    if view.refusal:
        lines.append(message("status.profile_refused", language, reason=view.refusal))
    if view.key_rejected:
        lines.append(message("status.key_rejected", language))
    if not view.store_ok:
        lines.append(message("status.store_failed", language))
    if view.undelivered and not for_pin:
        lines.append(message("status.pending_alerts", language, count=view.undelivered))
    return lines


def warning_lines(view: RuntimeView, confirmed: bool) -> list[str]:
    """Every warning `/status` shows for this view, in its wording, so no MCP tool reads as all
    good while `/status` does not."""
    language = shared_language(view)
    health = [_health_line(o.health, language=language) for o in view.occurrences if not o.resolved]
    return [
        *dict.fromkeys(x for x in health if x != message("status.health_ok", language)),
        *dict.fromkeys(
            w
            for o in view.occurrences
            if not o.resolved and (w := _disruption_warning(o, language))
        ),
        *_warnings(view),
        *(m.text for m in view.moved),
        *_check_lines(view),
        *([] if confirmed else [message("status.delivery_unconfirmed", language)]),
    ]


def _stale(view: RuntimeView, ok: datetime) -> bool:
    return view.at.astimezone(UTC) - ok.astimezone(UTC) > LOOKAHEAD_STALE


def works_lines(view: RuntimeView) -> list[str]:
    return _works_ahead(view) + _check_lines(view)


def _works_ahead(view: RuntimeView) -> list[str]:
    names = {d.profile_id: d.profile_name for d in view.directions}
    language = shared_language(view)
    lines = []
    for key, days in view.works.items():
        pid, d = split_direction_key(key)
        if days:
            dates = ", ".join(message_date(w.service_date, language) for w in days)
            lines.append(
                message(
                    "status.works_ahead",
                    language,
                    name=display_name(names.get(pid, pid)),
                    direction=message(f"direction.{d}", language),
                    dates=dates,
                )
            )
    return lines


def _check_lines(view: RuntimeView, *, for_pin: bool = False) -> list[str]:
    lines = []
    language = shared_language(view)
    for ok in view.lookahead_last_ok.values():
        if ok is None:
            lines.append(message("status.works_not_run", language))
        elif _stale(view, ok):
            lines.append(
                message("status.works_stale", language)
                if for_pin
                else message(
                    "status.works_last_ok",
                    language,
                    date=message_date(ok.astimezone(TZ).date(), language),
                    time=hhmm(ok),
                )
            )
    return lines


def render_status(view: RuntimeView, confirmed: bool, *, for_pin: bool = False) -> str:
    pending = [o for o in view.occurrences if not o.resolved]
    language = shared_language(view)
    languages = {p.id: p.alerts.language for p in view.profiles}
    lines = [
        line
        for o in pending
        for line in _occurrence(
            o, for_pin=for_pin, language=languages.get(o.key.profile_id, language)
        )
    ]
    lines += _warnings(view, for_pin=for_pin)
    lines += [m.text for m in view.moved]
    works, checks = _works_ahead(view), _check_lines(view, for_pin=for_pin)
    # State storage sorts mapping keys, so its order can change across a restart.
    lines += sorted(works) + sorted(checks) if for_pin else works + checks
    if not confirmed:
        lines.append(message("status.delivery_unconfirmed", language))
    if not pending:
        lines.append(message("status.empty", language))
    lines.append(message("status.source", language))
    return "\n".join(lines)


def view_for_profile(view: RuntimeView, profile_id: str) -> RuntimeView:
    return view_for_profiles(view, {profile_id})


def view_for_profiles(view: RuntimeView, profile_ids: set[str]) -> RuntimeView:
    """The view as one profile alone would see it; the refusal text echoes rejected values and
    read errors, so it is replaced by a fixed text."""
    keys = {direction_key(pid, d) for pid in profile_ids for d in Direction}
    return replace(
        view,
        profiles=tuple(p for p in view.profiles if p.id in profile_ids),
        directions=tuple(d for d in view.directions if d.profile_id in profile_ids),
        occurrences=tuple(o for o in view.occurrences if o.key.profile_id in profile_ids),
        moved=tuple(m for m in view.moved if m.profile_id in profile_ids),
        works={k: v for k, v in view.works.items() if k in keys},
        lookahead_last_ok={k: v for k, v in view.lookahead_last_ok.items() if k in keys},
        refusal=REFUSAL_HIDDEN if view.refusal else None,
    )


def _name(view: RuntimeView, profile_id: str) -> str:
    return next(d.profile_name for d in view.directions if d.profile_id == profile_id)


def status_tool(view: RuntimeView, confirmed: bool, profile_id: str) -> dict[str, Any]:
    scoped = view_for_profile(view, profile_id)
    return {
        "profile": profile_id,
        "name": display_name(_name(view, profile_id)),
        "as_of": scoped.at.isoformat(),
        "journeys": [_status_journey(o, scoped.at) for o in scoped.occurrences if not o.resolved],
        "text": render_status(scoped, confirmed),
    }


def _board_status(occurrence: OccurrenceView) -> dict[str, Any]:
    return {
        "timetable_last_ok_at": encode(occurrence.health.last_ok_at),
        "timetable_health": occurrence.health.phase.value,
    }


def _status_journey(occurrence: OccurrenceView, at: datetime) -> dict[str, Any]:
    o = occurrence
    recovering = _recovery_active(o)
    return {
        "direction": o.key.direction.value,
        "service_date": o.key.service_date.isoformat(),
        "state": "recovery_active" if recovering else o.direction_state.value,
        "not_travelling": o.not_travelling,
        **_board_status(o),
        "legs": [] if recovering else encode(o.legs),
        "original_legs": encode(o.legs) if recovering else [],
        "selected_recovery": o.selected_recovery.fields(at) if o.selected_recovery else None,
        "leave_by": None if recovering else encode(o.leave_by),
        "arrive_by": encode(o.arrive_by),
        "station_arrival_deadline": encode(o.station_arrival_deadline),
        "arrival_target_status": o.arrival_target_status,
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
    language = shared_language(v)
    occs = sorted(
        (o for o in v.occurrences if not o.resolved and o.key.direction is direction),
        key=lambda o: o.key.service_date,
    )
    journeys = [
        {
            "service_date": o.key.service_date.isoformat(),
            "state": "recovery_active" if _recovery_active(o) else o.direction_state.value,
            "original_train_state": o.direction_state.value,
            **_board_status(o),
            "next_good": None
            if o.next_good is None or _recovery_active(o)
            else _connection(o.next_good),
            "replacement_plans": [plan.fields() for plan in o.replacement_plans],
            "recovery_offer": None if o.recovery_offer is None else o.recovery_offer.fields(),
            "selected_recovery": None
            if o.selected_recovery is None
            else o.selected_recovery.fields(v.at),
            "train_context": None if o.train_context is None else snapshot_payload(o.train_context),
            "weather": [{**encode(w), "text": weather_text(w, language)} for w in o.weather],
            "leave_by": None if _recovery_active(o) else encode(o.leave_by),
            "weather_allowance": encode(o.weather_allowance),
        }
        for o in occs
    ]
    lines = [x for o in occs for x in _occurrence(o, language=language)] or [
        message("status.empty", language)
    ]
    lines += [x for x in warning_lines(v, confirmed) if x not in lines]  # health shown once
    lines.append(message("status.source", language))
    return {
        "profile": profile_id,
        "direction": direction.value,
        "journeys": journeys,
        "text": "\n".join(lines),
    }


def regular_schedules(config: Config | None) -> list[dict[str, Any]]:
    return [
        {
            "profile_id": p.id,
            "name": display_name(p.name),
            "origin": p.home_station,
            "destination": p.work_station,
            "weekdays": sorted(p.days),
            "repeat": encode(p.repeat),
            "extra_dates": encode(p.extra_dates),
            "pauses": encode(p.pauses),
            "skip_public_holidays": p.skip_public_holidays,
            "outbound": encode(p.outbound),
            "return": encode(p.return_),
        }
        for p in (config.profiles if config else ())
        if p.home_station != p.work_station
    ]


async def travel_connections_tool(rt: "Runtime", request: dict[str, Any]) -> dict[str, Any]:
    profile, plan = request.get("profile"), request.get("plan_id")
    if profile is not None and plan is not None:
        return {"error": "Supply one travel plan ID in profile or plan_id."}
    plan_id = plan if plan is not None else profile
    if not isinstance(plan_id, str) or not plan_id:
        return {"error": "A saved travel plan ID is required."}
    direction = next((d for d in Direction if d.value == request.get("direction")), None)
    if direction is None:
        return {"error": "Direction must be outbound or return."}
    date_text = request.get("date")
    day = None
    if date_text is not None:
        try:
            if not isinstance(date_text, str) or not _DATE.fullmatch(date_text):
                raise ValueError
            day = date.fromisoformat(date_text)
        except ValueError:
            return {"error": "Use a date in YYYY-MM-DD form."}
    preview = await rt.preview_travel_connection(plan_id, direction, day)
    if isinstance(preview, Refused):
        return {"error": preview.text}
    day, schedule, source, board, connection = preview
    status = "available"
    if board is None:
        status, text = "unavailable", "NS timetable data is unavailable. Try again."
    elif connection is None:
        status, text = "no_match", "NS returned no connection that fits this travel plan."
    else:
        text = f"Next planned connection: {_next_time(connection).astimezone(TZ):%H:%M}."
    return {
        "profile": plan_id,
        "plan_id": plan_id,
        "direction": direction.value,
        "date": day.isoformat(),
        "source": source,
        "status": status,
        "fetched_at": board.fetched_at.isoformat() if board else None,
        "journeys": [
            {
                "service_date": schedule.key.service_date.isoformat(),
                "state": "preview",
                "next_good": _connection(connection) if connection else None,
            }
        ],
        "text": text + "\nData: NS",
    }


def works_tool(view: RuntimeView, confirmed: bool, profile_id: str) -> dict[str, Any]:
    v = view_for_profile(view, profile_id)
    language = shared_language(v)
    lines = _works_ahead(v)
    directions = []
    for d in v.directions:
        key = direction_key(profile_id, d.direction)
        ok = v.lookahead_last_ok.get(key)
        if key not in v.lookahead_last_ok:  # a removed profile still running: never checked
            lines.append(message("status.works_not_run", language))
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
        lines.append(message("status.no_works", language))
    lines += [*warning_lines(v, confirmed), message("status.source", language)]
    return {"profile": profile_id, "directions": directions, "text": "\n".join(lines)}


def answer(view: RuntimeView, confirmed: bool, request: object) -> dict[str, Any]:
    """One MCP request, validated at the trust boundary; every error is a fixed text or
    `parse_target`'s own, which may repeat only the caller's own profile text."""
    if (
        isinstance(request, dict)
        and request.get("tool") == "get_next_connections"
        and (request.get("date") is not None or request.get("plan_id") is not None)
    ):
        return {"error": "Connect a registered agent to preview saved travel connections."}
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
        language = profile_language(view, k.profile_id)
        day = k.service_date.isoformat()
        row = [
            {"text": message(label, language), "callback_data": data}
            for label, tag in (("button.leaving", "l"), ("button.skip", "s"))
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
                row.append(
                    {
                        "text": message(
                            "button.take", language, time=hhmm(_next_time(o.next_good))
                        ),
                        "callback_data": data,
                    }
                )
        if row:
            rows.append(row)
        if confirmed and not o.not_travelling:
            if o.navigation:
                rows.append(map_buttons(o.navigation))
            if o.selected_recovery is not None and o.selected_recovery.status == "active":
                rows.extend(
                    recovery_progress_buttons(o.selected_recovery, language)["inline_keyboard"]
                )
            else:
                for plan in o.replacement_plans:
                    if plan.selectable and re.fullmatch(r"[0-9a-f]{32}", plan.proposal_id):
                        rows.append(
                            [
                                {
                                    "text": message("recovery.review_origin", language),
                                    "callback_data": f"rp|{plan.proposal_id}",
                                }
                            ]
                        )
                if o.recovery_offer is not None:
                    rows.extend(
                        recovery_offer_buttons(o.recovery_offer, language)["inline_keyboard"]
                    )
    return {"inline_keyboard": rows} if rows else None


def recovery_origins(plan: RecoveryPlanView) -> tuple[int, ...]:
    """Only unique physical IDs can be passed unambiguously to the runtime."""
    ids = [step.origin.physical_id for step in plan.itinerary.steps]
    return tuple(i for i, identifier in enumerate(ids) if identifier and ids.count(identifier) == 1)


def _recovery_active(occurrence: OccurrenceView) -> bool:
    selected = occurrence.selected_recovery
    return selected is not None and selected.status == "active"


def recovery_origin_prompt(
    plan: RecoveryPlanView, language: MessageLanguage = "en"
) -> tuple[str, dict[str, Any]]:
    rows, lines = [], [message("recovery.origin_prompt", language)]
    for i in recovery_origins(plan):
        place = plan.itinerary.steps[i].origin
        identifier = message("recovery.physical_id", language, id=place.physical_id)
        lines.append(f"{place.name}: {place.latitude:.6f}, {place.longitude:.6f} ({identifier})")
        rows.append(
            [
                {
                    "text": message("recovery.at", language, place=place.name),
                    "callback_data": f"ra|{plan.proposal_id}|{i}",
                }
            ]
        )
    if not rows:
        lines.append(message("recovery.no_origin", language))
    return "\n".join(lines), {"inline_keyboard": rows}


def render_recovery_offer(
    offer: RecoveryOffer, language: MessageLanguage = "en", *, for_pin: bool = False
) -> str:
    header = (
        message("recovery.refreshed", language)
        if for_pin
        else message("recovery.refreshed_at", language, time=hhmm(offer.checked_at))
    )
    return "\n".join(
        (
            header,
            render_recovery(offer.itinerary, language),
            message("recovery.follow_accepts", language),
            message("recovery.progress_limits", language),
        )
    )


def recovery_offer_buttons(
    offer: RecoveryOffer, language: MessageLanguage = "en"
) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": message("recovery.follow", language),
                    "callback_data": f"rf|{offer.id}",
                }
            ]
        ]
    }


def render_selected_recovery(
    selected: SelectedRecovery, language: MessageLanguage = "en", *, for_pin: bool = False
) -> str:
    status = message(f"recovery.{selected.status}", language)
    lines = [message("recovery.progress", language, status=status)]
    last = next(
        (action for action in reversed(selected.confirmations) if action.place is not None), None
    )
    place, at = (
        (last.place, last.at) if last is not None else (selected.origin.place, selected.origin.at)
    )
    assert place is not None
    lines.append(message("recovery.last_place", language, place=place.name, time=hhmm(at)))
    if selected.next_action is not None:
        step = selected.itinerary.steps[selected.index]
        times = f" {hhmm(step.departure)}-{hhmm(step.arrival)}" if for_pin else ""
        lines.append(
            message(
                "recovery.current_step",
                language,
                times=times,
                mode=step.mode,
                operator=step.operator or "",
                service=step.service_reference or "",
                origin=step.origin.name,
                destination=step.destination.name,
            )
        )
        action = message(f"recovery.{selected.next_action}", language)
        lines.append(message("recovery.next", language, action=action))
    if selected.monitoring_error:
        lines.append(message("recovery.refresh_failed", language))
    if selected.review_required:
        lines.append(message("recovery.review", language))
    lines.append(message("recovery.accessibility", language))
    return "\n".join(lines)


def recovery_progress_buttons(
    selected: SelectedRecovery, language: MessageLanguage = "en"
) -> dict[str, Any]:
    rows = []
    action = selected.next_action
    if action is not None:
        step = selected.itinerary.steps[selected.index]
        label = (
            message(
                "recovery.i_boarded",
                language,
                operator=step.operator or "",
                mode=step.mode,
                service=step.service_reference or "",
            ).strip()
            if action == "boarded"
            else message("recovery.i_arrived", language, place=step.destination.name)
        )
        code = {"boarded": "b", "arrived": "a", "walk_arrived": "w"}[action]
        data = callback_data("rc", selected.id, str(selected.revision), str(selected.index), code)
        if data is not None:
            rows.append([{"text": label, "callback_data": data}])
        stop = callback_data("rc", selected.id, str(selected.revision), str(selected.index), "s")
        if stop is not None:
            rows.append([{"text": message("recovery.stop", language), "callback_data": stop}])
    return {"inline_keyboard": rows}


def stations_text(view: RuntimeView) -> str:
    """The boarding stations TCC remembers, per profile and direction."""
    lines = [
        f"{display_name(i.profile_name)} {i.direction.value}: {', '.join(codes)}"
        for i in view.directions
        if (codes := view.remembered.get(direction_key(i.profile_id, i.direction)))
    ]
    if not lines:
        return "No remembered boarding stations."
    return "Remembered boarding stations:\n" + "\n".join(lines)


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
