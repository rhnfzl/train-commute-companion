"""Opt-in live NS checks. Capture alerts locally without sending phone notifications."""

import os
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
import pytest
import yaml

from train_commute_companion.calendar import WEEKDAYS, SystemClock, to_utc
from train_commute_companion.model import AlertEvent, Direction
from train_commute_companion.providers.base import Query
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.runtime import Runtime
from train_commute_companion.store import JsonFileStore

pytestmark = pytest.mark.skipif(not os.environ.get("NS_API_KEY"), reason="NS_API_KEY is required")


@dataclass
class Capture:
    events: list[AlertEvent] = field(default_factory=list)

    async def send(self, event: AlertEvent) -> bool:
        self.events.append(event)
        return True


@pytest.mark.parametrize("origin,destination", [("UT", "RTD"), ("AMF", "UT")])
@pytest.mark.parametrize("require_accessible", [False, True])
async def test_live_journey_locks_refreshes_and_persists_without_storing_credentials(
    tmp_path, origin, destination, require_accessible
):
    clock = SystemClock()
    now = clock.now()
    async with httpx.AsyncClient(timeout=30) as client:
        provider = NsProvider(os.environ["NS_API_KEY"], client, clock.now, deadline=30)
        stations = await provider.stations()
        assert {origin, destination} <= stations
        answer = await provider.trips(
            Query(origin, destination, now, require_accessible=require_accessible)
        )
        candidates = [
            c
            for c in answer.connections
            if c.is_train_only
            and len(c.legs) == 1
            and c.legs[0].train.operating_date == now.date()
            and timedelta(minutes=2)
            < to_utc(c.legs[0].train.planned_departure) - to_utc(now)
            < timedelta(minutes=40)
        ]
        assert candidates, "No eligible near-future direct train for this live sample"
        train = min(candidates, key=lambda c: to_utc(c.legs[0].train.planned_departure)).legs[0]
        departure = train.train.planned_departure
        homeward = departure + timedelta(hours=12)
        path = tmp_path / "profiles.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "profiles": [
                        {
                            "id": "live",
                            "name": "live_probe",
                            "days": [WEEKDAYS[now.isoweekday() - 1]],
                            "home_station": origin,
                            "work_station": destination,
                            "skip_public_holidays": False,
                            "accessibility": {"require_accessible": require_accessible},
                            "outbound": {
                                "first_train": (departure - timedelta(minutes=5)).strftime("%H:%M"),
                                "first_train_by_weekday": {
                                    WEEKDAYS[now.isoweekday() - 1]: departure.strftime("%H:%M")
                                },
                                "buffer_min": 0,
                            },
                            "return": {
                                "first_train": homeward.strftime("%H:%M"),
                                "buffer_min": 0,
                                "day_offset": (homeward.date() - now.date()).days,
                            },
                        }
                    ]
                }
            )
        )
        store = JsonFileStore(tmp_path / "state.json")
        capture = Capture()
        runtime = Runtime(
            profile_path=path,
            store=store,
            provider=provider,
            notifier=capture,
            clock=clock,
            known_stations=stations,
            require_stations=True,
        )
        try:
            await runtime.start()
            await runtime.poll_once()
            result = await runtime.choose_at(
                "live",
                departure.time(),
                train.train.train_number,
                departure.date(),
                Direction.OUTBOUND,
            )
            assert result is None, result
            await runtime.poll_once()
            await runtime.vehicle_context_idle()
            view = runtime.view()
            assert view.started and view.store_ok and not view.key_rejected and view.refusal is None
            outbound = [o for o in view.occurrences if o.key.direction is Direction.OUTBOUND]
            assert len(outbound) == 1 and outbound[0].legs
            assert outbound[0].legs[0].train_number == train.train.train_number
            assert outbound[0].health.fresh
            context = outbound[0].train_context
            assert context is not None
            assert context.watch.train.train_number == train.train.train_number
            assert context.watch.station == origin
            assert context.operating_date_confirmed is False
            assert context.measurement_at is None
            # The optional product may have no composition for an otherwise valid journey.
            if context.context is not None:
                assert context.context.station == origin
                assert context.context.requested_train == context.watch.train
            assert context.positions_checked_at is not None
            assert outbound[0].legs[0].crowd_forecast in (None, "low", "medium", "high")
            restored = store.load()
            assert restored is not None and restored.occurrences
            secret_in_store = os.environ["NS_API_KEY"] in (tmp_path / "state.json").read_text()
            assert not secret_in_store, "The runtime must not persist credentials"
        finally:
            await runtime.stop_vehicle_context()
