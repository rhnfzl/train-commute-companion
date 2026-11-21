"""Keep timetable correction receipts bound to the adoption and recipient that produced them."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date

from train_commute_companion.disruptions import LEGACY_PRIMARY
from train_commute_companion.lookahead_probe import adopt_key, bundle_names, cancel_key, day_key
from train_commute_companion.model import Obligation, direction_key, split_direction_key
from train_commute_companion.revisions import find_profile
from train_commute_companion.state import BaselineHistory, LookAheadLedger
from train_commute_companion.store import PersistedState


def adoption_correction(o: Obligation) -> str | None:
    version = o.baseline_version
    if o.look_ahead is None or version is None or not bundle_names(o.fact_key, adopt_key(version)):
        return None
    return cancel_key(version)


def correction_keys(o: Obligation) -> tuple[str, ...]:
    if o.look_ahead is None:
        return ()
    return tuple(
        k
        for k in o.fact_key.split(";")
        if k.startswith("cancel:") and k[7:].isascii() and k[7:].isdigit()
    )


def merge_adoption_receipts(
    ledger: LookAheadLedger, other: LookAheadLedger
) -> dict[str, frozenset[str]]:
    return {
        key: destinations | other.adoption_receipts.get(key, frozenset())
        if ledger.adoption_ids.get(key) == other.adoption_ids.get(key)
        else destinations
        for key, destinations in ledger.adoption_receipts.items()
    }


def receipt_write_pending(st: PersistedState, pending: PersistedState, o: Obligation) -> bool:
    keys = correction_keys(o)
    if not keys or o.look_ahead is None:
        return False
    dk = direction_key(*o.look_ahead[:2])
    active, waiting = st.lookahead.get(dk), pending.lookahead.get(dk)
    if waiting is None:
        return False
    if active is None:
        return any(waiting.adoption_receipts.get(key) for key in keys)
    for key in keys:
        received = waiting.adoption_receipts.get(key, frozenset())
        if active.adoption_ids.get(key) == waiting.adoption_ids.get(key):
            received -= active.adoption_receipts.get(key, frozenset())
        elif o.id not in waiting.facts:
            continue
        if received:
            return True
    return False


def lookahead_received(
    ledgers: dict[str, LookAheadLedger], received: Sequence[tuple[Obligation, str]]
) -> dict[str, LookAheadLedger]:
    for o, destination in received:
        correction = adoption_correction(o)
        if o.look_ahead is None or correction is None:
            continue
        key = direction_key(*o.look_ahead[:2])
        ledger = ledgers.get(key)
        if ledger is not None and ledger.adoption_ids.get(correction) == o.id:
            receipts = ledger.adoption_receipts
            destinations = receipts.get(correction, frozenset()) | {destination}
            ledgers[key] = replace(ledger, adoption_receipts={**receipts, correction: destinations})
    return ledgers


def _future(history: BaselineHistory, today: date) -> dict[str, date]:
    return {
        cancel_key(v.version): v.effective_from
        for v in history.versions
        if v.effective_from > today
    }


def _queued(st: PersistedState, dk: str, ledger: LookAheadLedger) -> tuple[Obligation, ...]:
    route = split_direction_key(dk)
    return tuple(
        o
        for o in (*ledger.pending, *st.obligations, *(c.obligation for c in st.webhook_copies))
        if o.look_ahead is not None and o.look_ahead[:2] == route
    )


def restore_adoptions(st: PersistedState, today: date, primary: str | None) -> PersistedState:
    """Recover only evidence a pre-receipt store still has for the same adoption."""
    ledgers = dict(st.lookahead)
    for dk, ledger in st.lookahead.items():
        if find_profile(st.config, split_direction_key(dk)[0]) is None:
            continue
        queued = _queued(st, dk, ledger)
        dates = _future(st.baselines.get(dk, BaselineHistory()), today)
        dates.update(
            (key, o.look_ahead[2])
            for o in queued
            if o.look_ahead is not None
            for key in correction_keys(o)
        )
        if not dates:
            continue
        pending = {o.id for o in ledger.pending}
        ids, receipts = dict(ledger.adoption_ids), dict(ledger.adoption_receipts)
        for key, day in dates.items():
            tag = "adopt:" + key.removeprefix("cancel:")
            marked = tag in ledger.per_date.get(day_key(day, None), ())
            owners = {
                o.id
                for o in queued
                if o.look_ahead is not None
                and o.look_ahead[2] == day
                and adoption_correction(o) == key
                and (
                    o.id in pending
                    or (marked and any(f.key == tag for f in ledger.facts.get(o.id, ())))
                )
            }
            # The ledger must still identify the original message, even if its phone send ended.
            if key not in ids and len(owners) == 1:
                (ids[key],) = owners
            owner = ids.get(key)
            if key not in receipts and (marked or owner is not None):
                pins = [o.target_ids for o in queued if o.id == owner and o.target_ids is not None]
                receipts[key] = frozenset()
                # Current phone confirmation cannot establish an older adoption's recipient.
                if (
                    primary is not None
                    and owner not in pending
                    and (primary == LEGACY_PRIMARY or bool(pins))
                    and all(primary in targets for targets in pins)
                ):
                    receipts[key] = frozenset({primary})
            for c in st.webhook_copies:
                if (
                    c.obligation.id == owner
                    and c.first_try is not None
                    and adoption_correction(c.obligation) == key
                ):
                    accepted = set(c.done) & (c.target_ids or frozenset())
                    if accepted:
                        receipts[key] = receipts.get(key, frozenset()) | accepted
        ledgers[dk] = replace(ledger, adoption_ids=ids, adoption_receipts=receipts)
    return replace(st, lookahead=ledgers)


def prune_adoptions(
    st: PersistedState, today: date, in_flight: Obligation | None
) -> PersistedState:
    ledgers: Mapping[str, LookAheadLedger] = st.lookahead
    trimmed = dict(ledgers)
    in_flight_ids = {
        c.obligation.id
        for c in st.webhook_copies
        if c.first_try is not None and adoption_correction(c.obligation) is not None
    }
    if in_flight is not None:
        in_flight_ids.add(in_flight.id)
    for dk, ledger in ledgers.items():
        if not ledger.adoption_receipts and not ledger.adoption_ids:
            continue
        # The receipt outlives a withdrawal only while its correction is still owed.
        live: set[str] = set()
        if find_profile(st.config, split_direction_key(dk)[0]) is not None:
            live.update(_future(st.baselines.get(dk, BaselineHistory()), today))
            live.update(key for o in _queued(st, dk, ledger) for key in correction_keys(o))
        live.update(key for key, owner in ledger.adoption_ids.items() if owner in in_flight_ids)
        trimmed[dk] = replace(
            ledger,
            adoption_receipts={k: v for k, v in ledger.adoption_receipts.items() if k in live},
            adoption_ids={k: v for k, v in ledger.adoption_ids.items() if k in live},
        )
    return st if trimmed == ledgers else replace(st, lookahead=trimmed)
