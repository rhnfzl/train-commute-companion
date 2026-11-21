"""Travel plan input, recurrence, and schedule comparisons."""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from itertools import islice
from typing import Any

from train_commute_companion.calendar import TZ, has_room, to_utc
from train_commute_companion.model import (
    AccessibilitySettings,
    AlertSettings,
    DatedSchedule,
    Direction,
    Profile,
    Stations,
    UsualJourney,
)
from train_commute_companion.recurrence import (
    MAX_RECURRENCE_DATE,
    MAX_TRIPS,
    MonthlyWeekday,
    recurring_dates,
)
from train_commute_companion.schedule import dated_schedule


@dataclass(frozen=True, slots=True)
class TravelPlan:
    profile: Profile
    revision: int
    start: date
    end: date | None
    weekdays: frozenset[int]
    interval: int
    skip: frozenset[date]
    has_return: bool
    inherits: str | None
    replaces: tuple[str, ...] = ()
    monthly: MonthlyWeekday | None = None
    end_after: int | None = None

    @property
    def id(self) -> str:
        return self.profile.id

    def runs(self, day: date) -> bool:
        if day < self.start or (self.end is not None and day > self.end) or day in self.skip:
            return False
        if self.monthly:
            return self.monthly.matches(day)
        if not self.weekdays:
            return day == self.start
        week = ((day - self.start).days + self.start.weekday()) // 7
        return day.isoweekday() in self.weekdays and week % self.interval == 0

    def dates(self, first: date, last: date) -> Iterator[date]:
        return (
            day
            for day in recurring_dates(
                first,
                min(last, self.end or last),
                self.start,
                self.weekdays,
                self.interval,
                self.monthly,
            )
            if day not in self.skip
        )

    def schedules(self, day: date) -> Iterator[DatedSchedule]:
        if self.runs(day):
            yield dated_schedule(self.profile, Direction.OUTBOUND, day, None)
            if self.has_return:
                yield dated_schedule(self.profile, Direction.RETURN, day, None)


@dataclass(frozen=True, slots=True)
class PlanReceipt:
    request_hash: str
    revision: int
    previous: TravelPlan | None
    applied: TravelPlan | None
    plan_id: str
    text: str

    def result(self, request_id: str) -> dict[str, Any]:
        return {
            "status": "saved",
            "text": self.text,
            "plan_id": self.plan_id,
            "undo_id": request_id,
        }


def fields(value: object, allowed: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(k not in allowed.split() for k in value):
        raise ValueError(f"{label} has unknown fields or is not an object.")
    return value


def _text(value: object, label: str, *, max_len: int = 80) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_len
        or not value.isprintable()
    ):
        raise ValueError(f"{label} must be non-empty text up to {max_len} characters.")
    return value.strip()


def _date(value: object, label: str) -> date:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, re.ASCII):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            if has_room(day):
                return day
    raise ValueError(f"{label} must be a date in YYYY-MM-DD form.")


def _time(value: object, label: str) -> time:
    if isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        return time.fromisoformat(value)
    raise ValueError(f"{label} must be a time in HH:MM form.")


def _station(value: object, known: frozenset[str] | None, label: str) -> str:
    text = _text(value, label)
    if text == "8400002":
        raise ValueError(f"{label} must be a railway station.")
    if known is None:
        if re.fullmatch(r"[A-Za-z0-9]{1,12}", text):
            return text.upper()
        raise ValueError("The station catalogue is unavailable. Enter a station code or retry.")
    if text.upper() in known:
        return text.upper()
    choices = known.lookup(text) if isinstance(known, Stations) else ()
    if len(choices) == 1:
        return choices[0]
    if choices:
        raise ValueError(f"{label} matches {', '.join(choices)}. Use the station code.")
    raise ValueError(f"{label} is not in the station catalogue.")


