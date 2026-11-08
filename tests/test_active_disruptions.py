import asyncio
from dataclasses import replace

import pytest
from builders import make_leg
from lifecycle_fixtures import at, ctx, journey, occ, usual_locked
from runtime_rig import Rig

from train_commute_companion.disruptions import disruptions_told, update_disruptions
from train_commute_companion.model import EventKind, Obligation, okey
from train_commute_companion.providers.base import (
    ActiveDisruption,
    ActiveDisruptions,
    ProviderError,
)
from train_commute_companion.providers.ns import map_active_disruptions
from train_commute_companion.serde import dumps, loads
from train_commute_companion.store import PersistedState
from train_commute_companion.view import occurrence_view


def item(title="A signal fault", stations=frozenset({"UT"})):
    return ActiveDisruption("incident-1", title, stations, None)


def feed(*items, coverage="complete"):
    return ActiveDisruptions(items, coverage)


def test_mapper_uses_source_ids_and_stations_without_requiring_dates():
    result = map_active_disruptions(
        [
            {
                "id": "42",
                "type": "DISRUPTION",
                "isActive": True,
                "title": "Signal fault",
                "publicationSections": [
                    {"section": {"stations": [{"stationCode": "UT", "uicCode": "8400621"}]}}
                ],
            },
            {
                "id": "banner",
                "type": "CALAMITY",
                "isActive": True,
                "title": "Works ahead",
                "url": "https://www.ns.nl/reizen/berichten/gevolgen",
            },
        ]
    )
    assert result.coverage == "complete"
    assert result.items[0].id == "42"
    assert result.items[0].stations == frozenset({"UT", "8400621"})
    assert result.items[1].stations == frozenset()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        [{"title": "missing identity"}],
        [
            {
                "id": "x",
                "type": "DISRUPTION",
                "isActive": True,
                "title": "x",
                "publicationSections": "broken",
            }
        ],
    ],
)
def test_malformed_feed_is_unknown_not_an_empty_complete_snapshot(raw):
    assert map_active_disruptions(raw).coverage == "unknown"


def test_matching_notice_changes_and_clears_only_after_complete_snapshot():
    context = ctx(at("07:50"))
    first = update_disruptions(usual_locked(), context, feed(item()))
    assert [e.kind for e in first.events] == [EventKind.DISRUPTION]
    assert "NS reports" in first.events[0].text and "check your journey" in first.events[0].text
    assert "your train is cancelled" not in first.events[0].text
    first = replace(
        first,
        state=replace(
            first.state,
            disruptions={
                "incident-1": replace(
                    first.state.disruptions["incident-1"], notified=True, announced=True
                )
            },
        ),
    )
    changed = update_disruptions(first.state, context, feed(item("Fault has expanded")))
    assert changed.events[0].key != first.events[0].key
    uncertain = update_disruptions(changed.state, context, feed(coverage="unknown"))
    assert uncertain.state.disruptions["incident-1"].active
    assert all(e.kind is not EventKind.DISRUPTION_CLEARED for e in uncertain.events)
    cleared = update_disruptions(uncertain.state, context, feed())
    assert not cleared.state.disruptions["incident-1"].active
    assert [e.kind for e in cleared.events] == [EventKind.DISRUPTION_CLEARED]
    returned = update_disruptions(cleared.state, context, feed(item("Fault has expanded")))
    assert returned.events[0].key != changed.events[0].key
    assert (
        loads(type(returned.state), dumps(returned.state)).disruptions == returned.state.disruptions
    )


def test_irrelevant_or_unscoped_notices_do_not_start_alerts_or_clear_known_incident():
    context = ctx(at("07:50"))
    initial = update_disruptions(
        usual_locked(), context, feed(item(stations=frozenset({"ZL"})))
    ).events
    assert not initial
    initial = update_disruptions(usual_locked(), context, feed(item(stations=frozenset()))).events
    assert not initial
    first = update_disruptions(usual_locked(), context, feed(item()))
    unknown_scope = update_disruptions(first.state, context, feed(item(stations=frozenset())))
    assert unknown_scope.state.disruptions["incident-1"].active
    assert all(e.kind is not EventKind.DISRUPTION_CLEARED for e in unknown_scope.events)


def test_pause_skip_and_closed_window_do_not_create_context_alerts():
    context = ctx(at("07:50"))
    paused = replace(
        context,
        profile=replace(
            context.profile,
            pauses=((context.schedule.key.service_date, context.schedule.key.service_date),),
        ),
    )
    assert not update_disruptions(usual_locked(), paused, feed(item())).events
    assert not update_disruptions(
        replace(usual_locked(), not_travelling=True), context, feed(item())
    ).events
    assert not update_disruptions(usual_locked(), ctx(at("11:30")), feed(item())).events


