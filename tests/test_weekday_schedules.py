from dataclasses import replace
from datetime import UTC, date, time, timedelta

import pytest
from lookahead_fixtures import P, confirmed

from train_commute_companion.model import Direction, Profile, UsualJourney
from train_commute_companion.profile import parse_profiles
from train_commute_companion.revisions import baseline_on
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.serde import decode, dumps, encode, loads
from train_commute_companion.state import BaselineHistory, BaselineVersion

OUT, RET = Direction.OUTBOUND, Direction.RETURN
TUE, WED = date(2026, 10, 27), date(2026, 10, 28)


def weekdays(p=P, **overrides):
    return replace(p, outbound=replace(p.outbound, first_train_by_weekday=overrides))


def test_dated_journey_moves_only_first_train_and_window():
    p = weekdays(tue=time(9, 12))
    actual = p.journey_on(OUT, TUE)
    assert actual.first_train == time(9, 12)
    assert actual.window == (time(8, 12), time(11, 12))
    assert actual.transfers == p.outbound.transfers and actual.buffer_min == p.outbound.buffer_min
    assert p.journey_on(OUT, WED).first_train == time(8, 12)
    assert p.outbound.first_train == time(8, 12)
    assert loads(Profile, dumps(p)) == p
    old = encode(p.outbound)
    old.pop("first_train_by_weekday")
    assert decode(UsualJourney, old).first_train_by_weekday == {}


def test_overnight_return_uses_original_commute_weekday_before_offset():
    p = replace(
        P,
        return_=replace(
            P.return_, day_offset=1, first_train_by_weekday={"tue": time(0, 20), "wed": time(1, 20)}
        ),
    )
    schedule = dated_schedule(p, RET, TUE, None)
    assert schedule.key.service_date == WED
    assert schedule.first_departure.time() == time(0, 20)
    assert schedule.window_start.date() == TUE
    assert schedule.window_start.time() == time(23, 20)
    assert p.paused_on(WED) is False
    paused = replace(p, pauses=((WED, WED),))
    assert paused.paused_on(schedule.key.service_date)


@pytest.mark.parametrize("value", [None, [], {"Tuesday": "09:12"}, {"tue": 912}, {"tue": "25:00"}])
def test_invalid_weekday_override_is_refused(value):
    import json

    from lookahead_fixtures import YAML

    text = YAML.replace(
        'first_train: "08:12",',
        'first_train: "08:12", first_train_by_weekday: ' + json.dumps(value) + ",",
    )
    profiles, errors = parse_profiles(text, None)
    assert not profiles and any("first_train_by_weekday" in e.path for e in errors)


def test_baseline_scope_never_uses_another_weekday_or_legacy_global():
    p = weekdays(tue=time(9, 12))
    monday = replace(confirmed(2), commute_weekday=1)
    tuesday = replace(confirmed(3), commute_weekday=2)
    history = BaselineHistory((confirmed(), monday, tuesday))
    assert baseline_on(history, p, OUT, TUE) == tuesday
    assert baseline_on(history, p, OUT, WED) is None
    assert history.in_force(TUE) == history.versions[0]
    assert history.in_force(TUE, 2) == tuesday
    raw = encode(confirmed())
    raw.pop("commute_weekday")
    assert decode(BaselineVersion, raw).commute_weekday is None


def test_overrides_shift_windows_across_midnight_and_use_dst_policy():
    p = weekdays(sun=time(2, 20), tue=time(0, 20))
    midnight = dated_schedule(p, OUT, TUE, None)
    assert midnight.window_start.date() == TUE - timedelta(days=1)
    spring = dated_schedule(p, OUT, date(2026, 3, 29), None)
    fall = dated_schedule(p, OUT, date(2026, 10, 25), None)
    assert spring.first_departure.time() == time(3, 0)
    assert fall.first_departure.fold == 0
    assert spring.window_start.astimezone(UTC) < spring.window_end.astimezone(UTC)


def test_cold_probe_uses_weekday_departure_without_shifting_unknown_onward_legs():
    from train_commute_companion.lookahead_probe import probe_queries

    p = weekdays(tue=time(9, 12))
    queries = probe_queries(p, OUT, TUE, None)
    assert queries and all(q.at.time() == time(8, 57) for q in queries)