def _journey(raw: object, label: str, template: UsualJourney | None) -> UsualJourney:
    general = "mode day_offset "
    d = fields(raw, general + "departure train_number earliest latest arrive_by", label)
    day_offset = d.get("day_offset", 0)
    if (
        type(day_offset) is not int
        or day_offset not in (0, 1)
        or label == "Outbound"
        and day_offset
    ):
        raise ValueError(f"{label} day_offset must be 0, or 1 for a next-day return.")
    buffer = template.buffer_min if template else 10
    lead = template.active_lead_min if template else None
    mode = d.get("mode")
    if mode == "exact":
        fields(d, general + "departure train_number", label)
        t = _time(d.get("departure"), f"{label} departure")
        n = d.get("train_number")
        if n is not None and (not isinstance(n, str) or not re.fullmatch(r"[0-9]{1,12}", n)):
            raise ValueError(f"{label} train number must contain only digits.")
        first = datetime.combine(date(2000, 1, 2), t)
        watch_start = (first - timedelta(minutes=max(buffer + 15, lead or 0))).time()
        watch_end = (first + timedelta(hours=2)).time()
        return UsualJourney(
            t,
            (),
            (watch_start, watch_end),
            buffer,
            False,
            day_offset,
            active_lead_min=lead,
            exact_train=n or "",
        )
    if mode not in ("window", "on_leave"):
        raise ValueError(f"{label} mode must be exact, window, or on_leave.")
    fields(d, general + "earliest latest arrive_by", label)
    deadline = None if d.get("arrive_by") is None else _time(d["arrive_by"], f"{label} arrival")
    start = d.get("earliest")
    end = d.get("latest")
    if start is None and deadline is not None:
        # shortcut: a deadline alone opens a three-hour departure window, widen it explicitly for longer journeys.
        opening = datetime.combine(date(2000, 1, 2), deadline) - timedelta(hours=3)
        start = opening.strftime("%H:%M") if opening.day == 2 else "00:00"
    if mode == "on_leave" and start is None:
        start = "00:00"
    if end is None and (mode == "on_leave" or deadline is not None):
        end = deadline.strftime("%H:%M") if deadline else "23:59"
    earliest = _time(start, f"{label} earliest departure")
    latest = _time(end, f"{label} latest departure")
    if latest < earliest:
        raise ValueError(
            f"{label} departure window must fit in one day. Use a next-day return when needed."
        )
    if deadline is not None and deadline <= earliest:
        raise ValueError(f"{label} arrival deadline must be after the earliest departure.")
    return UsualJourney(
        earliest,
        (),
        (earliest, latest),
        buffer,
        False,
        day_offset,
        active_lead_min=lead,
        mode="window",
        pick="on_leave" if mode == "on_leave" else "earliest",
        first_train_set=False,
        arrive_by=deadline,
    )