def test_followed_reroute_does_not_match_the_unused_usual_transfer_station():
    reroute = occ(
        journey(
            [
                make_leg("AAA", "BBB", "08:12", "08:40", number="9001"),
                make_leg("BBB", "RTD", "08:50", "09:24", number="9002"),
            ]
        )
    )
    assert not update_disruptions(reroute, ctx(at("07:50")), feed(item())).events


async def test_runtime_fetches_once_after_trip_requests_and_deduplicates_across_restart(tmp_path):
    rig = Rig(tmp_path, "08:04")
    rig.provider.disruptions = feed(item())
    await rig.rt.start()
    initial_calls = rig.provider.disruption_calls
    assert initial_calls == 1
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION]) == 1
    await rig.rt.poll_once()
    assert rig.provider.disruption_calls == initial_calls + 1
    await rig.restart()
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION]) == 1
    rig.provider.disruptions = feed()
    await rig.rt.poll_once()
    await rig.rt.poll_once()
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION_CLEARED]) == 1


async def test_failed_snapshot_never_clears_and_pending_starts_drop_after_real_clear(tmp_path):
    rig = Rig(tmp_path, "08:04")
    rig.provider.disruptions = feed(item())
    await rig.rt.start()
    rig.notifier.fails = lambda e: e.kind is EventKind.DISRUPTION
    rig.provider.disruptions = feed(item("Fault has expanded"))
    rig.clock.t = at("08:05")
    await rig.rt.poll_once()
    assert any(o.event.kind is EventKind.DISRUPTION for o in rig.rt.state.obligations)
    rig.provider.disruption_error = ProviderError("offline")
    rig.clock.t = at("08:06")
    await rig.rt.poll_once()
    assert not [e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION_CLEARED]
    rig.provider.disruption_error = None
    rig.provider.disruptions = feed()
    rig.notifier.fails = lambda e: False
    rig.clock.t = at("08:08")
    await rig.rt.poll_once()
    assert not any(o.event.kind is EventKind.DISRUPTION for o in rig.rt.state.obligations)
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION_CLEARED]) == 1


async def test_later_incident_does_not_cover_a_different_pending_incident(tmp_path):
    rig = Rig(tmp_path, "08:04")
    first = item()
    second = replace(item("Another fault"), id="incident-2")
    rig.provider.disruptions = feed(first)
    rig.notifier.fails = lambda e: e.kind is EventKind.DISRUPTION
    await rig.rt.start()
    rig.clock.t = at("08:06")
    rig.provider.disruptions = feed(first, second)
    await rig.rt.poll_once()
    held = {
        o.event.facts.disruption_id
        for o in rig.rt.state.obligations
        if o.event.kind is EventKind.DISRUPTION
    }
    assert held == {"incident-1", "incident-2"}


def test_incident_that_moves_outside_route_does_not_stay_as_route_context():
    context = ctx(at("07:50"))
    first = update_disruptions(usual_locked(), context, feed(item()))
    moved = update_disruptions(first.state, context, feed(item(stations=frozenset({"ZL"}))))
    assert not moved.events
    returned = update_disruptions(moved.state, context, feed(item()))
    assert returned.events[0].key != first.events[0].key


async def test_quiet_notice_that_clears_before_delivery_does_not_send_an_unprompted_clear(tmp_path):
    rig = Rig(tmp_path, "08:04")
    rig.write(
        text=rig.path.read_text().replace(
            "transfer_min: {UT: 4}",
            'transfer_min: {UT: 4}\n    alerts: {quiet_hours: ["07:00", "08:08"]}',
        )
    )
    rig.provider.disruptions = feed(item())
    await rig.rt.start()
    assert not [e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION]
    rig.provider.disruptions = feed()
    rig.clock.t = at("08:06")
    await rig.rt.poll_once()
    rig.clock.t = at("08:08")
    await rig.rt.poll_once()
    assert not [
        e
        for e in rig.notifier.attempts
        if e.kind in (EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED)
    ]


def test_duplicate_conflicts_and_unsafe_links_do_not_become_false_certainty():
    base = {
        "id": "42",
        "type": "DISRUPTION",
        "isActive": True,
        "title": "Signal fault",
        "url": "https://private:secret@www.ns.nl/example",
    }
    result = map_active_disruptions([base])
    assert result.items[0].url is None
    assert (
        map_active_disruptions([base, {**base, "title": "Conflicting title"}]).coverage == "unknown"
    )


