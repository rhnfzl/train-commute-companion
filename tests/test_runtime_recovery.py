from recovery_sample import NOW, answer
from runtime_rig import Rig

from train_commute_companion.messages import Refused
from train_commute_companion.recovery_state import RecoveryOffer, SelectedRecovery

PROFILE = """profiles:
  - id: p1
    name: office
    days: [sat]
    home_station: UT
    work_station: RTD
    outbound: {first_train: '11:58', transfers: [], buffer_min: 5, window: ['10:30', '16:00']}
    return: {first_train: '19:00', buffer_min: 5}
"""


async def world(tmp_path):
    rig = Rig(tmp_path)
    rig.clock.t = NOW
    rig.write(text=PROFILE)
    rig.provider.world = list(answer().connections)
    await rig.rt.start()
    return rig


def occurrence(rig):
    return next(o for o in rig.rt.view().occurrences if o.key.direction.value == "outbound")


async def prepare(rig):
    shown = occurrence(rig)
    plan = shown.replacement_plans[0]
    origin = next(s.origin for s in plan.itinerary.steps if s.mode == "BUS")
    offered = await rig.rt.prepare_recovery(shown.key, plan.proposal_id, origin.physical_id)
    assert isinstance(offered, RecoveryOffer), offered
    return offered


async def follow(rig):
    offered = await prepare(rig)
    result = await rig.rt.select_recovery(
        offered.key,
        offered.id,
        acknowledge_planned_times=True,
        acknowledge_accessibility_unknown=True,
    )
    assert isinstance(result, SelectedRecovery), result
    return result


async def test_actual_bus_walk_metro_selection_and_manual_progress(tmp_path):
    rig = await world(tmp_path)
    before = next(iter(rig.rt.state.occurrences.values()))
    selected = await follow(rig)
    assert selected.itinerary.steps[0].mode == "BUS"
    assert occurrence(rig).selected_recovery == selected
    while selected.status == "active":
        step = selected.itinerary.steps[selected.index]
        kind = selected.next_action
        place = step.origin if kind == "boarded" else step.destination
        rig.clock.t = max(rig.clock.t, step.departure if kind == "boarded" else step.arrival)
        selected = await rig.rt.confirm_recovery_step(
            selected.key,
            selected.id,
            selected.revision,
            selected.index,
            kind,
            place.physical_id,
            f"{kind}-{selected.index}",
        )
        assert isinstance(selected, SelectedRecovery), selected
    after = rig.rt.state.occurrences[next(iter(rig.rt.state.occurrences))]
    assert selected.status == "completed"
    assert after.lock == before.lock and after.retained == before.retained


async def test_failed_selection_never_activates_on_later_watchdog_or_restart(tmp_path):
    rig = await world(tmp_path)
    offered = await prepare(rig)
    rig.store.fail = True
    result = await rig.rt.select_recovery(
        offered.key,
        offered.id,
        acknowledge_planned_times=True,
        acknowledge_accessibility_unknown=True,
    )
    assert isinstance(result, Refused)
    assert occurrence(rig).selected_recovery is None
    rig.store.fail = False
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    await rig.restart()
    assert occurrence(rig).selected_recovery is None


async def test_restart_no_clock_progress_and_failed_manual_confirmation(tmp_path):
    rig = await world(tmp_path)
    selected = await follow(rig)
    await rig.restart()
    restored = occurrence(rig).selected_recovery
    assert restored == selected
    rig.clock.jump(180)
    await rig.rt.watchdog_once()
    assert occurrence(rig).selected_recovery.index == 0
    rig.store.fail = True
    step = selected.itinerary.steps[0]
    result = await rig.rt.confirm_recovery_step(
        selected.key, selected.id, 0, 0, "boarded", step.origin.physical_id, "attempt"
    )
    assert isinstance(result, Refused)
    rig.store.fail = False
    await rig.rt.poll_once()
    await rig.rt.recovery_idle()
    assert occurrence(rig).selected_recovery.confirmations == ()


async def test_failed_manual_action_can_be_retried_without_replaying_after_storage_recovers(
    tmp_path,
):
    rig = await world(tmp_path)
    selected = await follow(rig)
    origin = selected.itinerary.steps[0].origin.physical_id
    rig.store.fail = True
    result = await rig.rt.confirm_recovery_step(
        selected.key, selected.id, 0, 0, "boarded", origin, "retry"
    )
    assert isinstance(result, Refused)
    rig.store.fail = False
    result = await rig.rt.confirm_recovery_step(
        selected.key, selected.id, 0, 0, "boarded", origin, "retry"
    )
    assert isinstance(result, SelectedRecovery)
    assert len(result.confirmations) == 1


