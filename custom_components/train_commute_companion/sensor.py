"""Sensors per profile and direction, all driven by the coordinator's runtime view."""

from collections.abc import Callable
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from train_commute_companion.model import Direction, Leg, Provenance, Stop, TimeObs
from train_commute_companion.view import LegView, RuntimeView

from .coordinator import TccCoordinator
from .data import TccConfigEntry
from .entity import TccEntity, health_state, pick, sync_entities, unique_id

type Fn = Callable[[RuntimeView, str, Direction | None, int | None], Any]


def _prim(v: Any) -> Any:
    """Attribute values are primitives: ISO strings for dates, values for enums (never a dataclass)."""
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if is_dataclass(v) and not isinstance(v, type):
        return {f.name: _prim(getattr(v, f.name)) for f in fields(v)}
    return v


def train(leg: LegView) -> str:
    return f"{leg.planned_departure:%H:%M} {leg.board}-{leg.alight} {leg.train_number}"


def _occ(view: RuntimeView, pid: str, d: Direction | None) -> Any:
    assert d is not None
    return pick(view, pid, d)[0]


def _status(view: RuntimeView, pid: str, d: Direction | None, n: int | None) -> dict[str, Any]:
    assert d is not None
    occ, others = pick(view, pid, d)
    if occ is None:
        return {
            "service_date": None,
            "route_changed": False,
            "others": [],
            "extra_legs": [],
            "retained": [],
        }
    info = next(i for i in view.directions if i.profile_id == pid and i.direction is d)
    return {
        "service_date": occ.key.service_date.isoformat(),
        "route_changed": occ.route_changed,
        "others": [
            {
                "service_date": o.key.service_date.isoformat(),
                "state": o.direction_state.value,
                "trains": [train(x) for x in o.legs],
            }
            for o in others
        ],
        "extra_legs": [train(x) for x in occ.legs[info.max_transfers + 2 :]],
        "retained": [[train(x) for x in j] for j in occ.retained],
    }


def _leg_time(stop: Stop, obs: TimeObs | None, planned: datetime | None) -> datetime | None:
    return obs.value if obs and obs.provenance is Provenance.REPORTED and obs.value else planned


def _next(view: RuntimeView, pid: str, d: Direction | None, n: int | None) -> Any:
    occ = _occ(view, pid, d)
    return occ.next_good if occ else None


def _first(leg: Leg) -> datetime | None:
    b = leg.board
    return _leg_time(b, b.departure, b.planned_departure)


def _advised(view: RuntimeView, pid: str, d: Direction | None, n: int | None) -> datetime | None:
    occ = _occ(view, pid, d)
    a = occ.advice if occ else None
    return a.advised_train.planned_departure if a and a.advised_train and not a.withdrawn else None


def _works(view: RuntimeView, pid: str, d: Direction | None) -> Any:
    assert d is not None
    return view.works.get(f"{pid}|{d.value}", ())


def _health_attrs(
    view: RuntimeView, pid: str, d: Direction | None, n: int | None
) -> dict[str, Any]:
    out: dict[str, Any] = {"store_ok": view.store_ok, "requests": view.requests}
    for direction in Direction:
        occ = pick(view, pid, direction)[0]
        out[f"{direction.value}_last_ok_at"] = _prim(occ.health.last_ok_at) if occ else None
        out[f"{direction.value}_consecutive_failures"] = (
            occ.health.consecutive_failures if occ else 0
        )
    return out


def _leg_state(view: RuntimeView, pid: str, d: Direction | None, n: int | None) -> str:
    occ = _occ(view, pid, d)
    assert n is not None
    return occ.legs[n - 1].state if occ and n <= len(occ.legs) else "none"


def _leg_attrs(view: RuntimeView, pid: str, d: Direction | None, n: int | None) -> dict[str, Any]:
    occ = _occ(view, pid, d)
    assert n is not None
    return _prim(occ.legs[n - 1]) if occ and n <= len(occ.legs) else {}


def _transfer(view: RuntimeView, pid: str, d: Direction | None, n: int | None) -> Any:
    occ = _occ(view, pid, d)
    assert n is not None
    return occ.transfers[n - 1] if occ and n <= len(occ.transfers) else None