def test_migration_copies_known_global_signatures_only_for_unchanged_weekdays():
    from train_commute_companion.lookahead import rebase_on_edit
    from train_commute_companion.revisions import migrate_baselines

    old = P
    new = weekdays(tue=time(9, 12))
    base = confirmed(l1=("08:20", "08:48"), l2=("08:54", "09:32"))
    future = confirmed(2, eff=date(2026, 11, 3), l1=("08:30", "08:58"), l2=("09:04", "09:42"))
    history = BaselineHistory((base, future))
    migrated = rebase_on_edit(old, new, OUT, history, date(2026, 10, 26))
    assert migrated == migrate_baselines(old, new, OUT, history, date(2026, 10, 26))
    assert baseline_on(migrated, new, OUT, TUE) is None
    wednesday = baseline_on(migrated, new, OUT, WED)
    assert wednesday.signature == base.signature
    assert wednesday.version > history.latest and wednesday.commute_weekday == 3
    assert len({v.version for v in migrated.versions}) == len(migrated.versions)
    assert all(v.commute_weekday is not None for v in migrated.versions)
    assert baseline_on(migrated, new, OUT, date(2026, 11, 4)).signature == future.signature


def test_edit_keeps_unaffected_scopes_and_removal_never_promotes_one_weekday():
    from train_commute_companion.lookahead import rebase_on_edit

    old = weekdays(tue=time(9, 12))
    history = BaselineHistory(
        (replace(confirmed(3), commute_weekday=3), replace(confirmed(9), commute_weekday=2))
    )
    changed = weekdays(tue=time(9, 42))
    migrated = rebase_on_edit(old, changed, OUT, history, TUE)
    assert migrated.versions == (history.versions[0],) and migrated.floor == 9
    removed = rebase_on_edit(old, P, OUT, history, TUE)
    assert removed.versions == () and removed._next() > 9


def test_learning_and_confirmation_are_independent_for_each_commute_weekday():
    from lookahead_fixtures import MON, NEXT_TUE, NOW, ledger, probe

    from train_commute_companion.lookahead import run_lookahead

    p = weekdays(tue=time(9, 12))
    results = [
        *probe(TUE, p=p, l1=("09:12", "09:40"), l2=("09:46", "10:24")),
        *probe(WED, p=p),
        *probe(NEXT_TUE, p=p, l1=("09:12", "09:40"), l2=("09:46", "10:24")),
    ]
    history = BaselineHistory()
    out = run_lookahead(
        p,
        OUT,
        history,
        ledger(history),
        results,
        revision=1,
        today=MON,
        now=NOW,
        others=(),
        bases={},
    )
    assert out is not None
    history, _, _ = out
    tue, wed = baseline_on(history, p, OUT, TUE), baseline_on(history, p, OUT, WED)
    assert tue is not None and tue.confirmed and tue.signature.legs[0].departure == time(9, 12)
    assert wed is not None and not wed.confirmed and wed.signature.legs[0].departure == time(8, 12)
    assert len({v.version for v in history.versions}) == 2


def test_weekday_adoption_changes_only_that_scope_without_synthesizing_signature():
    from lookahead_fixtures import MON, NEXT_TUE, NOW, ledger, probe

    from train_commute_companion.lookahead import run_lookahead

    p = weekdays(tue=time(9, 12))
    tue = replace(confirmed(1, l1=("09:12", "09:40"), l2=("09:46", "10:24")), commute_weekday=2)
    wed = replace(confirmed(2), commute_weekday=3)
    history = BaselineHistory((tue, wed))
    results = [
        *probe(TUE, tue, p=p, l1=("09:20", "09:48"), l2=("09:54", "10:32")),
        *probe(WED, wed, p=p),
        *probe(NEXT_TUE, tue, p=p, l1=("09:20", "09:48"), l2=("09:54", "10:32")),
    ]
    out = run_lookahead(
        p,
        OUT,
        history,
        ledger(history),
        results,
        revision=1,
        today=MON,
        now=NOW,
        others=(),
        bases={},
    )
    assert out is not None
    history, _, events = out
    adopted = baseline_on(history, p, OUT, TUE)
    assert adopted.version == 3 and adopted.commute_weekday == 2
    assert adopted.signature.legs[1].departure == time(9, 54)
    assert baseline_on(history, p, OUT, WED) == wed
    assert len([e for e in events if e.kind.value == "timetable_adopted"]) == 1


