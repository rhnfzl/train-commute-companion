"""The exact stored form of alert keys and their ledger slots: a state file written by an earlier
version must still match them, so a known fact is never news again (7.5)."""

import json
from dataclasses import replace

from builders import make_leg
from test_lifecycle_follow import (
    DIRECT,
    L1,
    L1X,
    L2,
    L2X,
    LATER,
    R2,
    RIDE,
    ans,
    at,
    conn,
    ctx,
    journey,
    occ,
    poll,
    with_track,
)

from train_commute_companion.lifecycle import legacy_facts
from train_commute_companion.model import AlertEvent, EventKind, Leg, Severity, train_ident
from train_commute_companion.revalidation import _states_current
from train_commute_companion.serde import dumps, loads

DAY = "2026-10-27"


def _delayed_leg(delay: int) -> Leg:
    return make_leg("AAA", "RTD", "08:12", "09:24", delay_dep=delay, delay_arr=delay)


def test_a_delay_key_and_its_slots():
    leg = _delayed_leg(7)
    base = f"delayed:{DAY}:{train_ident(leg.train)}:08:12 AAA to RTD"
    c = ctx(at("07:40"), DIRECT)
    t = poll(occ(journey([_delayed_leg(0)])), c, ans("AAA", conn(leg)))
    assert [e.key for e in t.events] == [f"{base}|stated+7,7"]
    assert t.state.alert_keys[f"{base}#d"] == "7" and t.state.alert_keys[f"{base}#a"] == "7"
    t = poll(t.state, c, ans("AAA", conn(_delayed_leg(0))))
    assert [e.key for e in t.events] == [
        f"back_to_normal{base[len('delayed') :]}|stated+0,0",
    ]
    assert t.state.alert_keys[f"{base}#d"] == "0"


def test_a_platform_key_and_its_slot():
    ident = train_ident(L1.train)
    head = f"platform_change:{DAY}:{ident}:track"
    t = poll(
        occ(journey([L1, L2])), ctx(at("07:40")), ans("AAA", conn(with_track(L1, "1", "5"), L2))
    )
    (e,) = [e for e in t.events if e.kind is EventKind.PLATFORM_CHANGE]
    assert e.key == f"{head} 5"
    assert t.state.alert_keys[f"moved#{head}"] == "1"
    t = poll(t.state, ctx(at("07:42")), ans("AAA", conn(with_track(L1, "1", "1"), L2)))
    (e,) = [e for e in t.events if e.kind is EventKind.PLATFORM_CHANGE]
    assert e.key == f"{head} 1|planned"


def test_a_relock_key_names_its_replacement_and_a_retained_key_keeps_its_suffix():
    text = f"your 08:50 UT to RTD train is cancelled|{train_ident(R2.train)}"
    base = f"cancelled:{DAY}:{train_ident(L2.train)}:{text}"
    s = occ(journey([L1, L2], RIDE))
    t = poll(s, ctx(at("08:20")), ans("AAA", conn(L1, L2X)), ans("UT", conn(L2X), conn(R2)))
    assert [e.key for e in t.events] == [f"{base}|stated+0"]
    assert t.state.alert_keys[base] == "0"

    s = occ(journey(LATER.legs), retained=(journey([L1, L2], RIDE),))
    t = poll(s, ctx(at("08:20")), ans("AAA", conn(L1, L2X), LATER), ans("UT", conn(R2)))
    suffix = f":retained:{train_ident(L1.train)}"
    (e,) = t.events
    assert e.key == f"{base}{suffix}|stated+0"
    assert t.state.alert_keys[f"{base}{suffix}"] == "0"


def test_a_relock_from_the_origin_names_every_leg_of_the_replacement():
    named = ",".join(train_ident(x.train) for x in LATER.legs)
    t = poll(occ(journey([L1, L2])), ctx(at("07:50")), ans("AAA", conn(L1X, L2), LATER))
    text = f"your 08:12 AAA to UT train is cancelled|{named}"
    assert [e.key for e in t.events] == [f"cancelled:{DAY}:{train_ident(L1.train)}:{text}|stated+0"]