async def test_stale_offer_wrong_station_alias_and_skip_during_refresh_are_refused(tmp_path):
    rig = await world(tmp_path)
    shown = occurrence(rig)
    plan = shown.replacement_plans[0]
    assert isinstance(await rig.rt.prepare_recovery(shown.key, plan.proposal_id, "GD"), Refused)
    offered = await prepare(rig)
    rig.clock.jump(5)
    assert isinstance(
        await rig.rt.select_recovery(
            offered.key,
            offered.id,
            acknowledge_planned_times=True,
            acknowledge_accessibility_unknown=True,
        ),
        Refused,
    )
    await rig.rt.poll_once()

    async def skip(_query):
        rig.provider.hook = None
        await rig.rt.not_travelling("p1", offered.key.service_date, offered.key.direction)

    rig.provider.hook = skip
    shown = occurrence(rig)
    plan = shown.replacement_plans[0]
    place = next(s.origin for s in plan.itinerary.steps if s.mode == "BUS")
    result = await rig.rt.prepare_recovery(shown.key, plan.proposal_id, place.physical_id)
    assert isinstance(result, Refused)
    assert occurrence(rig).selected_recovery is None and occurrence(rig).recovery_offer is None


async def test_provider_outage_keeps_manual_observations_and_retries_persisted_warning(tmp_path):
    from train_commute_companion.model import EventKind
    from train_commute_companion.providers.base import ServerError

    rig = await world(tmp_path)
    selected = await follow(rig)
    rig.provider.error = lambda query: ServerError()
    rig.notifier.fails = lambda event: event.kind is EventKind.RECOVERY_MONITORING
    rig.clock.jump(1.1)
    await rig.rt.poll_once()
    await rig.rt.recovery_idle()
    displayed = occurrence(rig).selected_recovery
    assert displayed.monitoring_issue == "unavailable" and displayed.index == 0
    assert not displayed.fields(rig.clock.now())["source_fresh"]
    assert any(o.event.kind is EventKind.RECOVERY_MONITORING for o in rig.rt.state.obligations)
    rig.notifier.fails = lambda event: False
    await rig.restart()
    await rig.rt.recovery_idle()
    assert any(e.kind is EventKind.RECOVERY_MONITORING for e in rig.rt.events_log)
    step = selected.itinerary.steps[0]
    rig.clock.t = step.departure
    result = await rig.rt.confirm_recovery_step(
        selected.key,
        selected.id,
        0,
        0,
        "boarded",
        step.origin.physical_id,
        "confirmed-despite-source-outage",
    )
    assert isinstance(result, SelectedRecovery) and result.next_action == "arrived"
    assert result.monitoring_issue == "unavailable"


async def test_source_changes_require_review_and_never_replace_or_advance_selected_plan(tmp_path):
    from dataclasses import replace
    from datetime import timedelta

    rig = await world(tmp_path)
    selected = await follow(rig)
    connection = rig.provider.world[0]
    changed = []
    for index, leg in enumerate(connection.legs):
        stops = []
        for stop in leg.stops:
            updates = {}
            if index >= 2:
                for field in ("arrival", "departure"):
                    obs = getattr(stop, field)
                    if obs is not None:
                        from train_commute_companion.model import Provenance

                        updates[field] = replace(
                            obs,
                            value=(obs.value or obs.planned) + timedelta(minutes=1),
                            provenance=Provenance.REPORTED,
                        )
            stops.append(replace(stop, **updates))
        changed.append(replace(leg, stops=tuple(stops)))
    rig.provider.world = [replace(connection, legs=tuple(changed))]
    rig.clock.jump(1.1)
    await rig.rt.poll_once()
    await rig.rt.recovery_idle()
    displayed = occurrence(rig).selected_recovery
    assert displayed.id == selected.id and displayed.itinerary == selected.itinerary
    assert displayed.index == 0 and displayed.revision == 0 and displayed.review_required
    assert displayed.monitoring_issue == "changed"
    assert occurrence(rig).replacement_plans
    assert all(
        not q.origin.startswith("@place:") and q.origin != "8400002" for q in rig.provider.calls
    )


async def test_duplicate_runtime_callback_keeps_first_timestamp_and_skip_ends_recovery(tmp_path):
    rig = await world(tmp_path)
    selected = await follow(rig)
    place = selected.itinerary.steps[0].origin.physical_id
    first = await rig.rt.confirm_recovery_step(
        selected.key, selected.id, 0, 0, "boarded", place, "same-query"
    )
    rig.clock.jump(1)
    again = await rig.rt.confirm_recovery_step(
        selected.key, selected.id, 0, 0, "boarded", place, "same-query"
    )
    assert again == first
    wrong = await rig.rt.confirm_recovery_step(
        selected.key, selected.id, 0, 0, "arrived", place, "same-query"
    )
    assert isinstance(wrong, Refused)
    await rig.rt.not_travelling("p1", selected.key.service_date, selected.key.direction)
    assert occurrence(rig).selected_recovery.status == "stopped"
    assert occurrence(rig).recovery_offer is None