def test_scoped_provisional_retains_its_seed_date_when_other_weekday_changes():
    from lookahead_fixtures import MON, NOW, ledger, probe

    from train_commute_companion.lookahead import rebase_on_edit, reset_on_edit, run_lookahead
    from train_commute_companion.lookahead_probe import day_key, seed_mark

    p = weekdays(tue=time(9, 12))
    provisional = replace(
        confirmed(9, l1=("09:12", "09:40"), l2=("09:46", "10:24")),
        confirmed=False,
        commute_weekday=2,
    )
    history = BaselineHistory((provisional, replace(confirmed(10), commute_weekday=3)))
    old_ledger = ledger(
        history,
        per_date={day_key(TUE, None): (seed_mark(9),), day_key(WED, None): ("old-wed-mark",)},
    )
    changed = weekdays(tue=time(9, 12), wed=time(7, 42))
    reset = reset_on_edit(p, changed, OUT, history, old_ledger, MON)
    assert reset.per_date == {day_key(TUE, None): (seed_mark(9),)}
    migrated = rebase_on_edit(p, changed, OUT, history, MON)
    reset = replace(reset, started_baseline=migrated.latest)
    results = probe(TUE, provisional, p=changed, l1=("09:12", "09:40"), l2=("09:46", "10:24"))
    out = run_lookahead(
        changed, OUT, migrated, reset, results, revision=1, today=MON, now=NOW, others=(), bases={}
    )
    assert out is not None and not baseline_on(out[0], changed, OUT, TUE).confirmed
    assert out[0]._next() > 10


def test_disabling_overrides_then_reseeding_never_reuses_retired_scope_ids():
    from lookahead_fixtures import MON, NOW, ledger, probe

    from train_commute_companion.lookahead import rebase_on_edit, run_lookahead

    p = weekdays(tue=time(9, 12))
    history = BaselineHistory((replace(confirmed(23), commute_weekday=2),))
    uniform = rebase_on_edit(p, P, OUT, history, MON)
    assert not uniform.versions and uniform.floor == 23
    out = run_lookahead(
        P,
        OUT,
        uniform,
        ledger(uniform),
        probe(TUE),
        revision=1,
        today=MON,
        now=NOW,
        others=(),
        bases={},
    )
    assert out is not None and out[0].versions[0].version == 24
    assert out[0].versions[0].commute_weekday is None


def test_extra_date_uses_its_weekday_and_actual_service_date_pause():
    from lifecycle_fixtures import at

    from train_commute_companion.model import Config
    from train_commute_companion.schedule import schedules_active

    saturday = date(2026, 10, 31)
    p = replace(weekdays(sat=time(10, 12)), extra_dates=frozenset({saturday}))
    now = at("09:30").replace(year=2026, month=10, day=31)
    active = schedules_active(Config(1, (p,)), {}, now)
    assert len(active) == 1 and active[0].first_departure.time() == time(10, 12)
    paused = replace(p, pauses=((saturday, saturday),))
    assert schedules_active(Config(1, (paused,)), {}, now) == []


def test_shifted_weekday_window_collisions_and_dst_warning_are_validated():
    from lookahead_fixtures import YAML

    from train_commute_companion.profile import check_lines, load_config

    overlapping = YAML.replace(
        'first_train: "08:12",', 'first_train: "08:12", first_train_by_weekday: {tue: "17:12"},'
    )
    config, errors = load_config(overlapping, None, None, date(2026, 10, 26))
    assert config is None and any("overlapping" in e.message for e in errors)
    dst = YAML.replace(
        'first_train: "08:12",', 'first_train: "08:12", first_train_by_weekday: {sun: "02:20"},'
    )
    config, lines = check_lines(dst, None, date(2026, 10, 26))
    assert config is not None and any("first_train_by_weekday.sun" in line for line in lines)


def test_return_scope_uses_commute_weekday_and_does_not_match_service_weekday():
    p = replace(
        P, return_=replace(P.return_, day_offset=1, first_train_by_weekday={"tue": time(0, 20)})
    )
    tue = replace(confirmed(2), commute_weekday=2)
    wed = replace(confirmed(3), commute_weekday=3)
    assert baseline_on(BaselineHistory((tue, wed)), p, RET, WED) == tue


def test_moved_repairs_compare_each_weekday_against_its_own_configured_time():
    from train_commute_companion.view import moved

    p = weekdays(tue=time(9, 12))
    tue = replace(confirmed(1, l1=("09:20", "09:48"), l2=("09:54", "10:32")), commute_weekday=2)
    wed = replace(confirmed(2, l1=("08:20", "08:48"), l2=("08:54", "09:32")), commute_weekday=3)
    (repair,) = moved((p,), {"p1|outbound": BaselineHistory((tue, wed))}, today=TUE)
    assert "Tuesday: NS moved your 09:12 to 09:20" in repair.text
    assert "Wednesday: NS moved your 08:12 to 08:20" in repair.text