def test_a_retained_platform_key_keeps_its_suffix_after_the_track():
    moved = with_track(L2, "1", "5")
    s = occ(journey(LATER.legs), retained=(journey([L1, L2], RIDE),))
    t = poll(s, ctx(at("08:20")), ans("AAA", conn(L1, moved), LATER), ans("UT", conn(R2)))
    suffix = f":retained:{train_ident(L1.train)}"
    head = f"platform_change:{DAY}:{train_ident(L2.train)}:track"
    keys = [e.key for e in t.events if e.kind is EventKind.PLATFORM_CHANGE]
    assert f"{head} 5{suffix}" in keys
    assert t.state.alert_keys[f"moved#{head}{suffix}"] == "1"


def test_an_event_stored_before_facts_were_kept_still_loads():
    t = poll(
        occ(journey([_delayed_leg(0)])), ctx(at("07:40"), DIRECT), ans("AAA", conn(_delayed_leg(7)))
    )
    (e,) = t.events
    assert loads(AlertEvent, dumps(e)) == e and e.facts is not None and e.facts.told
    stored = json.loads(dumps(e))
    del stored["facts"]
    old = loads(AlertEvent, json.dumps(stored))
    assert old.facts is None and old.key == e.key
    # its stated delays are read from its key, so a stale one is still caught
    assert _states_current(old, t.state.alert_keys) and not _states_current(old, {})
    assert _states_current(e, t.state.alert_keys) and not _states_current(e, {})


def test_a_held_all_clear_stored_before_facts_were_kept_is_stale_once_late_again():
    c = ctx(at("07:40"), DIRECT)
    late = poll(occ(journey([_delayed_leg(0)])), c, ans("AAA", conn(_delayed_leg(7))))
    t = poll(late.state, c, ans("AAA", conn(_delayed_leg(0))))
    (back,) = [e for e in t.events if e.kind is EventKind.BACK_TO_NORMAL]
    old = replace(back, facts=None)
    assert _states_current(old, t.state.alert_keys)
    again = poll(t.state, c, ans("AAA", conn(_delayed_leg(7))))
    assert not _states_current(old, again.state.alert_keys)


def test_what_is_read_from_an_event_stored_before_facts_were_kept():
    named = f"{train_ident(R2.train)},{train_ident(L2.train)}"
    clause = " Next good connection: 08:58."
    e = AlertEvent(
        "p1", "office_a", None, None, None, EventKind.CANCELLED, Severity.LOUD, L1.train,
        f"Office A: your train is cancelled.{clause} (service date {DAY})",
        f"cancelled:{DAY}:x:your train is cancelled|{named}:retained:y|stated+0", at("07:40"),
    )  # fmt: skip
    f = legacy_facts(e)
    assert f.named == tuple(named.split(",")) and f.on and not f.told
    assert f.next_clause == clause and f.twin_note == f" (service date {DAY})"
    told = replace(e, kind=EventKind.DATA_UNCLEAR, key=f"data_unclear:{DAY}:x:abc123")
    assert legacy_facts(told).unclear == "abc123"


def test_a_recovery_opens_with_the_retained_prefix_and_keeps_a_colon_in_the_name():
    leg = _delayed_leg(7)
    c = ctx(at("08:30"), DIRECT)
    c = replace(c, profile=replace(c.profile, name="office: a"))
    s = occ(journey(LATER.legs), retained=(journey([_delayed_leg(0)], RIDE),))
    t = poll(s, c, ans("AAA", conn(leg), LATER))
    (late,) = [e for e in t.events if e.kind is EventKind.DELAYED]
    assert late.text.startswith("Office: A: if you are on the 08:12: your 08:12 AAA to RTD train")
    t = poll(t.state, c, ans("AAA", conn(_delayed_leg(0)), LATER))
    (back,) = [e for e in t.events if e.kind is EventKind.BACK_TO_NORMAL]
    assert back.text == (
        "Office: A: if you are on the 08:12: your 08:12 AAA to RTD train "
        "is back to normal, on time per NS."
    )
