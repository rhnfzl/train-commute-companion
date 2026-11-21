"""Transaction phases protect pending actions and historical store compatibility."""

from dataclasses import replace
from datetime import datetime

import pytest
from lifecycle_fixtures import full_state
from test_delivery import ob

from train_commute_companion.calendar import TZ
from train_commute_companion.history import JourneyHistory
from train_commute_companion.model import EventKind
from train_commute_companion.replay import MemoryStore
from train_commute_companion.state_writer import Change, StateWriter
from train_commute_companion.store import StorageError
from train_commute_companion.webhook_queue import CopyQueue


class FlakyStore(MemoryStore):
    failing = False

    def save(self, state):
        if self.failing:
            raise StorageError("disk full")
        super().save(state)


def writer():
    store = FlakyStore()
    queue = CopyQueue(
        None, lambda: datetime.now(TZ), lambda _: None, lambda _: False, lambda _: None
    )
    return StateWriter(store, queue, lambda: ob(EventKind.STORAGE_FAILED)), store


def test_pending_action_cannot_be_discarded_by_starting_again():
    w, store = writer()
    w.begin(full_state())
    store.failing = True
    w.commit(replace(w.st, started_before=False), Change.ACTION)
    assert w.waiting
    with pytest.raises(RuntimeError, match="pending"):
        w.begin(full_state())
    assert w.waiting
    store.failing = False
    w.retry()
    assert not w.waiting
    assert store.load().started_before is False


@pytest.mark.parametrize("already_pending", [False, True])
def test_unwritten_archive_keeps_history_when_another_change_lands(already_pending):
    w, store = writer()
    w.begin(full_state())
    key, occurrence = next(iter(w.st.occurrences.items()))
    row = JourneyHistory(occurrence.key, None, None, None, True, False, False)
    store.failing = True
    if already_pending:
        w.commit(replace(w.st, started_before=not w.st.started_before), Change.RECOMPUTED)
    w.commit(
        replace(
            w.st,
            occurrences={k: v for k, v in w.st.occurrences.items() if k != key},
            commute_history={key: row},
        ),
        Change.TICK,
    )
    assert w.waiting
    store.failing = False
    w.commit(w.st, Change.LOOKAHEAD)
    assert key not in store.load().occurrences
    assert store.load().commute_history[key] == row


def test_writer_phase_is_transactional_and_strict_failure_is_not_replayed():
    from train_commute_companion.state_writer import WritePhase

    w, store = writer()
    original = full_state()
    w.begin(original)
    assert w.phase is WritePhase.DURABLE
    candidate = replace(original, started_before=False)
    w.propose(candidate)
    assert w.phase is WritePhase.PROPOSED
    store.failing = True
    w.commit(candidate, Change.ACTION)
    assert w.phase is WritePhase.PENDING
    assert not w.commit_strict(original)
    store.failing = False
    w.retry()
    assert w.phase is WritePhase.DURABLE and w.st.started_before is False
    store.failing = True
    assert not w.commit_strict(original)
    assert w.phase is WritePhase.DEGRADED
    store.failing = False
    w.commit(w.st, Change.RECOMPUTED)
    assert w.st.started_before is False
    assert w.phase is WritePhase.DURABLE


