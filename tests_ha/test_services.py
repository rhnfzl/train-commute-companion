from dataclasses import replace
from datetime import date

import pytest
from builders import make_connection, make_leg
from harness import (
    ONE_TRANSFER,
    PROFILE,
    PROFILES,
    drive,
    only_occurrence,
    setup_tcc,
    write_profiles,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from test_runtime import at

from custom_components.train_commute_companion.const import DOMAIN
from train_commute_companion.lifecycle import DirectionState
from train_commute_companion.location import LocationEvent
from train_commute_companion.model import Direction, OccurrenceKey
from train_commute_companion.runtime import _fresh_state

pytestmark = pytest.mark.usefixtures("tcc_freezer")

B_ONLY = (PROFILES / "office_b_only.yaml").read_text()
TUE = date(2026, 10, 27)


def state(hass, eid):
    return hass.states.get(eid).state


def attr(hass, eid, name):
    return hass.states.get(eid).attributes[name]


async def settle(hass):
    # the coordinator debounces a second requested refresh within 10 s; tests read at once
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def call(hass, service, **data):
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await settle(hass)


async def press(hass, eid):
    await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
    await settle(hass)


def refused(h) -> bool:
    return any("cannot follow that connection" in c.data["message"] for c in h.pushes)


async def test_choose_service_locks_departure(hass):
    h = await setup_tcc(hass, PROFILE)
    await call(hass, "choose", profile="office_a", departure="08:44")
    assert attr(hass, "sensor.office_a_outbound_lock", "source") == "choose"
    leg1 = "sensor.office_a_outbound_leg_1"
    assert attr(hass, leg1, "planned_departure") == at("08:44").isoformat()
    assert not refused(h)


async def test_choose_retained_remainder_the_user_rides(hass):  # review-codex-rev11 N3, required
    h = await setup_tcc(hass, PROFILE, now="08:00")
    await drive(h, "08:18")  # auto lock 08:12
    await h.host.run(
        lambda rt: rt.location("p1", LocationEvent("AAA", "report", at("08:18"), at("08:18")))
    )
    await drive(h, "08:20")  # rule 7: lock now 08:44, 08:12 retained
    pre = await h.host.run(lambda rt: only_occurrence(rt))
    assert pre.lock.legs[0].train.planned_departure == at("08:44")
    assert pre.retained[0].legs[0].train.planned_departure == at("08:12")  # the precondition
    await call(hass, "choose", profile="office_a", departure="08:12")
    s = await h.host.run(lambda rt: only_occurrence(rt))
    assert s.lock.legs[0].train.planned_departure == at("08:12")
    assert s.lock.legs[0].state is pre.retained[0].legs[0].state  # ridden prefix keeps its progress
    assert s.direction_state is DirectionState.RIDING and s.retained == ()
    assert not refused(h)


async def test_choose_unknown_time_raises_with_reason(hass):
    await setup_tcc(hass, PROFILE)
    with pytest.raises(ServiceValidationError, match="no train at 08:13"):
        await call(hass, "choose", profile="office_a", departure="08:13")


async def test_unknown_profile_raises(hass):
    await setup_tcc(hass, PROFILE)
    with pytest.raises(ServiceValidationError, match="unknown profile nope; known: office_a"):
        await call(hass, "leaving", profile="nope")


async def test_service_before_setup_raises_not_loaded(hass):
    h = await setup_tcc(hass, PROFILE)
    await hass.config_entries.async_unload(h.entry.entry_id)
    with pytest.raises(ServiceValidationError, match="not loaded"):
        await call(hass, "leaving", profile="office_a")


async def test_leaving_with_no_open_journey_raises(hass):
    await setup_tcc(hass, PROFILE, now="12:00")
    with pytest.raises(ServiceValidationError, match="no journey of office_a is being followed"):
        await call(hass, "leaving", profile="office_a")


async def test_leaving_marks_travelling(hass):
    await setup_tcc(hass, PROFILE)
    assert state(hass, "sensor.office_a_outbound_lock") == "intended"  # auto lock at 07:47
    await call(hass, "leaving", profile="p1", direction="outbound")
    # travelling shows as the committed lock (7.2); the status stays locked until boarding
    assert state(hass, "sensor.office_a_outbound_lock") == "committed"


async def test_not_travelling_whole_day_expires_both_directions(hass):
    h = await setup_tcc(hass, PROFILE)
    await call(hass, "not_travelling", profile="office_a", whole_day=True)
    assert state(hass, "sensor.office_a_outbound_status") == "expired"
    assert state(hass, "sensor.office_a_return_status") == "expired"
    before = len(h.pushes)
    first = h.provider.world[0]
    gone = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True)
    h.provider.world[0] = replace(first, legs=(gone, *first.legs[1:]))
    await drive(h, "08:30")
    assert len(h.pushes) == before


