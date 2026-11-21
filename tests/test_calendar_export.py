from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import runtime_seam as seam
from lifecycle_fixtures import L1, L2, ans, at, conn, ctx, usual_locked
from runtime_rig import DAY, PROFILE, Rig

from train_commute_companion.calendar_export import (
    CalendarExportError,
    calendar_feed,
    export_ics,
    export_id,
)
from train_commute_companion.lookahead_probe import day_key, warn_mark
from train_commute_companion.model import Direction, LegKind, OccurrenceKey
from train_commute_companion.state import LookAheadLedger
from train_commute_companion.travel_plans import TravelPlan, parse_plan
from train_commute_companion.view import occurrence_view


async def test_subscription_is_read_only_and_reflects_local_service_dates(tmp_path):
    rig = Rig(tmp_path, now="07:50")
    rig.write(
        text=PROFILE.replace(
            '    name: "office_b"', '    name: "office_b"\n    alerts: {calendar_feed: false}'
        ).replace("    days: [mon, tue, wed, thu, fri]", "    days: [tue]")
    )
    await rig.rt.start()
    assert await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND) is None
    next_tuesday = DAY + timedelta(days=7)
    seam.seed_state(
        rig.rt,
        lookahead={
            "p1|outbound": LookAheadLedger(
                {day_key(next_tuesday, None): (warn_mark("works"),)}, (), None, 1, None
            )
        },
    )

    before = len(rig.provider.calls)
    ics = calendar_feed(rig.rt.state, at("07:50")).decode().replace("\r\n ", "")
    assert len(rig.provider.calls) == before
    assert ics.count("BEGIN:VEVENT") == 3
    assert "DTSTART:20261027T071200Z" not in ics
    assert "DTSTART:20261027T165200Z" in ics
    assert "DTSTART:20261103T071200Z" in ics
    assert "DTSTART:20261110" not in ics
    uid = export_id(OccurrenceKey("p1", Direction.OUTBOUND, next_tuesday))
    assert f"UID:{uid}@train-commute-companion.invalid" in ics
    assert ics.count("NS timetable warning") == 1
    assert "office_b" not in ics


async def test_subscription_respects_pauses_and_next_day_returns(tmp_path):
    rig = Rig(tmp_path, now="07:50")
    rig.write(
        text=PROFILE.split('  - id: "p2"')[0]
        .replace("days: [mon, tue, wed, thu, fri]", "days: [mon]")
        .replace(
            'name: "NAME"', 'name: "NAME"\n    pauses: [{start: "2026-11-02", end: "2026-11-03"}]'
        )
        .replace('first_train: "17:52"', 'first_train: "08:00", day_offset: 1')
    )
    await rig.rt.start()
    ics = unfolded(calendar_feed(rig.rt.state, at("07:50")))
    uid = export_id(OccurrenceKey("p1", Direction.RETURN, DAY))
    assert ics.count("BEGIN:VEVENT") == 2
    assert f"UID:{uid}@train-commute-companion.invalid" in ics
    assert "DTSTART:20261027T070000Z" in ics
    assert "DTSTART:20261109T071200Z" in ics
    assert all(f"DTSTART:202611{day}" not in ics for day in ("02", "03", "10"))


