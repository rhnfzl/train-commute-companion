from dataclasses import replace
from datetime import time, timedelta
from urllib.parse import parse_qs, urlparse

from builders import make_connection, make_leg
from recovery_sample import NOW, selection
from runtime_rig import Rig
from test_arrival_prompt import start, write_profile
from test_runtime_recovery import occurrence, world

from train_commute_companion.model import EventKind, alert_facts
from train_commute_companion.navigation import boarding_station, station_links
from train_commute_companion.recovery_state import RecoveryOffer, SelectedRecovery
from train_commute_companion.runtime import fresh_state


def outbound(rig):
    return next(o for o in rig.rt.view().occurrences if o.key.direction.value == "outbound")


def assert_maps(links, point):
    apple, google = map(urlparse, links)
    assert (apple.scheme, apple.netloc, apple.path) == ("https", "maps.apple.com", "/place")
    assert (google.scheme, google.netloc, google.path) == (
        "https",
        "www.google.com",
        "/maps/search/",
    )
    assert parse_qs(apple.query) == {"coordinate": [point]}
    assert parse_qs(google.query) == {"api": ["1"], "query": [point]}


async def test_current_pretrip_links_only_use_catalogue_and_expire(tmp_path):
    rig = Rig(tmp_path, now="08:05")
    rig.provider.positions = {"AAA": (52.1, 5.2), "RTD": (51.9, 4.5)}
    await start(rig)
    assert_maps(outbound(rig).navigation, "52.1,5.2")
    [check_in] = [e for e in rig.notifier.attempts if e.kind is EventKind.ALL_GOOD]
    assert_maps(alert_facts(check_in).navigation, "52.1,5.2")
    rig.clock.jump(5)
    assert outbound(rig).navigation is None
    await rig.rt.poll_once()
    assert_maps(outbound(rig).navigation, "52.1,5.2")
    rig.clock.set("08:12")
    await rig.rt.poll_once()
    assert outbound(rig).navigation is None


async def test_explicit_alternate_boarding_replaces_usual_map_target(tmp_path):
    rig = Rig(tmp_path, now="08:05")
    rig.provider.positions = {"AAA": (52.1, 5.2), "VL": (51.4, 6.2)}
    rig.provider.world.append(
        make_connection([make_leg("VL", "RTD", "08:26", "09:15", number="9001")])
    )
    await start(rig)
    assert await rig.rt.choose_at("p1", time(8, 26), "9001", station="VL") is None
    occurrence = outbound(rig)
    assert occurrence.legs[0].board == "VL"
    assert_maps(occurrence.navigation, "51.4,6.2")
    [sent] = [e for e in rig.notifier.attempts if e.kind is EventKind.STATION_NAVIGATION]
    assert_maps(alert_facts(sent).navigation, "51.4,6.2")
    write_profile(rig, "navigate: false")
    assert await rig.rt.reload() is None
    assert outbound(rig).navigation is None


def test_recovery_only_navigates_to_a_fresh_supported_train_station():
    selected = selection()
    state = replace(fresh_state(selected.key, 0, None), selected_recovery=selected)
    assert boarding_station(state, (), NOW, None, None) == "UT"
    assert boarding_station(state, (), NOW + timedelta(minutes=5), None, None) == ""
    changed = replace(state, selected_recovery=replace(selected, review_required=True))
    assert boarding_station(changed, (), NOW, None, None) == ""
    bus = replace(state, selected_recovery=replace(selected, index=1))
    assert boarding_station(bus, (), NOW, None, None) == ""
    assert station_links({"RTD": (52.1, 5.2)}, "UT") is None
    assert station_links({"UT": (float("nan"), 5.2)}, "UT") is None


async def test_reselecting_recovery_sends_a_map_for_the_new_selection(tmp_path):
    rig = await world(tmp_path)
    rig.rt._primary_destination = lambda: "test-phone"
    rig.provider.positions = {"UT": (52.09, 5.11)}

    async def select():
        shown = occurrence(rig)
        plan = shown.replacement_plans[0]
        first = plan.itinerary.steps[0]
        offer = await rig.rt.prepare_recovery(shown.key, plan.proposal_id, first.origin.physical_id)
        assert isinstance(offer, RecoveryOffer)
        selected = await rig.rt.select_recovery(
            offer.key,
            offer.id,
            acknowledge_planned_times=True,
            acknowledge_accessibility_unknown=True,
        )
        assert isinstance(selected, SelectedRecovery)
        await rig.rt._deliver()
        return selected

    first = await select()
    await rig.rt.stop_recovery(first.key, first.id, first.revision)
    second = await select()
    await rig.rt.recovery_idle()
    assert first.id != second.id
    notices = [e for e in rig.notifier.attempts if e.kind is EventKind.STATION_NAVIGATION]
    assert [alert_facts(e).recovery_selection for e in notices] == [first.id, second.id]
    for notice in notices:
        assert "Original train monitoring" not in notice.text
        assert_maps(alert_facts(notice).navigation, "52.09,5.11")