def parse_plan(
    raw: object,
    pid: str,
    revision: int,
    profiles: tuple[Profile, ...],
    known: frozenset[str] | None,
    now: datetime,
) -> TravelPlan:
    d = fields(
        raw,
        "name origin destination start_date end_date end_after weekdays interval_weeks monthly "
        "skip_dates outbound return profile_id",
        "Travel plan",
    )
    start = _date(d.get("start_date"), "Start")
    weekdays = d.get("weekdays", [])
    if (
        not isinstance(weekdays, list)
        or len(weekdays) > 7
        or any(type(i) is not int or not 1 <= i <= 7 for i in weekdays)
    ):
        raise ValueError("Weekdays must be a list of ISO weekday numbers (Monday is 1).")
    monthly = None
    if d.get("monthly") is not None:
        rule = fields(d["monthly"], "ordinal weekday", "Monthly recurrence")
        ordinal, weekday = rule.get("ordinal"), rule.get("weekday")
        if type(ordinal) is not int or not 1 <= ordinal <= 5:
            raise ValueError("A monthly ordinal must be a whole number from 1 to 5.")
        if type(weekday) is not int or not 1 <= weekday <= 7:
            raise ValueError("A monthly weekday must be an ISO weekday number (Monday is 1).")
        if weekdays:
            raise ValueError("Choose either a monthly rule or weekly weekdays.")
        monthly = MonthlyWeekday(ordinal, weekday)
    recurring = bool(weekdays) or monthly is not None
    raw_end = d.get("end_date")
    end = (None if recurring else start) if raw_end is None else _date(raw_end, "End")
    end_after = d.get("end_after")
    if end_after is not None:
        if type(end_after) is not int or not 1 <= end_after <= MAX_TRIPS:
            raise ValueError(f"The trip count must be a whole number from 1 to {MAX_TRIPS}.")
        if raw_end is not None or not recurring:
            raise ValueError("End after a trip count requires a repeat rule and no end date.")
    interval = d.get("interval_weeks", 1)
    if type(interval) is not int or not 1 <= interval <= 52 or (not weekdays and interval != 1):
        raise ValueError("An interval must be 1 to 52 weeks and needs weekdays when it exceeds 1.")
    if end is not None and (end < start or not recurring and end != start):
        raise ValueError("End must be on or after the start. A date range needs a repeat rule.")
    skip = d.get("skip_dates", [])
    if not isinstance(skip, list) or len(skip) > 366:
        raise ValueError("Skip dates must be a list of up to 366 dates.")
    skipped = frozenset(_date(item, "Skip date") for item in skip)
    if any(day < start or end is not None and day > end for day in skipped):
        raise ValueError("Skip dates must be inside the plan dates.")
    if end_after is not None:
        dates = recurring_dates(
            start, MAX_RECURRENCE_DATE, start, frozenset(weekdays), interval, monthly
        )
        matching_dates = (day for day in dates if day not in skipped)
        end = next(islice(matching_dates, end_after - 1, None), None)
        if end is None:
            raise ValueError("The trip count extends beyond the supported calendar.")
    origin = _station(d.get("origin"), known, "Origin")
    destination = _station(d.get("destination"), known, "Destination")
    if origin == destination:
        raise ValueError("Choose different origin and destination stations.")
    name = _text(d.get("name", f"{origin} to {destination}"), "Name")
    base_id = d.get("profile_id")
    if base_id is not None:
        base_id = _text(base_id, "Profile ID", max_len=128)
    matching = tuple(
        p for p in profiles if p.home_station == origin and p.work_station == destination
    ) or tuple(p for p in profiles if p.home_station == origin)

    # Readout and scheduled-profile options do not change flexible-trip routing.
    def defaults(p: Profile) -> tuple[object, ...]:
        return (
            p.traveller,
            replace(
                p.alerts,
                calendar_feed=True,
                commute_stats=False,
                weekly_recap=False,
                after_trip_note=False,
                alert_history=False,
                quick_trip=True,
                daily_brief=True,
                location_leave_now=True,
                arrival_prompt=True,
                navigate=True,
                compensation_details=True,
                timetable_update=True,
                wake_at=None,
                partner_eta=False,
                partner_phone=None,
                language="en",
            ),
            p.accessibility,
            p.transfer_min,
            p.location,
            p.outbound.buffer_min,
            p.return_.buffer_min,
            p.outbound.active_lead_min,
            p.return_.active_lead_min,
        )

    match = matching[0] if matching else None
    if match and any(defaults(p) != defaults(match) for p in matching[1:]):
        match = None
    fallback = profiles[0] if len(profiles) == 1 else None
    base = next((p for p in profiles if p.id == base_id), None) if base_id else match or fallback
    if base_id is not None and base is None:
        raise ValueError("The profile whose preferences should apply was not found.")
    alerts = base.alerts if base else AlertSettings()
    alerts = replace(alerts, partner_eta=False, partner_phone=None)
    if base_id is None and match is not None:
        alerts = replace(
            alerts,
            commute_stats=all(p.alerts.commute_stats for p in matching),
            weekly_recap=all(p.alerts.weekly_recap for p in matching),
            after_trip_note=all(p.alerts.after_trip_note for p in matching),
            alert_history=all(p.alerts.alert_history for p in matching),
            location_leave_now=all(p.alerts.location_leave_now for p in matching),
            arrival_prompt=all(p.alerts.arrival_prompt for p in matching),
            navigate=all(p.alerts.navigate for p in matching),
            compensation_details=all(p.alerts.compensation_details for p in matching),
            language=alerts.language
            if all(p.alerts.language == alerts.language for p in matching)
            else "en",
        )
    outbound = _journey(d.get("outbound"), "Outbound", base.outbound if base else None)
    has_return = d.get("return") is not None
    back = (
        _journey(d.get("return"), "Return", base.return_ if base else None)
        if has_return
        else outbound
    )
    latest_return = back.first_train if back.exact_train is not None else back.window[1]
    if has_return and back.day_offset == 0 and latest_return <= outbound.first_train:
        raise ValueError("The same-day return must leave after the outbound journey departs.")
    profile = Profile(
        pid,
        name,
        base.traveller if base else "me",
        frozenset(),
        frozenset(),
        False,
        origin,
        destination,
        outbound,
        back,
        base.transfer_min if base else {},
        alerts,
        base.location
        if base and base.home_station == origin and base.work_station == destination
        else None,
        accessibility=base.accessibility
        if base
        else AccessibilitySettings(any(p.accessibility.require_accessible for p in profiles)),
        travel_plan=True,
    )
    plan = TravelPlan(
        profile,
        revision,
        start,
        end,
        frozenset(weekdays),
        interval,
        skipped,
        has_return,
        base.id if base else None,
        monthly=monthly,
        end_after=end_after,
    )
    floor = max(start, now.astimezone(TZ).date() - timedelta(days=1))
    if end is not None and end < floor:
        raise ValueError("The plan has no remaining travel.")
    if not any(
        to_utc(sch.window_end) > to_utc(now)
        for day in islice(plan.dates(floor, MAX_RECURRENCE_DATE), 2)
        for sch in plan.schedules(day)
    ):
        raise ValueError("The plan has no remaining travel.")
    return plan


