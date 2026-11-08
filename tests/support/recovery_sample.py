import json
from datetime import datetime
from pathlib import Path

from train_commute_companion.calendar import TZ
from train_commute_companion.providers.base import Query
from train_commute_companion.providers.ns import map_trips

NOW = datetime(2026, 11, 7, 11, 30, tzinfo=TZ)


def answer():
    data = Path(__file__).parents[1] / "data/ns/multimodal_physical.json"
    return map_trips(json.loads(data.read_text()), Query("UT", "RTD", NOW), NOW)


def selection():
    from train_commute_companion.model import Direction, OccurrenceKey
    from train_commute_companion.recovery import from_connection
    from train_commute_companion.recovery_state import PhysicalConfirmation, SelectedRecovery

    source = answer()
    itinerary = from_connection(source.connections[0])
    return SelectedRecovery(
        "selection",
        OccurrenceKey("p1", Direction.OUTBOUND, NOW.date()),
        0,
        0,
        itinerary,
        source.query,
        NOW,
        NOW,
        PhysicalConfirmation(itinerary.steps[0].origin, NOW),
        "approved",
        True,
        True,
    )