def test_pre_refactor_state_load_save_read_preserves_fields_and_adds_resume_default(tmp_path):
    import json
    from pathlib import Path

    from train_commute_companion.serde import encode
    from train_commute_companion.store import JsonFileStore

    fixture = Path(__file__).parent / "data/state/pre-explicit-v2.json"
    raw = json.loads(fixture.read_text())
    path = tmp_path / "state.json"
    path.write_text(fixture.read_text())
    store = JsonFileStore(path)
    state = store.load()

    def obligation_defaults(value):
        if isinstance(value, dict):
            if {"fact_key", "event", "target_ids"} <= value.keys():
                value["primary_suppressed"] = False
                value["alert_history_origin"] = None
            for child in value.values():
                obligation_defaults(child)
        elif isinstance(value, list):
            for child in value:
                obligation_defaults(child)

    obligation_defaults(raw["state"])
    for occurrence in raw["state"]["occurrences"].values():
        occurrence["skipped_direction_state"] = None
        occurrence["moved_at"] = None  # Q12 defaults for an older store
        occurrence["wifi_reports"] = {}
        occurrence["released"] = False  # 2026-11-18 defaults for an older store
        occurrence["missed_from_origin"] = False
        occurrence["history_flags"] = []
        occurrence |= {  # 0.3.0 other boarding station defaults
            "boarding_station": None,
            "prompt": None,
            "asked_stations": [],
            "station_seen": {},
            # seeded from zone_reports so the 30-minute guard survives the upgrade
            "station_visits": occurrence.get("zone_reports", {}),
        }
        assert occurrence["station_visits"]  # the fixture holds a report: not a vacuous check
        for journey in (occurrence["lock"], *occurrence["retained"]):
            if journey is not None:
                journey["unproven"] = False  # 0.3.0 other-station guess default
                if journey["plan"] is not None:
                    journey["plan"] |= {"origin": None, "train_numbers": []}
    raw["state"]["remembered_stations"] = {}
    raw["state"]["commute_history"] = {}
    raw["state"]["commute_recap_weeks"] = {}
    raw["state"]["alert_history"] = []
    raw["state"] |= {"travel_plans": {}, "travel_receipts": {}, "travel_revision": 0}
    raw["state"]["brief_days"] = {}
    obligations = [*raw["state"]["obligations"]]
    obligations.extend(copy["obligation"] for copy in raw["state"]["webhook_copies"])
    for ledger in raw["state"]["lookahead"].values():
        ledger["last_evening"] = None
        ledger["last_evening_attempt"] = None
        ledger["evening_failures"] = 0
        obligations.extend(ledger["pending"])
    for obligation in obligations:
        obligation["brief_directions"] = []
        obligation["brief_rows"] = []
    for cfg in (raw["state"]["config"], *raw["state"]["config_snapshots"].values()):
        for profile in cfg["profiles"]:
            profile["travel_plan"] = False
            profile["repeat"] = None
            for d in ("outbound", "return_"):
                profile[d]["active_lead_min"] = None  # Q9 default for an older store
                profile[d] |= {  # 0.3.0 window mode defaults
                    "mode": "fixed",
                    "pick": "earliest",
                    "min_stay_min": None,
                    "by_weekday": {},
                    "first_train_set": True,
                    "other_stations": [],
                    "exact_train": None,
                    "arrive_by": None,
                }
                profile[d] |= {"arrive_by": None, "walk_after_min": 0, "prefer_direct_min": 15}
            profile["alerts"]["quiet_repeat_misses"] = False  # 2026-11-18 default
            profile["alerts"]["weather"] = True
            profile["alerts"] |= {"delay_min": 3, "delay_step_min": 3}
            profile["alerts"]["leave_by"] = True
            profile["alerts"]["weather_allowance"] = True
            profile["alerts"]["quick_trip"] = True
            profile["alerts"] |= {
                "commute_stats": True,
                "weekly_recap": True,
                "after_trip_note": True,
            }
            profile["alerts"] |= {
                "custom_timetable_horizon": True,
                "timetable_horizon_unit": "calendar_days",
                "timetable_horizon_count": 7,
                "daily_brief": True,
                "arrival_prompt": True,
                "compensation_details": True,
                "navigate": True,
                "timetable_update": True,
            }
            profile["alerts"] |= {"live_today": True, "telegram_status_pin": True}
            profile["alerts"]["ntfy_enabled"] = True
            profile["alerts"]["arrive_by"] = True
            profile["alerts"]["alert_history"] = False
            profile["alerts"]["calendar_feed"] = True
            profile["brief"] = {"at": None, "evening_at": "20:00:00", "only_if_something": False}
            profile["alerts"]["location_leave_now"] = True
            if profile["location"] is not None:  # Q12 defaults
                profile["location"] |= {"wifi": {}, "activity_entity": None}
                profile["location"] |= {  # 0.3.0 other boarding station defaults
                    "other_station_radius_m": 500,
                    "other_station_dwell_min": 0,
                    "ask_to_remember_station": True,
                }
    for ledger in raw["state"]["lookahead"].values():
        ledger |= {"adoption_ids": {}, "adoption_receipts": {}}
    for history in raw["state"]["baselines"].values():
        for version in history["versions"]:
            version["update_nonce"] = None
    assert encode(state) == raw["state"]
    store.save(state)
    assert JsonFileStore(path).load() == state
    assert json.loads(path.read_text())["state"] == raw["state"]


def test_legacy_advice_migrates_then_survives_current_save(tmp_path):
    import json
    from pathlib import Path

    from train_commute_companion.serde import encode
    from train_commute_companion.store import JsonFileStore

    raw = json.loads((Path(__file__).parent / "data/state/pre-explicit-v2.json").read_text())
    raw["version"] = 1
    for state in raw["state"]["occurrences"].values():
        if state["advice"] is not None:
            state["advice"].pop("delivered")
    path = tmp_path / "state.json"
    path.write_text(json.dumps(raw))
    store = JsonFileStore(path)
    state = store.load()
    assert all(o.advice is None or o.advice.delivered for o in state.occurrences.values())
    migrated = encode(state)
    store.save(state)
    assert encode(JsonFileStore(path).load()) == migrated
