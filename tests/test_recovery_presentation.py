from dataclasses import replace
from datetime import timedelta

from lifecycle_fixtures import ctx
from recovery_sample import NOW
from recovery_sample import answer as sample_answer

from train_commute_companion.model import AccessibilitySettings
from train_commute_companion.view import _replacement_plans


def sample():
    context = ctx(NOW)
    context = replace(
        context, now=NOW, schedule=replace(context.schedule, window_end=NOW + timedelta(hours=4))
    )
    answer = sample_answer()
    return answer, context


def test_fresh_validated_plans_preserve_walks_and_do_not_claim_following():
    answer, context = sample()
    plans = _replacement_plans((answer,), context)
    assert len(plans) == 1 and plans[0].checked_at == NOW
    assert [s.mode for s in plans[0].itinerary.steps] == [
        "TRAIN",
        "WALK",
        "BUS",
        "WALK",
        "METRO",
        "METRO",
    ]
    assert not plans[0].itinerary.reported_times
    assert plans[0].source_query == answer.query
    assert plans[0].occurrence == context.schedule.key
    assert plans[0].fields()["proposal_id"] == plans[0].proposal_id


def test_expired_or_future_source_data_never_becomes_replacement_guidance():
    answer, context = sample()
    for fetched in (NOW - timedelta(minutes=5), NOW + timedelta(seconds=1)):
        assert _replacement_plans((replace(answer, fetched_at=fetched),), context) == ()


def test_departed_disconnected_and_wrong_destination_plans_are_hidden():
    answer, context = sample()
    departed = replace(
        context, now=answer.connections[0].legs[0].board.planned_departure + timedelta(seconds=1)
    )
    assert _replacement_plans((replace(answer, fetched_at=departed.now),), departed) == ()
    wrong = replace(context, profile=replace(context.profile, work_station="AMF"))
    assert _replacement_plans((answer,), wrong) == ()
    connection = answer.connections[0]
    without_walk = replace(connection, legs=(connection.legs[0], *connection.legs[2:]))
    assert _replacement_plans((replace(answer, connections=(without_walk,)),), context) == ()


def test_duplicate_plans_keep_the_latest_source_time():
    answer, context = sample()
    old = replace(answer, fetched_at=NOW - timedelta(seconds=30))
    plans = _replacement_plans((old, answer), context)
    assert len(plans) == 1 and plans[0].checked_at == NOW


def test_proposal_identity_keeps_refreshes_separate_from_approved_offer_times():
    answer, context = sample()
    plan = _replacement_plans((answer,), context)[0]
    refreshed = replace(
        plan,
        checked_at=NOW + timedelta(seconds=30),
        source_query=replace(answer.query, at=NOW + timedelta(seconds=30)),
    )
    assert refreshed.proposal_id == plan.proposal_id
    steps = list(plan.itinerary.steps)
    steps[0] = replace(steps[0], departure=steps[0].departure + timedelta(minutes=2))
    walk = steps[1]
    steps[1] = replace(walk, planned_departure=walk.planned_departure + timedelta(minutes=1))
    assert (
        replace(plan, itinerary=replace(plan.itinerary, steps=tuple(steps))).proposal_id
        == plan.proposal_id
    )
    assert replace(plan, generation=plan.generation + 1).proposal_id != plan.proposal_id
    assert replace(plan, route_revision=plan.route_revision + 1).proposal_id != plan.proposal_id


def test_accessibility_query_provenance_must_match_the_profile():
    answer, context = sample()
    context = replace(
        context, profile=replace(context.profile, accessibility=AccessibilitySettings(True))
    )
    assert _replacement_plans((answer,), context) == ()
    filtered = replace(answer, query=replace(answer.query, require_accessible=True))
    assert len(_replacement_plans((filtered,), context)) == 1


def test_newer_cancellation_or_missing_plan_never_revives_an_older_running_copy():
    answer, context = sample()
    old = replace(answer, fetched_at=NOW - timedelta(seconds=30))
    connection = answer.connections[0]
    cancelled = replace(
        connection, legs=(replace(connection.legs[0], cancelled=True), *connection.legs[1:])
    )
    assert _replacement_plans((old, replace(answer, connections=(cancelled,))), context) == ()
    assert _replacement_plans((old, replace(answer, connections=())), context) == ()
    assert _replacement_plans((answer, replace(answer, connections=(cancelled,))), context) == ()
    assert (
        _replacement_plans((replace(answer, connections=(connection, cancelled)),), context) == ()
    )