async def test_subscription_includes_replacement_plan_and_omits_replaced_commute(tmp_path):
    rig = Rig(tmp_path, now="07:50")
    rig.write(text=PROFILE.split('  - id: "p2"')[0])
    await rig.rt.start()
    (profile,) = rig.rt.config.profiles
    borrowed = replace(profile, id="travel-1", name="Appointment", work_station="BBB")
    prior = TravelPlan(
        replace(borrowed, id="travel-old", name="Old Appointment"),
        1,
        DAY,
        DAY,
        frozenset(),
        1,
        frozenset(),
        False,
        None,
    )
    plan = TravelPlan(
        borrowed, 2, DAY, DAY, frozenset(), 1, frozenset(), False, "p1", ("p1", prior.id)
    )
    seam.seed_state(rig.rt, travel_plans={prior.id: prior, plan.id: plan})

    ics = unfolded(calendar_feed(rig.rt.state, at("07:50")))
    usual_uid = export_id(OccurrenceKey("p1", Direction.OUTBOUND, DAY))
    replacement_uid = export_id(OccurrenceKey(plan.id, Direction.OUTBOUND, DAY))
    assert f"UID:{replacement_uid}@train-commute-companion.invalid" in ics
    assert f"UID:{usual_uid}@train-commute-companion.invalid" not in ics
    assert "SUMMARY:Appointment: AAA to BBB" in ics
    assert "Old Appointment" not in ics
    assert "DTSTART:20261027T165200Z" not in ics

    cfg = rig.rt.config
    off = replace(profile, alerts=replace(profile.alerts, calendar_feed=False))
    seam.seed_state(rig.rt, config=replace(cfg, profiles=(off,)))
    ics = unfolded(calendar_feed(rig.rt.state, at("07:50")))
    assert "BEGIN:VEVENT" not in ics


@pytest.mark.parametrize(
    "field",
    [
        "calendar_feed",
        "commute_stats",
        "weekly_recap",
        "after_trip_note",
        "alert_history",
        "quick_trip",
        "daily_brief",
        "location_leave_now",
    ],
)
async def test_non_routing_settings_do_not_change_flexible_trip_defaults(tmp_path, field):
    rig = Rig(tmp_path)
    text = PROFILE.split('  - id: "p2"')[0].replace("buffer_min: 10", "buffer_min: 22")
    rig.write(text=text + "    alerts: {disabled_categories: [routine], calendar_feed: false}\n")
    await rig.rt.start()
    (profile,) = rig.rt.config.profiles
    profile = replace(profile, alerts=replace(profile.alerts, **{field: True}))
    other = replace(profile, id="p2", alerts=replace(profile.alerts, **{field: False}))
    plan = parse_plan(
        {
            "origin": "AAA",
            "destination": "RTD",
            "start_date": DAY.isoformat(),
            "outbound": {"mode": "exact", "departure": "08:30"},
        },
        "travel-calendar",
        1,
        (profile, other),
        None,
        at("07:50"),
    )
    assert plan.profile.outbound.buffer_min == 22
    assert plan.profile.alerts.disabled_categories == frozenset({"routine"})
    if field in {
        "commute_stats",
        "weekly_recap",
        "after_trip_note",
        "alert_history",
        "location_leave_now",
    }:
        assert not getattr(plan.profile.alerts, field)
    if field != "calendar_feed":
        assert not plan.profile.alerts.calendar_feed


async def test_subscription_is_valid_when_no_profiles_are_included(tmp_path):
    rig = Rig(tmp_path, now="07:50")
    rig.write(text="profiles: []")
    await rig.rt.start()
    ics = unfolded(calendar_feed(rig.rt.state, at("07:50")))
    assert "BEGIN:VEVENT" not in ics
    assert "BEGIN:X-TCC-EMPTY\r\nCOMMENT:No planned journeys\r\nEND:X-TCC-EMPTY\r\n" in ics


def journey():
    return occurrence_view(usual_locked(), ctx(at("07:50")), (ans("AAA", conn(L1, L2)),))


def unfolded(data):
    return data.decode().replace("\r\n ", "")


def test_exports_one_tentative_planned_journey_without_recurrence_or_actual_times():
    occurrence = journey()
    text = unfolded(export_ics(occurrence, exported_at=at("07:50")))
    assert text.count("BEGIN:VEVENT\r\n") == text.count("END:VEVENT\r\n") == 1
    assert "STATUS:TENTATIVE\r\n" in text
    assert "RRULE" not in text and "RDATE" not in text
    assert "DTSTART:20261027T071200Z\r\n" in text
    final = (
        occurrence.legs[-1].planned_arrival.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    )
    assert "DTEND:" + final + "\r\n" in text
    assert "not live" in text and "does not prove attendance" in text
    assert "NS source check:" in text and "URL:https://www.ns.nl/reisplanner/\r\n" in text
    changed = replace(
        occurrence,
        legs=tuple(
            replace(leg, departure=at("10:00"), arrival=at("11:00")) for leg in occurrence.legs
        ),
    )
    assert export_ics(changed, exported_at=at("07:50")) == export_ics(
        occurrence, exported_at=at("07:50")
    )


