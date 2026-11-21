from dataclasses import replace
from datetime import UTC, datetime

import pytest
from builders import make_leg
from lifecycle_fixtures import L1, L2, at, ctx, journey, occ
from test_news import good_poll

from train_commute_companion.advice import leave_now_text
from train_commute_companion.calendar import TZ, hhmm
from train_commute_companion.messages import all_good_text, leave_by
from train_commute_companion.model import Provenance
from train_commute_companion.news import all_good
from train_commute_companion.state import LockSource
from train_commute_companion.view import occurrence_view


@pytest.mark.parametrize(
    ("expected", "buffer", "wanted"),
    [
        ("08:09", 10, "07:59"),
        ("08:12", 10, "08:02"),
        ("08:22", 10, "08:02"),
        (None, 10, "08:02"),
        (None, 0, "08:12"),
        (None, 20, "07:52"),
    ],
)
def test_leave_by_uses_earlier_departure_and_journey_buffer(expected, buffer, wanted):
    assert hhmm(leave_by(at("08:12"), at(expected) if expected else None, buffer)) == wanted


@pytest.mark.parametrize(
    ("planned", "expected", "buffer", "wanted"),
    [
        ("2026-11-22T00:05+01:00", None, 10, "2026-11-21T22:55+00:00"),
        ("2026-03-29T03:05+02:00", None, 10, "2026-03-29T00:55+00:00"),
        ("2026-10-25T02:05+01:00", None, 10, "2026-10-25T00:55+00:00"),
        ("2026-10-25T02:10+01:00", "2026-10-25T02:50+02:00", 10, "2026-10-25T00:40+00:00"),
    ],
)
def test_leave_by_compares_and_subtracts_real_elapsed_time(planned, expected, buffer, wanted):
    plan = datetime.fromisoformat(planned).astimezone(TZ)
    exp = datetime.fromisoformat(expected).astimezone(TZ) if expected else None
    assert leave_by(plan, exp, buffer).astimezone(UTC) == datetime.fromisoformat(wanted)


def test_all_good_keeps_route_platform_crowd_and_source():
    first = replace(L1, stops=(replace(L1.board, actual_track="5"), *L1.stops[1:]))
    text = all_good_text([first, replace(L2, crowd_forecast="high")], 10)
    assert "on time per NS, change at UT, platform 5" in text
    assert "crowd forecasts per NS: AAA-UT unknown, UT-RTD high" in text
    assert text.endswith(". Leave by 08:02.")


def test_earlier_train_advice_keeps_crowd_and_uses_conservative_leave_by():
    from builders import make_connection

    leg = replace(make_leg("AAA", "RTD", "08:12", "09:00", delay_dep=5), crowd_forecast="high")
    connection = make_connection([leg])
    text = leave_now_text(connection, TZ, 10)
    assert "leaves at 08:17" in text and "Leave by 08:02" in text
    assert text.endswith(", crowd forecast: high per NS")
    assert "Leave by" not in leave_now_text(connection, TZ, None)


def test_check_in_and_view_use_selected_train_instead_of_usual_time():
    first = make_leg("AAA", "UT", "08:44", "09:12", number="4321")
    onward = make_leg("UT", "RTD", "09:22", "09:56", number="9999")
    state = occ(journey([first, onward], source=LockSource.CHOOSE))
    context = ctx(at("08:00"))
    polled = good_poll(state, first, onward)
    event = all_good(state, polled, context)
    assert event is not None and "Leave by 08:34" in event.text
    view = occurrence_view(state, context, polled.answers)
    assert hhmm(view.leave_by) == "08:34"
    disabled = replace(
        context,
        profile=replace(context.profile, alerts=replace(context.profile.alerts, leave_by=False)),
    )
    event = all_good(state, polled, disabled)
    assert event is not None and "Leave by" not in event.text
    assert occurrence_view(state, disabled, polled.answers).leave_by is None


@pytest.mark.parametrize("provenance", [Provenance.MISSING, Provenance.PLANNED])
def test_view_falls_back_to_planned_departure_when_ns_time_is_unknown(provenance):
    first = make_leg("AAA", "UT", "08:12", "08:40", provenance=provenance)
    state = occ(journey([first, L2]))
    polled = good_poll(state, first, L2)
    assert hhmm(occurrence_view(state, ctx(at("08:00")), polled.answers).leave_by) == "08:02"


def test_view_without_followed_train_has_no_leave_by():
    assert occurrence_view(occ(None), ctx(at("08:00")), ()).leave_by is None
