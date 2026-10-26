from dataclasses import replace
from datetime import date, time, timedelta

from lookahead_fixtures import (
    DAYS,
    MON,
    NEXT_TUE,
    NOW,
    OUT,
    TUE,
    WED,
    P,
    confirmed,
    first_train,
    ledger,
    normal_signature,
    probe,
    profile,
    run,
)

from train_commute_companion.lookahead import (
    BaselineHistory,
    BaselineVersion,
    probe_queries,
    rebase_on_edit,
    run_lookahead,
)


def test_in_force_picks_latest_effective_version_and_waits_for_future_ones():
    v1 = confirmed(1, date(2026, 10, 1))
    v2 = replace(confirmed(2, NEXT_TUE, l1=("08:15", "08:43")), confirmed=True)
    h = BaselineHistory((v1, v2))
    assert h.in_force(date(2026, 9, 30)) is None
    assert h.in_force(MON) is v1 and h.in_force(NEXT_TUE) is v2
    assert h.latest == 2


def test_shorter_clean_version_before_confirmation_is_silent():
    long_sig = normal_signature(l2=("08:50", "09:35"))
    h = BaselineHistory((BaselineVersion(1, long_sig, MON, False),))
    led = ledger(h, per_date={f"{TUE.isoformat()}|j": ("seed:1",)})
    h2, _, events = run(h, probe(WED, h.in_force(WED)), led=led)
    assert [v.version for v in h2.versions] == [1, 2]
    assert not h2.versions[-1].confirmed and events == []
    assert h2.in_force(WED).signature == normal_signature(WED)  # type: ignore[union-attr]


def test_longer_clean_probe_does_not_replace_provisional():
    h = BaselineHistory((BaselineVersion(1, normal_signature(), MON, False),))
    led = ledger(h, per_date={f"{TUE.isoformat()}|j": ("seed:1",)})
    h2, _, _ = run(h, probe(WED, h.in_force(WED), l2=("08:50", "09:35")), led=led)
    assert h2 == h


def test_disrupted_day_cannot_move_the_first_train():
    # not anchored on the profile's 08:12: no seed
    rs = probe(TUE, l1=("08:15", "08:43"), l2=("08:49", "09:27"))
    assert not any(r.clean for r in rs if r.leg_index is None)
    h, _, _ = run(BaselineHistory(), rs)
    assert h.versions == ()


def test_baseline_change_since_start_discards_the_run():
    h = BaselineHistory((confirmed(),))
    led = ledger(BaselineHistory())  # started before any baseline existed
    assert run_lookahead(P, OUT, h, led, probe(TUE, h.in_force(TUE)), 1, MON, NOW) is None


def test_older_overlapping_run_is_rejected():
    h = BaselineHistory()
    led = ledger(h, last_run=NOW + timedelta(minutes=5))
    assert run_lookahead(P, OUT, h, led, probe(TUE), 1, MON, NOW) is None


def test_first_train_set_to_baseline_keeps_it_other_route_edits_reseed():
    h = BaselineHistory((confirmed(), confirmed(2, TUE, l1=("08:15", "08:43"))))
    to_base = profile(first_train=time(8, 15))
    assert rebase_on_edit(P, to_base, OUT, h, WED) is h
    moved = profile(first_train=time(8, 20))
    reseeded = rebase_on_edit(P, moved, OUT, h, WED)
    assert reseeded.versions == () and reseeded.floor == 2
    via = profile(transfers=("BBB",))
    assert rebase_on_edit(P, via, OUT, h, WED).versions == ()
    # numbers never repeat after a re-seed
    h3, _, _ = run(reseeded, probe(TUE), led=ledger(reseeded))
    assert h3.versions[0].version == 3


def test_queries_one_per_leg_plus_whole_around_baseline_departure():
    base = confirmed(l1=("08:15", "08:43"), l2=("08:49", "09:27"))
    qs = probe_queries(P, OUT, TUE, base)
    assert [(q.origin, q.destination) for q in qs] == [
        ("AAA", "UT"),
        ("UT", "RTD"),
        ("AAA", "RTD"),
    ]
    assert [q.at.strftime("%H:%M") for q in qs] == ["08:00", "08:34", "08:00"]
    assert all(q.via is None for q in qs)
    direct = profile(transfers=())
    assert len(probe_queries(direct, OUT, TUE, None)) == 1


def test_adoption_changing_service_date_is_refused():
    p = profile(first_train=time(0, 5), window=(time(23, 5), time(2, 5)))
    early = {"l1": ("23:58", "00:20"), "l2": ("00:30", "01:00"), "night": True}
    base_kw = {"l1": ("00:05", "00:27"), "l2": ("00:35", "01:05"), "night": True}
    h = BaselineHistory((BaselineVersion(1, normal_signature(**base_kw), date(2026, 10, 1), True),))
    rs = [r for d in DAYS for r in probe(d, h.in_force(d), p=p, **early)]
    h2, _, events = run(h, rs, p=p)
    assert h2 == h
    assert any("on another day" in e.text for e in events)
    assert first_train(h2, NEXT_TUE) == time(0, 5)
