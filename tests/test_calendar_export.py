from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from lifecycle_fixtures import L1, L2, ans, at, conn, ctx, usual_locked

from train_commute_companion.calendar_export import CalendarExportError, export_ics, export_id
from train_commute_companion.model import LegKind
from train_commute_companion.view import occurrence_view


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