@pytest.mark.parametrize("retained", [False, True])
async def test_running_or_retained_journey_keeps_its_full_signature_across_weekday_enable(
    tmp_path, retained
):
    import runtime_seam
    from runtime_rig import PROFILE, USUAL, Rig

    from train_commute_companion.lookahead_probe import signature_of
    from train_commute_companion.model import Config
    from train_commute_companion.revisions import occurrence_ctx
    from train_commute_companion.store import PersistedState

    rig = Rig(tmp_path)
    profiles, errors = parse_profiles(PROFILE.replace("NAME", "office_a"), None)
    assert not errors
    config = Config(1, tuple(profiles))
    signature = signature_of(USUAL, TUE)
    hist = BaselineHistory((BaselineVersion(1, signature, date(2026, 10, 1), True),))
    rig.store.save(PersistedState(config, {1: config}, {"p1|outbound": hist}, {}, {}, (), False))
    await rig.rt.start()
    await rig.rt.poll_once()
    original = rig.occ()
    assert original.lock and original.baseline_signature == signature
    if retained:
        original = replace(original, retained=(original.lock,), lock=None)
        runtime_seam.seed_state(
            rig.rt, occurrences={**rig.rt.state.occurrences, "p1|outbound|2026-10-27": original}
        )
    text = PROFILE.replace("NAME", "office_a").replace(
        'first_train: "08:12",', 'first_train: "08:12", first_train_by_weekday: {tue: "09:12"},'
    )
    rig.write(text=text)
    await rig.rt.reload()
    current = rig.occ()
    context = occurrence_ctx(rig.rt.state, current, rig.clock.now())
    assert current.route_revision == 1 and current.baseline_signature == signature
    assert current.lock == original.lock and current.retained == original.retained
    assert context.signature == signature and context.schedule.first_departure.time() == time(8, 12)
    assert (
        baseline_on(rig.rt.state.baselines["p1|outbound"], rig.rt.config.profiles[0], OUT, TUE)
        is None
    )
    assert all(v.version > 1 for v in rig.rt.state.baselines["p1|outbound"].versions)


def test_waiting_adoption_cancellation_does_not_retire_another_weekday():
    from lookahead_fixtures import MON, NEXT_TUE, NOW, ledger, probe

    from train_commute_companion.lookahead import run_lookahead
    from train_commute_companion.model import AlertEvent, EventKind, Obligation, Severity

    p = weekdays(tue=time(9, 12))
    tue = replace(confirmed(1, l1=("09:12", "09:40"), l2=("09:46", "10:24")), commute_weekday=2)
    wed = replace(confirmed(2), commute_weekday=3)
    waiting_tue = replace(tue, version=3, effective_from=NEXT_TUE)
    waiting_wed = replace(wed, version=4, effective_from=NEXT_TUE + timedelta(days=1))
    history = BaselineHistory((tue, wed, waiting_tue, waiting_wed))

    def pending(v):
        key = f"adopt:{v.version}"
        event = AlertEvent(
            p.id,
            p.name,
            OUT,
            v.effective_from,
            None,
            EventKind.TIMETABLE_ADOPTED,
            Severity.SILENT,
            None,
            "adoption",
            key,
            NOW,
        )
        return Obligation(
            key, event, None, (p.id, OUT, v.effective_from), 1, v.version, key, None, 0
        )

    pending_tue, pending_wed = pending(waiting_tue), pending(waiting_wed)
    results = [
        *probe(TUE, tue, p=p, l1=("09:12", "09:40"), l2=("09:46", "10:24")),
        *probe(WED, wed, p=p, cancel2=True),
        *probe(NEXT_TUE, tue, p=p, l1=("09:12", "09:40"), l2=("09:46", "10:24")),
    ]
    out = run_lookahead(
        p,
        OUT,
        history,
        ledger(history, pending=(pending_tue, pending_wed)),
        results,
        revision=1,
        today=MON,
        now=NOW,
        others=(),
        bases={},
    )
    assert out is not None
    after, owed, _ = out
    assert waiting_tue not in after.versions and waiting_wed in after.versions
    assert pending_tue not in owed.pending and pending_wed in owed.pending
    assert after._next() == 5