def _journey_dict(j: UsualJourney) -> dict[str, Any]:
    if j.exact_train is not None:
        return {
            "mode": "exact",
            "departure": j.first_train.strftime("%H:%M"),
            "train_number": j.exact_train or None,
            "day_offset": j.day_offset,
        }
    return {
        "mode": "on_leave" if j.pick == "on_leave" else "window",
        "earliest": j.window[0].strftime("%H:%M"),
        "latest": j.window[1].strftime("%H:%M"),
        "arrive_by": j.arrive_by.strftime("%H:%M") if j.arrive_by else None,
        "day_offset": j.day_offset,
    }


def plan_dict(plan: TravelPlan) -> dict[str, Any]:
    p = plan.profile
    return {
        "plan_id": p.id,
        "name": p.name,
        "origin": p.home_station,
        "destination": p.work_station,
        "start_date": plan.start.isoformat(),
        "end_date": plan.end.isoformat() if plan.end and plan.end_after is None else None,
        **({"end_after": plan.end_after} if plan.end_after is not None else {}),
        **(
            {"monthly": {"ordinal": plan.monthly.ordinal, "weekday": plan.monthly.weekday}}
            if plan.monthly
            else {}
        ),
        "weekdays": sorted(plan.weekdays),
        "interval_weeks": plan.interval,
        "skip_dates": [day.isoformat() for day in sorted(plan.skip)],
        "outbound": _journey_dict(p.outbound),
        "return": _journey_dict(p.return_) if plan.has_return else None,
        "profile_id": plan.inherits,
        "replaces": list(plan.replaces),
    }


def plan_text(plan: TravelPlan) -> str:
    p = plan.profile
    timing = plan.start.isoformat()
    if plan.weekdays or plan.monthly:
        timing += f" to {plan.end.isoformat()}" if plan.end else " onward"
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        if plan.monthly:
            nth = ("first", "second", "third", "fourth", "fifth")[plan.monthly.ordinal - 1]
            timing += f", on the {nth} {days[plan.monthly.weekday - 1]} of each month"
        else:
            frequency = "weekly" if plan.interval == 1 else f"every {plan.interval} weeks"
            timing += f", {frequency} on " + ", ".join(days[i - 1] for i in sorted(plan.weekdays))
        if plan.end_after is not None:
            timing += f", ends after {plan.end_after} trips"
    if plan.skip:
        timing += ". Skips " + ", ".join(d.isoformat() for d in sorted(plan.skip))
    legs: list[str] = []
    for d in Direction:
        if d is Direction.RETURN and not plan.has_return:
            break
        j = p.journey(d)
        if j.exact_train is not None:
            when = f"{j.first_train:%H:%M}" + (f" ({j.exact_train})" if j.exact_train else "")
        else:
            when = f"{j.window[0]:%H:%M} to {j.window[1]:%H:%M}"
            if j.pick == "on_leave":
                when += ", pick when you leave"
            if j.arrive_by:
                when += f", arrive by {j.arrive_by:%H:%M}"
        legs.append(f"{d.value.title()} {when}" + (" (next day)" if j.day_offset else ""))
    return f"{p.name}: {p.home_station} to {p.work_station}, {timing}. " + ". ".join(legs) + "."


def overlaps(a: DatedSchedule, b: DatedSchedule) -> bool:
    return max(to_utc(a.window_start), to_utc(b.window_start)) < min(
        to_utc(a.window_end), to_utc(b.window_end)
    )


def regular_date(p: Profile, day: date) -> bool:
    return p.home_station != p.work_station and p.commutes_on(day)


def scheduled_days(sch: DatedSchedule, p: Profile) -> Iterator[date]:
    base = sch.key.service_date - timedelta(days=p.journey(sch.key.direction).day_offset)
    for offset in range(-2, 3):
        yield base + timedelta(days=offset)


def plan_suppresses(sch: DatedSchedule, p: Profile, plans: Iterable[TravelPlan]) -> bool:
    """Replacement follows the approved recurrence on its actual dates, including an overnight return."""
    commute_day = sch.key.service_date - timedelta(days=p.journey(sch.key.direction).day_offset)
    return any(
        plan.runs(commute_day)
        or any(
            overlaps(sch, other) for day in scheduled_days(sch, p) for other in plan.schedules(day)
        )
        for plan in plans
        if p.id in plan.replaces
    )