def test_old_checked_context_is_explicitly_unverified_in_view():
    context = ctx(at("07:50"))
    first = update_disruptions(usual_locked(), context, feed(item()))
    shown = occurrence_view(first.state, ctx(at("08:10")), ())
    assert shown.disruptions
    assert shown.disruptions_coverage == "unknown"


async def test_stale_held_notice_waits_for_a_fresh_feed_before_delivery(tmp_path):
    rig = Rig(tmp_path, "08:04")
    rig.write(
        text=rig.path.read_text().replace(
            "transfer_min: {UT: 4}",
            'transfer_min: {UT: 4}\n    alerts: {quiet_hours: ["07:00", "08:10"]}',
        )
    )
    rig.provider.disruptions = feed(item())
    await rig.rt.start()
    rig.clock.t = at("08:10")
    await rig.rt.watchdog_once()
    assert not [e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION]
    await rig.rt.poll_once()
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION]) == 1


@pytest.mark.parametrize(
    "active,relevant,announced,notified,expected_notified",
    [
        (False, True, False, True, False),
        (True, True, False, False, False),
        (False, True, True, True, True),
        (False, False, False, True, True),
    ],
)
def test_start_success_remains_announced_after_source_revision_changes(
    active, relevant, announced, notified, expected_notified
):
    first = update_disruptions(usual_locked(), ctx(at("07:50")), feed(item()))
    event = first.events[0]
    current = replace(
        first.state.disruptions["incident-1"],
        revision=2,
        active=active,
        relevant=relevant,
        announced=announced,
        notified=notified,
    )
    occurrence = replace(first.state, disruptions={"incident-1": current})
    key = okey(occurrence.key)
    stored = PersistedState(None, {}, {}, {}, {key: occurrence}, (), False)
    obligation = Obligation("old-start", event, occurrence.key, None, 1, None, event.key, None, 0)
    result = disruptions_told(stored, [(obligation, True)])
    notice = result.occurrences[key].disruptions["incident-1"]
    assert notice.announced is True
    assert notice.revision == 2
    assert notice.notified is expected_notified


async def test_start_delivery_racing_with_source_clear_still_gets_one_clear(tmp_path):
    rig = Rig(tmp_path, "08:04")
    rig.provider.disruptions = feed(item())
    started, release, read_clear = asyncio.Event(), asyncio.Event(), asyncio.Event()
    send = rig.notifier.send
    read_feed = rig.provider.active_disruptions

    async def delayed_send(event):
        if event.kind is EventKind.DISRUPTION:
            started.set()
            await release.wait()
        return await send(event)

    async def observed_feed():
        result = await read_feed()
        if not result.items:
            read_clear.set()
        return result

    rig.notifier.send = delayed_send
    rig.provider.active_disruptions = observed_feed
    initial = asyncio.create_task(rig.rt.start())
    try:
        await asyncio.wait_for(started.wait(), 2)
        rig.provider.disruptions = feed()
        rig.clock.t = at("08:06")
        polling = asyncio.create_task(rig.rt.poll_once())
        try:
            await asyncio.wait_for(read_clear.wait(), 2)
            assert rig.occ().disruptions["incident-1"].active is False
        finally:
            release.set()
            await asyncio.wait_for(polling, 2)
    finally:
        release.set()
        await asyncio.wait_for(initial, 2)
    await rig.rt.poll_once()
    await rig.rt.poll_once()
    notices = [
        e.kind
        for e in rig.notifier.attempts
        if e.kind in (EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED)
    ]
    assert notices == [EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED]


@pytest.mark.parametrize(
    "kind,sent", [(EventKind.DISRUPTION_CLEARED, True), (EventKind.DISRUPTION, False)]
)
def test_old_clear_or_failed_start_does_not_acknowledge_new_active_revision(kind, sent):
    first = update_disruptions(usual_locked(), ctx(at("07:50")), feed(item()))
    event = replace(first.events[0], kind=kind)
    current = replace(first.state.disruptions["incident-1"], revision=2)
    occurrence = replace(first.state, disruptions={"incident-1": current})
    key = okey(occurrence.key)
    stored = PersistedState(None, {}, {}, {}, {key: occurrence}, (), False)
    obligation = Obligation("old-event", event, occurrence.key, None, 1, None, event.key, None, 0)
    result = disruptions_told(stored, [(obligation, sent)])
    assert result.occurrences[key].disruptions["incident-1"] == current