@pytest.mark.parametrize("stale", ["revision", "baseline"])
def test_old_probe_result_is_rejected_after_weekday_migration(stale):
    from lookahead_fixtures import MON, NOW, probe

    from train_commute_companion.ledger import LiveLedger, PollMemory
    from train_commute_companion.lookahead_run import ProbedRun, apply_run
    from train_commute_companion.model import Config
    from train_commute_companion.revisions import migrate_baselines
    from train_commute_companion.store import PersistedState
    from train_commute_companion.webhook_queue import CopyQueue

    changed = weekdays(tue=time(9, 12))
    old = BaselineHistory((confirmed(),))
    new = migrate_baselines(P, changed, OUT, old, MON)
    config = Config(2, (changed,))
    state = PersistedState(
        config, {1: Config(1, (P,)), 2: config}, {"p1|outbound": new}, {}, {}, (), False
    )
    run = ProbedRun(
        NOW,
        1 if stale == "revision" else 2,
        new.latest if stale == "revision" else old.latest,
        tuple(probe(TUE, old.versions[0])),
    )
    queue = CopyQueue(
        None,
        lambda: NOW,
        lambda accepted: None,
        lambda o: False,
        lambda held: LiveLedger(lambda: state, lambda: state, PollMemory(), lambda: NOW),
    )
    assert apply_run(state, queue, P, OUT, run) is None
    assert queue.copies == {} and state.baselines["p1|outbound"] == new


async def test_changed_weekday_occurrence_is_cold_and_unchanged_day_keeps_full_adoption(tmp_path):
    from runtime_rig import PROFILE, Rig, at

    from train_commute_companion.model import Config
    from train_commute_companion.revisions import occurrence_ctx
    from train_commute_companion.store import PersistedState

    rig = Rig(tmp_path, "06:00")
    profiles, errors = parse_profiles(PROFILE.replace("NAME", "office_a"), None)
    assert not errors
    config = Config(1, tuple(profiles))
    adopted = confirmed(l1=("08:20", "08:48"), l2=("08:54", "09:32"))
    rig.store.save(
        PersistedState(
            config, {1: config}, {"p1|outbound": BaselineHistory((adopted,))}, {}, {}, (), False
        )
    )
    await rig.rt.start()
    rig.write(
        text=PROFILE.replace("NAME", "office_a").replace(
            'first_train: "08:12",', 'first_train: "08:12", first_train_by_weekday: {tue: "09:12"},'
        )
    )
    await rig.rt.reload()
    rig.clock.set("08:12")
    await rig.rt.poll_once()
    changed = rig.occ()
    context = occurrence_ctx(rig.rt.state, changed, rig.clock.now())
    assert changed.baseline_signature is None and changed.baseline_version is None
    assert context.signature is None and context.schedule.first_departure.time() == time(9, 12)
    rig.clock.t = at("07:21", WED)
    await rig.rt.poll_once()
    unchanged = rig.occ("p1|outbound|2026-10-28")
    assert unchanged.baseline_signature == adopted.signature
    context = occurrence_ctx(rig.rt.state, unchanged, rig.clock.now())
    assert context.schedule.first_departure.time() == time(8, 20)
    assert context.signature.legs[1].departure == time(8, 54)


def test_scoped_probe_dates_keep_three_date_ten_day_budget_per_actual_baseline():
    from lookahead_fixtures import MON, NEXT_TUE

    from train_commute_companion.lookahead_probe import probe_dates, probe_queries
    from train_commute_companion.model import Signature

    p = weekdays(tue=time(9, 12))
    base = replace(confirmed(), commute_weekday=2)
    base = replace(
        base,
        signature=Signature(
            (base.signature.legs[0], replace(base.signature.legs[1], day_offset=9))
        ),
    )
    history = BaselineHistory((base,))
    dates = probe_dates(p, MON, direction=OUT, histories={OUT: history})
    assert dates == [TUE, WED, NEXT_TUE + timedelta(days=1)]
    for commute_day in dates:
        selected = baseline_on(history, p, OUT, commute_day)
        assert all(
            (query.at.date() - MON).days <= 10
            for query in probe_queries(p, OUT, commute_day, selected)
        )


def test_global_migration_preserves_version_tie_breaking_even_if_store_order_differs():
    from train_commute_companion.revisions import migrate_baselines

    older = confirmed(2)
    newer = confirmed(7, l1=("08:20", "08:48"), l2=("08:54", "09:32"))
    history = BaselineHistory((newer, older))
    p = weekdays(tue=time(9, 12))
    migrated = migrate_baselines(P, p, OUT, history, TUE)
    assert baseline_on(migrated, p, OUT, WED).signature == newer.signature
    assert migrated.floor >= 7 and migrated._next() > max(v.version for v in migrated.versions)