def _next_attrs(view: RuntimeView, pid: str, d: Direction | None, n: int | None) -> dict[str, Any]:
    conn = _next(view, pid, d, n)
    if conn is None:
        return {}
    last = conn.legs[-1].alight
    return {
        "platform": conn.legs[0].board.actual_track or conn.legs[0].board.planned_track,
        "arrival": _prim(_leg_time(last, last.arrival, last.planned_arrival)),
        "transfers": len(conn.legs) - 1,
    }


# key -> (state, attributes, device class); the functions read one view and return plain values
SPECS: dict[str, tuple[Fn, Fn, SensorDeviceClass | None]] = {
    "status": (
        lambda v, p, d, n: o.direction_state.value if (o := _occ(v, p, d)) else "none",
        _status,
        None,
    ),
    "lock": (
        lambda v, p, d, n: o.commitment.value if (o := _occ(v, p, d)) and o.commitment else "none",
        lambda v, p, d, n: (
            {"source": _prim(o.lock_source), "trains": [train(x) for x in o.legs]}
            if (o := _occ(v, p, d))
            else {}
        ),
        None,
    ),
    "leg": (_leg_state, _leg_attrs, None),
    "transfer": (
        lambda v, p, d, n: t.state.value if (t := _transfer(v, p, d, n)) else "none",
        lambda v, p, d, n: (
            {"station": t.station, "margin_min": t.margin_min, "minimum": t.minimum}
            if (t := _transfer(v, p, d, n))
            else {}
        ),
        None,
    ),
    "next_connection": (
        lambda v, p, d, n: _first(c.legs[0]) if (c := _next(v, p, d, n)) else None,
        _next_attrs,
        SensorDeviceClass.TIMESTAMP,
    ),
    "advice": (
        _advised,
        lambda v, p, d, n: (
            {"sent_at": _prim(o.advice.sent_at) if o.advice else None}
            if (o := _occ(v, p, d))
            else {}
        ),
        SensorDeviceClass.TIMESTAMP,
    ),
    "works_ahead": (
        lambda v, p, d, n: len(_works(v, p, d)),
        lambda v, p, d, n: {
            "dates": [
                {
                    "date": w.service_date.isoformat(),
                    "leg": "journey" if w.leg_index is None else w.leg_index + 1,
                }
                for w in _works(v, p, d)
            ]
        },
        None,
    ),
    "health": (lambda v, p, d, n: health_state(v, p), _health_attrs, None),
}


class TccSensor(TccEntity, SensorEntity):
    def __init__(
        self,
        coordinator: TccCoordinator,
        profile_id: str,
        profile_name: str,
        key: str,
        direction: Direction | None = None,
        n: int | None = None,
    ) -> None:
        super().__init__(coordinator, profile_id, profile_name, key, direction, n)
        self._value_fn, self._attrs_fn, device_class = SPECS[key]
        self._attr_device_class = device_class
        if key == "health":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> Any:
        return self._value_fn(self.coordinator.data, self.profile_id, self.direction, self.n)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs_fn(self.coordinator.data, self.profile_id, self.direction, self.n)


def wanted(coordinator: TccCoordinator, view: RuntimeView) -> dict[str, Callable[[], TccEntity]]:
    out: dict[str, Callable[[], TccEntity]] = {}

    def add(
        pid: str, name: str, key: str, d: Direction | None = None, n: int | None = None
    ) -> None:
        out[unique_id(pid, key, d, n)] = lambda: TccSensor(coordinator, pid, name, key, d, n)

    for i in view.directions:
        pid, name, d = i.profile_id, i.profile_name, i.direction
        for key in ("status", "lock", "next_connection", "advice", "works_ahead"):
            add(pid, name, key, d)
        for n in range(1, i.max_transfers + 3):
            add(pid, name, "leg", d, n)
        for n in range(1, i.max_transfers + 2):
            add(pid, name, "transfer", d, n)
        add(pid, name, "health")  # once per profile: the same ID from both directions
    return out


async def async_setup_entry(
    hass: HomeAssistant, entry: TccConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    entry.async_on_unload(
        sync_entities(coordinator, "sensor", async_add_entities, lambda v: wanted(coordinator, v))
    )
