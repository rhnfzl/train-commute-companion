from datetime import date, time, timedelta

import pytest
from builders import make_connection, make_leg
from runtime_rig import DAY, PROFILE, Rig, at, usual_on

from train_commute_companion.location import LocationEvent
from train_commute_companion.model import Direction, EventKind
from train_commute_companion.providers.base import PlannedWorks


def utrecht(**options):
    return {
        "action": "create",
        "plan": {
            "name": "Utrecht",
            "origin": "AAA",
            "destination": "UT",
            "start_date": (DAY + timedelta(days=7)).isoformat(),
            "end_date": (DAY + timedelta(days=21)).isoformat(),
            "weekdays": [2],
            "skip_dates": [(DAY + timedelta(days=14)).isoformat()],
            "outbound": {"mode": "exact", "departure": "08:12", "train_number": "8801"},
        },
        **options,
    }


def save(rt, change, request_id):
    review = rt.preview_travel_plan(change)
    assert review["status"] == "ready", review
    saved = rt.save_travel_plan(change, review["review_token"], request_id)
    assert saved["status"] == "saved", saved
    return saved


async def test_temporary_replacement_follows_the_selected_train_after_restart(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    review = rig.rt.preview_travel_plan(utrecht())
    assert review["status"] == "conflict"
    assert "2026-11-03" in review["text"]
    assert "2026-11-10" not in review["text"]
    assert "2026-11-17" in review["text"]
    change = utrecht(conflict_policy="replace")
    saved = save(rig.rt, change, "1" * 64)
    day = DAY + timedelta(days=7)
    rig.clock.t = at("07:55", day)
    rig.provider.world = [
        make_connection([make_leg("AAA", "UT", "08:12", "08:38", number=n, day=day)])
        for n in ("8800", "8801")
    ]
    await rig.restart()
    assert rig.rt.travel_change_receipt("1" * 64) == saved
    occ = rig.occ(f"{saved['plan_id']}|outbound|{day}")
    assert occ is not None and occ.lock is not None
    assert occ.lock.legs[0].train.train_number == "8801"
    assert rig.occ(f"p1|outbound|{day}") is None
    for offset in (14, 28):
        ordinary = DAY + timedelta(days=offset)
        rig.clock.t = at("07:55", ordinary)
        await rig.rt.poll_once()
        assert rig.occ(f"p1|outbound|{ordinary}") is not None


async def test_monthly_plan_skips_absent_weekdays_and_ends_after_count(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    change = utrecht(conflict_policy="replace")
    plan = change["plan"]
    del plan["end_date"], plan["weekdays"]
    plan.update(
        start_date="2026-11-01",
        monthly={"ordinal": 5, "weekday": 1},
        end_after=2,
        skip_dates=["2026-11-30"],
    )
    saved = save(rig.rt, change, "monthly-trip")
    await rig.restart()

    actual = rig.rt.state.travel_plans[saved["plan_id"]]
    assert tuple(actual.dates(date(2026, 11, 1), date(2027, 9, 1))) == (
        date(2027, 3, 29),
        date(2027, 5, 31),
    )
    assert not actual.runs(date(2027, 8, 30))
    (listed,) = rig.rt.list_travel_plans()["plans"]
    assert listed["end_date"] is None
    assert listed["end_after"] == 2
    assert listed["monthly"] == {"ordinal": 5, "weekday": 1}


async def test_ongoing_monthly_preview_stops_after_detecting_more_conflicts(tmp_path, monkeypatch):
    from train_commute_companion import travel_changes

    rig = Rig(tmp_path)
    await rig.rt.start()
    change = utrecht()
    plan = change["plan"]
    del plan["end_date"], plan["weekdays"]
    plan.update(start_date="2026-11-01", monthly={"ordinal": 1, "weekday": 1}, skip_dates=[])
    original = travel_changes._regular_schedules
    checked = 0

    def counted(state, profile, day):
        nonlocal checked
        checked += 1
        return original(state, profile, day)

    monkeypatch.setattr(travel_changes, "_regular_schedules", counted)
    review = rig.rt.preview_travel_plan(change)
    assert review["status"] == "conflict"
    (collision,) = review["conflicts"]
    assert len(collision["dates"]) == 12
    assert collision["dates"][0] == "2026-11-02"
    assert collision["more_dates"]
    assert not collision["keep"]
    assert checked < 200


async def test_monthly_preview_keeps_late_extra_date_with_an_overnight_return(tmp_path):
    profile = PROFILE.split('  - id: "p2"')[0].replace(
        "[mon, tue, wed, thu, fri]",
        "[sat]\n    extra_dates: [2450-02-07]",
    )
    rig = Rig(tmp_path)
    rig.path.write_text(profile.replace("NAME", "office_a"))
    await rig.rt.start()
    change = utrecht()
    plan = change["plan"]
    del plan["end_date"], plan["weekdays"]
    plan.update(
        start_date="2026-11-01",
        monthly={"ordinal": 1, "weekday": 7},
        skip_dates=[],
    )
    plan["return"] = {"mode": "exact", "departure": "08:12", "day_offset": 1}

    review = rig.rt.preview_travel_plan(change)
    assert review["status"] == "conflict"
    (collision,) = review["conflicts"]
    assert collision["dates"] == ["2450-02-07"]
    assert not collision["more_dates"]
    assert not collision["keep"]


@pytest.mark.parametrize("monthly_first", [False, True])
async def test_monthly_and_weekly_plans_detect_overlaps_beyond_one_year(tmp_path, monthly_first):
    rig = Rig(tmp_path)
    rig.path.unlink()
    await rig.rt.start()
    weekly = utrecht()
    weekly["plan"].update(
        start_date="2026-11-02",
        end_date="2028-11-01",
        weekdays=[1],
        interval_weeks=52,
        skip_dates=[],
    )
    monthly = utrecht()
    del monthly["plan"]["weekdays"]
    monthly["plan"].update(
        start_date="2026-11-01",
        end_date="2028-11-01",
        monthly={"ordinal": 5, "weekday": 1},
        skip_dates=[],
    )
    first, second = (monthly, weekly) if monthly_first else (weekly, monthly)
    save(rig.rt, first, "first-repeat")
    review = rig.rt.preview_travel_plan(second)
    assert review["status"] == "conflict"
    assert "2028-10-30" in review["text"]


async def test_preview_is_revision_bound_and_retry_cannot_make_a_duplicate(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    change = utrecht(conflict_policy="replace")
    reviewed = rig.rt.preview_travel_plan(change)
    token = reviewed["review_token"]
    rig.clock.jump(1)
    rig.store.fail = True
    failed = rig.rt.save_travel_plan(change, token, "2" * 64)
    assert failed["status"] == "retry"
    assert rig.rt.list_travel_plans()["plans"] == []
    rig.store.fail = False
    saved = rig.rt.save_travel_plan(change, token, "2" * 64)
    assert saved["status"] == "saved"
    await rig.restart()
    assert rig.rt.save_travel_plan(change, token, "2" * 64) == saved
    assert rig.rt.save_travel_plan(change, token, "3" * 64)["status"] == "stale"
    assert len(rig.rt.list_travel_plans()["plans"]) == 1
    save(rig.rt, {"action": "undo", "undo_id": saved["undo_id"]}, "4" * 64)
    assert rig.rt.list_travel_plans()["plans"] == []


def one_day(outbound, back=None):
    return {
        "action": "create",
        "conflict_policy": "replace",
        "plan": {
            "origin": "AAA",
            "destination": "UT",
            "start_date": (DAY + timedelta(days=7)).isoformat(),
            "outbound": outbound,
            "return": back,
        },
    }


def connection(day, number, departure, arrival, *, back=False, cancelled=False):
    origin, destination = ("UT", "AAA") if back else ("AAA", "UT")
    return make_connection(
        [
            make_leg(
                origin, destination, departure, arrival, number=number, day=day, cancelled=cancelled
            )
        ]
    )


async def test_overnight_return_waits_for_the_user_to_leave(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    day = DAY + timedelta(days=8)
    saved = save(
        rig.rt,
        one_day(
            {"mode": "exact", "departure": "08:12"},
            {"mode": "on_leave", "day_offset": 1},
        ),
        "night",
    )
    rig.clock.t = at("00:15", day)
    rig.provider.world = [connection(day - timedelta(days=1), "8910", "00:30", "01:03", back=True)]
    rig.provider.calls.clear()
    rig.provider.disruption_calls = 0
    await rig.restart()
    key = f"{saved['plan_id']}|return|{day}"
    occ = rig.occ(key)
    assert occ is not None and occ.lock is None
    assert not any(q.origin == "UT" and q.destination == "AAA" for q in rig.provider.calls)
    assert rig.provider.disruption_calls == 0
    rig.write(name="renamed_office")
    assert await rig.rt.reload() is None
    assert rig.occ(key) == occ
    assert await rig.rt.leaving(saved["plan_id"], day, Direction.RETURN) is None
    await rig.rt.poll_once()
    assert rig.occ(key).lock.legs[0].train.train_number == "8910"


async def test_exact_cancellation_suggests_candidate_without_switching(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    day = DAY + timedelta(days=7)
    saved = save(
        rig.rt, one_day({"mode": "exact", "departure": "08:12", "train_number": "8801"}), "exact"
    )
    rig.clock.t = at("07:55", day)
    rig.provider.world = [
        connection(day, "8801", "08:12", "08:38", cancelled=True),
        connection(day, "8802", "08:25", "08:51"),
    ]
    await rig.rt.poll_once()
    assert rig.occ(f"{saved['plan_id']}|outbound|{day}").lock.legs[0].train.train_number == "8801"
    (alert,) = [a for a in rig.notifier.attempts if a.kind is EventKind.CANCELLED]
    assert "Next option: 08:25" in alert.text


async def test_replacement_uses_arrival_deadline_and_reports_switch(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    day = DAY + timedelta(days=7)
    saved = save(
        rig.rt,
        one_day({"mode": "window", "earliest": "08:10", "latest": "09:00", "arrive_by": "09:00"}),
        "deadline",
    )
    rig.clock.t = at("07:55", day)
    original = connection(day, "8801", "08:12", "08:38")
    fits = connection(day, "8802", "08:40", "08:58")
    late = connection(day, "8803", "08:25", "09:02")
    rig.provider.world = [original, late, fits]
    await rig.rt.poll_once()
    key = f"{saved['plan_id']}|outbound|{day}"
    assert rig.occ(key).lock.legs[0].train.train_number == "8801"
    rig.provider.world[0] = connection(day, "8801", "08:12", "08:38", cancelled=True)
    await rig.rt.poll_once()
    assert rig.occ(key).lock.legs[0].train.train_number == "8802"
    (alert,) = [a for a in rig.notifier.attempts if a.kind is EventKind.CANCELLED]
    assert "08:40" in alert.text


async def test_suppressed_usual_journey_is_not_probed_ahead(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    day = DAY + timedelta(days=1)
    change = one_day({"mode": "exact", "departure": "08:12"})
    change["plan"]["start_date"] = day.isoformat()
    save(rig.rt, change, "tomorrow")
    rig.provider.world = [usual_on(DAY + timedelta(days=offset)) for offset in range(1, 40)]
    rig.provider.calls.clear()
    await rig.rt.lookahead_once()
    assert rig.provider.calls
    assert not any(q.at.date() == day for q in rig.provider.calls)
    assert rig.rt.state.lookahead["p1|outbound"].last_run is not None


async def test_saving_a_travel_plan_stops_obsolete_lookahead_queries(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    profile = rig.rt.state.config.profiles[0]
    day = DAY + timedelta(days=1)
    change = one_day({"mode": "exact", "departure": "08:12"})
    change["plan"]["start_date"] = day.isoformat()

    def replace_on_first_trip(_query):
        rig.provider.hook = None
        save(rig.rt, change, "tomorrow")

    rig.provider.calls.clear()
    rig.provider.hook = replace_on_first_trip
    await rig.rt._lookahead_direction(profile, Direction.OUTBOUND, PlannedWorks((), "complete"))
    assert len(rig.provider.calls) == 1
    assert "p1|outbound" not in rig.rt.state.lookahead


async def test_changing_a_departed_plan_cannot_stop_its_monitor(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    day = DAY + timedelta(days=7)
    saved = save(rig.rt, one_day({"mode": "exact", "departure": "08:12"}), "departed")
    rig.clock.t = at("07:55", day)
    rig.provider.world = [connection(day, "8801", "08:12", "08:38")]
    await rig.rt.poll_once()
    rig.clock.t = at("08:21", day)
    edit = {"action": "cancel", "plan_id": saved["plan_id"]}
    review = rig.rt.preview_travel_plan(edit)
    assert review["status"] == "invalid"
    assert "actively being followed" in review["text"]
    assert rig.occ(f"{saved['plan_id']}|outbound|{day}").lock is not None


async def test_new_route_uses_selected_profile_settings_among_multiple_profiles(tmp_path):
    rig = Rig(tmp_path)
    text = PROFILE.replace('name: "NAME"', 'name: "office_a"').replace(
        "    transfer_min: {UT: 4}",
        "    accessibility: {require_accessible: true}\n    transfer_min: {UT: 4}",
    )
    rig.write(text=text)
    await rig.rt.start()
    day = DAY + timedelta(days=7)
    change = one_day({"mode": "exact", "departure": "08:12"})
    change["plan"]["profile_id"] = "p1"
    save(rig.rt, change, "access")
    rig.clock.t = at("07:55", day)
    rig.provider.calls.clear()
    rig.provider.world = [connection(day, "8801", "08:12", "08:38")]
    await rig.rt.poll_once()
    travel_calls = [q for q in rig.provider.calls if q.origin == "AAA" and q.destination == "UT"]
    assert travel_calls and all(q.require_accessible for q in travel_calls)


async def test_same_day_return_must_be_possible_after_outbound(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    change = one_day(
        {"mode": "exact", "departure": "08:40"},
        {"mode": "exact", "departure": "07:30"},
    )
    review = rig.rt.preview_travel_plan(change)
    assert review["status"] == "invalid"
    assert "after the outbound" in review["text"]
    change["plan"]["return"]["day_offset"] = 1
    assert rig.rt.preview_travel_plan(change)["status"] == "ready"


async def test_late_alternative_requires_a_choice_and_can_be_followed(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    day = DAY + timedelta(days=7)
    saved = save(
        rig.rt,
        one_day({"mode": "window", "earliest": "08:10", "latest": "09:00", "arrive_by": "09:00"}),
        "late-option",
    )
    pid = saved["plan_id"]
    key = f"{pid}|outbound|{day}"
    rig.clock.t = at("07:55", day)
    original = connection(day, "8801", "08:12", "08:38")
    late = connection(day, "8802", "09:01", "09:21")
    rig.provider.world = [original, late]
    await rig.rt.poll_once()
    rig.provider.world[0] = connection(day, "8801", "08:12", "08:38", cancelled=True)
    await rig.rt.poll_once()
    assert rig.occ(key).lock.legs[0].train.train_number == "8801"
    (alert,) = [a for a in rig.notifier.attempts if a.kind is EventKind.CANCELLED]
    assert "09:01" in alert.text and "09:21" in alert.text
    (view,) = [o for o in rig.rt.view().occurrences if o.key.profile_id == pid]
    assert view.next_good == late
    assert await rig.rt.choose_at(pid, time(9, 1), "8802", day, Direction.OUTBOUND) is None
    assert rig.occ(key).lock.legs[0].train.train_number == "8802"


async def test_expired_one_off_is_refused_unless_a_return_window_remains(tmp_path):
    rig = Rig(tmp_path, now="23:00")
    await rig.rt.start()
    change = one_day({"mode": "exact", "departure": "08:12"})
    change["plan"]["start_date"] = DAY.isoformat()
    review = rig.rt.preview_travel_plan(change)
    assert review["status"] == "invalid"
    assert "no remaining travel" in review["text"]
    change["plan"]["return"] = {"mode": "on_leave", "day_offset": 1}
    assert rig.rt.preview_travel_plan(change)["status"] == "ready"


async def test_edit_cannot_report_saved_when_same_day_journey_is_already_skipped(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    day = DAY + timedelta(days=7)
    draft = one_day({"mode": "exact", "departure": "08:12"})
    saved = save(rig.rt, draft, "before-skip")
    pid = saved["plan_id"]
    rig.clock.t = at("07:55", day)
    await rig.rt.poll_once()
    assert await rig.rt.not_travelling(pid, day, Direction.OUTBOUND) is None
    draft.update(action="edit", plan_id=pid)
    draft["plan"]["outbound"]["departure"] = "10:30"
    reviewed = rig.rt.preview_travel_plan(draft)
    assert reviewed["status"] == "invalid"
    assert "already completed or skipped" in reviewed["text"]


async def test_leaving_does_not_override_the_exact_train(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    day = DAY + timedelta(days=7)
    saved = save(
        rig.rt,
        one_day({"mode": "exact", "departure": "08:40", "train_number": "8801"}),
        "exact-leave",
    )
    pid = saved["plan_id"]
    key = f"{pid}|outbound|{day}"
    rig.clock.t = at("08:19", day)
    rig.provider.world = [
        connection(day, "8800", "08:30", "09:00"),
        connection(day, "8801", "08:40", "09:09"),
    ]
    await rig.rt.poll_once()
    assert rig.occ(key).lock.legs[0].train.train_number == "8801"
    await rig.rt.report_location(pid, LocationEvent("home", "leave", rig.clock.t))
    assert rig.occ(key).lock.legs[0].train.train_number == "8801"
