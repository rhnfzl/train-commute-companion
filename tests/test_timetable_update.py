from dataclasses import replace
from datetime import time

from lookahead_fixtures import MON, NEXT_TUE, OUT, P, confirmed

from train_commute_companion.model import Config, direction_key
from train_commute_companion.revisions import migrate_baselines
from train_commute_companion.serde import decode, encode
from train_commute_companion.state import BaselineHistory
from train_commute_companion.store import PersistedState
from train_commute_companion.timetable_update import offered_edit, resolve_edit, timetable_edit

RECIPIENT = "ha-mobile:alice"


def history(scope=None):
    return BaselineHistory(
        (
            replace(confirmed(), commute_weekday=scope),
            replace(
                confirmed(2, NEXT_TUE, l1=("08:18", "08:44")),
                commute_weekday=scope,
                update_nonce="fixture-secret",
            ),
        )
    )


def state():
    config = Config(1, (P,))
    return PersistedState(
        config, {1: config}, {direction_key(P.id, OUT): history()}, {}, {}, (), False
    )


def test_update_action_is_bound_to_adoption_and_recipient_across_reload():
    saved = state()
    edit, token = offered_edit(saved, P.id, OUT, 2, RECIPIENT)
    assert edit.field == "outbound.first_train"
    assert edit.yaml_hint.splitlines()[0] == (
        'From 2026-11-03, set outbound.first_train to "08:18" in the profiles.yaml entry '
        'with id "p1".'
    )
    assert resolve_edit(decode(PersistedState, encode(saved)), token, RECIPIENT) == edit
    assert resolve_edit(saved, token, "ha-mobile:bob") is None
    assert resolve_edit(saved, token, "email:alice") is None
    assert resolve_edit(saved, "ë" * 64, RECIPIENT) is None
    assert offered_edit(saved, P.id, OUT, 2, "telegram:alice") == (edit, None)
    changed = replace(saved.config, revision=2)
    assert resolve_edit(replace(saved, config=changed), token, RECIPIENT) is None
    off = replace(P, alerts=replace(P.alerts, timetable_update=False))
    assert resolve_edit(replace(saved, config=Config(1, (off,))), token, RECIPIENT) is None
    replaced = replace(history().versions[-1], version=3, update_nonce="another-secret")
    superseded = replace(
        saved,
        baselines={direction_key(P.id, OUT): BaselineHistory((*history().versions, replaced))},
    )
    assert resolve_edit(superseded, token, RECIPIENT) is None


def test_yaml_hint_includes_shifted_explicit_window():
    profile = replace(P, outbound=replace(P.outbound, window=(time(7, 47), time(10, 12))))
    adopted = BaselineHistory((confirmed(2, NEXT_TUE, l1=("08:06", "08:32")),))
    edit = timetable_edit(profile, OUT, adopted, 2)
    assert 'set outbound.first_train to "08:06"' in edit.yaml_hint
    assert 'If outbound.window is set, change it to ["07:41", "10:06"].' in edit.yaml_hint


def test_weekday_edit_keeps_old_trains_until_the_effective_date():
    profile = replace(P, outbound=replace(P.outbound, first_train_by_weekday={"tue": time(8, 12)}))
    old = history(2)
    edit = timetable_edit(profile, OUT, old, 2)
    assert edit.field == "outbound.first_train_by_weekday.tue"
    new = replace(
        profile, outbound=replace(profile.outbound, first_train_by_weekday={"tue": edit.departure})
    )
    kept = migrate_baselines(profile, new, OUT, old, MON)
    assert kept is old
    assert kept.in_force(MON, 2).signature.legs[0].departure == time(8, 12)
    assert kept.in_force(NEXT_TUE, 2).signature.legs[0].departure == time(8, 18)
    ordinary_edit = replace(
        new, outbound=replace(new.outbound, first_train_by_weekday={"tue": time(9)})
    )
    assert not migrate_baselines(profile, ordinary_edit, OUT, old, MON).versions