async def test_original_train_warnings_remain_labeled_while_routine_advice_is_suppressed(tmp_path):
    from dataclasses import replace

    from builders import make_leg
    from lifecycle_fixtures import conn, journey

    from train_commute_companion.model import DirectionState, EventKind, okey

    rig = await world(tmp_path)
    selected = await follow(rig)
    original = make_leg("UT", "RTD", "12:00", "13:34", number="7777", day=NOW.date())
    stored = rig.rt.state.occurrences[okey(selected.key)]
    rig.occ_set(
        replace(stored, lock=journey([original]), direction_state=DirectionState.LOCKED),
        key=okey(selected.key),
    )
    cancelled = make_leg(
        "UT", "RTD", "12:00", "13:34", number="7777", day=NOW.date(), cancelled=True
    )
    rig.provider.world = [conn(cancelled), *rig.provider.world]
    rig.notifier.attempts.clear()
    await rig.rt.poll_once()
    await rig.rt.recovery_idle()
    warnings = [e for e in rig.notifier.attempts if e.kind is EventKind.CANCELLED]
    assert warnings and all(e.text.startswith("Original train monitoring:") for e in warnings)
    assert all(
        e.kind not in (EventKind.LEAVE_NOW, EventKind.ALL_GOOD, EventKind.NEXT_CONNECTION)
        for e in rig.notifier.attempts
    )
    assert occurrence(rig).selected_recovery.id == selected.id


async def test_repeated_monitoring_outages_are_new_episodes_after_original_train_finishes(tmp_path):
    from dataclasses import replace

    from train_commute_companion.model import DirectionState, EventKind, okey
    from train_commute_companion.providers.base import ServerError

    rig = await world(tmp_path)
    selected = await follow(rig)
    stored = rig.rt.state.occurrences[okey(selected.key)]
    rig.occ_set(replace(stored, direction_state=DirectionState.DONE), key=okey(selected.key))
    for failure, advance in ((True, 1.1), (False, 5), (True, 1.1)):
        rig.provider.error = (lambda query: ServerError()) if failure else (lambda query: None)
        rig.clock.jump(advance)
        await rig.rt.poll_once()
        await rig.rt.recovery_idle()
    notices = [event for event in rig.rt.events_log if event.kind is EventKind.RECOVERY_MONITORING]
    assert len(notices) == 3
    assert occurrence(rig).resolved is False
    assert occurrence(rig).selected_recovery.index == 0


async def test_monitoring_issue_and_notice_survive_same_durable_write(tmp_path):
    from train_commute_companion.model import EventKind
    from train_commute_companion.providers.base import ServerError

    rig = await world(tmp_path)
    await follow(rig)
    rig.provider.error = lambda query: ServerError()
    saved = rig.store.save
    writes = 0

    def fail_after_first_write(state):
        nonlocal writes
        writes += 1
        if writes > 1:
            rig.store.fail = True
        saved(state)

    rig.store.save = fail_after_first_write
    rig.clock.jump(1.1)
    await rig.rt._recovery._refresh()
    persisted = rig.store.load()
    assert persisted is not None
    assert any(o.event.kind is EventKind.RECOVERY_MONITORING for o in persisted.obligations)
    rig.store.save = saved
    rig.store.fail = False
    await rig.restart()
    await rig.rt.poll_once()
    await rig.rt.recovery_idle()
    assert any(e.kind is EventKind.RECOVERY_MONITORING for e in rig.rt.events_log)


async def test_older_prepare_cannot_replace_a_newer_reviewed_offer(tmp_path):
    import asyncio

    rig = await world(tmp_path)
    shown = occurrence(rig)
    plan = shown.replacement_plans[0]
    origin = next(s.origin for s in plan.itinerary.steps if s.mode == "BUS")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def block_first(_query):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()

    rig.provider.hook = block_first
    old = asyncio.create_task(
        rig.rt.prepare_recovery(shown.key, plan.proposal_id, origin.physical_id)
    )
    await entered.wait()
    latest = await rig.rt.prepare_recovery(shown.key, plan.proposal_id, origin.physical_id)
    assert isinstance(latest, RecoveryOffer)
    release.set()
    previous = await old
    assert isinstance(previous, Refused)
    assert occurrence(rig).recovery_offer == latest