@pytest.mark.parametrize("status", ["active", "completed", "stopped"])
def test_recovery_selection_cannot_export_the_original_safety_watch_as_the_followed_plan(status):
    from recovery_sample import selection

    occurrence = replace(journey(), selected_recovery=replace(selection(), status=status))
    with pytest.raises(CalendarExportError, match="recovery selection"):
        export_ics(occurrence, exported_at=at("07:50"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: replace(o, not_travelling=True),
        lambda o: replace(o, legs=()),
        lambda o: replace(o, legs=(replace(o.legs[0], state=LegKind.CANCELLED),)),
        lambda o: replace(o, legs=(replace(o.legs[0], planned_arrival=None),)),
        lambda o: replace(o, legs=(replace(o.legs[0], planned_departure=None),)),
        lambda o: replace(
            o, legs=(replace(o.legs[0], planned_departure=at("08:12").replace(tzinfo=None)),)
        ),
        lambda o: replace(o, legs=(replace(o.legs[0], planned_arrival=at("08:00")),)),
        lambda o: replace(o, legs=(o.legs[1], o.legs[0])),
    ],
)
def test_invalid_skipped_cancelled_and_incomplete_plans_are_refused(mutate):
    with pytest.raises(CalendarExportError):
        export_ics(mutate(journey()), exported_at=at("07:50"))


def test_crlf_text_escaping_and_utf8_byte_folding_prevent_property_injection():
    profile = "é" * 90 + "\\;comma,\r\nBEGIN:VEVENT\r\nATTENDEE:evil@example.test\x00"
    data = export_ics(replace(journey(), profile_name=profile), exported_at=at("07:50"))
    assert data.endswith(b"\r\n")
    assert all(len(line) <= 75 for line in data.split(b"\r\n"))
    for line in data.split(b"\r\n"):
        line.decode("utf-8")
    text = unfolded(data)
    assert text.count("BEGIN:VEVENT\r\n") == 1
    assert "\r\nATTENDEE:" not in text and "\x00" not in text
    assert "\\\\" in text and "\\;" in text and "\\," in text and "\\nBEGIN:VEVENT" in text


def test_uid_is_stable_opaque_and_scoped_to_one_occurrence():
    occurrence = journey()
    identifier = export_id(occurrence.key)
    assert len(identifier) == 64 and occurrence.key.profile_id not in identifier
    first = unfolded(export_ics(occurrence, exported_at=at("07:50")))
    second = unfolded(
        export_ics(replace(occurrence, profile_name="Renamed"), exported_at=at("07:51"))
    )
    assert next(line for line in first.splitlines() if line.startswith("UID:")) == next(
        line for line in second.splitlines() if line.startswith("UID:")
    )
    assert export_id(replace(occurrence.key, profile_id="other")) != identifier


def test_utc_dates_keep_both_sides_of_the_dst_fold():
    occurrence = journey()
    zone = ZoneInfo("Europe/Amsterdam")
    departure = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    arrival = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    leg = replace(occurrence.legs[0], planned_departure=departure, planned_arrival=arrival)
    data = unfolded(export_ics(replace(occurrence, legs=(leg,)), exported_at=departure))
    assert "DTSTART:20261025T003000Z" in data
    assert "DTEND:20261025T011500Z" in data


def test_retained_journeys_and_alternatives_do_not_change_the_single_selected_event():
    occurrence = journey()
    with_others = replace(occurrence, retained=(occurrence.legs,), next_good=conn(L1, L2))
    assert export_ics(with_others, exported_at=at("07:50")) == export_ics(
        occurrence, exported_at=at("07:50")
    )