async def test_not_travelling_with_date_and_direction_touches_only_that_direction(hass):
    h = await setup_tcc(hass, PROFILE)
    key = OccurrenceKey("p1", Direction.RETURN, TUE)

    def seed(rt):  # as the core test does: both of Tuesday's directions unresolved at once
        st = rt.state
        fresh = _fresh_state(key, rt.config.revision, None)
        rt._st = replace(st, occurrences={**st.occurrences, "p1|return|2026-10-27": fresh})

    await h.host.run(seed)
    await call(
        hass, "not_travelling", profile="office_a", service_date=TUE.isoformat(), direction="return"
    )
    assert state(hass, "sensor.office_a_return_status") == "expired"
    assert state(hass, "sensor.office_a_outbound_status") == "locked"  # still alerting


async def test_buttons_run_the_same_actions(hass):
    h = await setup_tcc(hass, PROFILE)
    await press(hass, "button.office_a_outbound_leaving")
    assert state(hass, "sensor.office_a_outbound_lock") == "committed"
    await press(hass, "button.office_a_outbound_choose_next")
    assert attr(hass, "sensor.office_a_outbound_lock", "trains")[0].startswith("08:44")
    assert attr(hass, "sensor.office_a_outbound_lock", "source") == "choose"
    assert not refused(h)
    await press(hass, "button.office_a_not_travelling_today")
    assert state(hass, "sensor.office_a_outbound_status") == "expired"
    assert state(hass, "sensor.office_a_return_status") == "expired"


TIGHT = make_connection(  # the 08:44 again, onward after 2 min at UT (transfer_min 4)
    [
        make_leg("AAA", "UT", "08:44", "09:08", number="1236"),
        make_leg("UT", "RTD", "09:10", "09:34", number="5690"),
    ]
)


async def test_choose_next_locks_the_shown_connection_not_an_earlier_broken_one(hass):  # 7.12 r1
    h = await setup_tcc(hass, PROFILE, world=[*ONE_TRANSFER, TIGHT])
    # shown: the 09:16 onward (arrives 09:50); the 09:10 arrives first but its transfer fails
    assert (
        attr(hass, "sensor.office_a_outbound_next_connection", "arrival") == at("09:50").isoformat()
    )
    await press(hass, "button.office_a_outbound_choose_next")
    assert attr(hass, "sensor.office_a_outbound_lock", "source") == "choose"
    s = await h.host.run(lambda rt: only_occurrence(rt))
    assert [x.train.planned_departure for x in s.lock.legs] == [at("08:44"), at("09:16")]
    assert not refused(h)


async def test_choose_raises_with_the_refusal_when_no_connection_passes(hass):  # 7.12 r1
    h = await setup_tcc(hass, PROFILE, world=[ONE_TRANSFER[0], TIGHT])
    with pytest.raises(ServiceValidationError, match="choice refused: transfer at UT broken"):
        await call(hass, "choose", profile="office_a", departure="08:44")
    s = await h.host.run(lambda rt: only_occurrence(rt))
    assert s.lock.legs[0].train.planned_departure == at("08:12")


async def test_not_travelling_direction_button_expires_only_that_direction(hass):  # 10.4
    await setup_tcc(hass, PROFILE)
    await press(hass, "button.office_a_outbound_not_travelling")
    assert state(hass, "sensor.office_a_outbound_status") == "expired"
    assert state(hass, "sensor.office_a_return_status") != "expired"


async def test_choose_next_with_nothing_to_choose_raises(hass):
    await setup_tcc(hass, PROFILE, now="12:00")
    with pytest.raises(HomeAssistantError, match="no next connection to choose now"):
        await press(hass, "button.office_a_outbound_choose_next")


async def test_actions_reach_a_removed_profile_that_still_runs(hass):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:20")
    assert state(hass, "sensor.office_a_outbound_status") == "riding"
    write_profiles(hass, B_ONLY)
    await h.host.run(lambda rt: rt.check_file())
    await h.entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    for eid in ("button.office_a_outbound_leaving", "button.office_a_not_travelling_today"):
        assert hass.states.get(eid) is not None, eid
    await call(hass, "not_travelling", profile="office_a", direction="outbound")
    s = await h.host.run(lambda rt: only_occurrence(rt, Direction.OUTBOUND))
    assert s.direction_state is DirectionState.EXPIRED
