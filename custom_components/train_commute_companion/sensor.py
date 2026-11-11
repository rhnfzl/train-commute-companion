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
from homeassistant.helpers.typing import StateType

from train_commute_companion.model import (
    Connection,
    Direction,
    Leg,
    Provenance,
    Stop,
    TimeObs,
    direction_key,
)
from train_commute_companion.vehicle import snapshot_payload
from train_commute_companion.view import (
    LegView,
    OccurrenceView,
    RuntimeView,
    TransferView,
    WorksDay,
)

from .coordinator import TccCoordinator
from .data import TccConfigEntry
from .entity import TccEntity, health_state, shown_and_others, sync_entities, unique_id

type Value = StateType | date | datetime
type Attrs = dict[str, Any]
# (state, attributes, device class): each reads one view for a profile, a direction or a leg
type Spec[*Where] = tuple[
    Callable[[RuntimeView, *Where], Value],
    Callable[[RuntimeView, *Where], Attrs],
    SensorDeviceClass | None,
]
type Bound = Spec[()]


def _to_primitive(v: Any) -> Any:
    """Attribute values are primitives: ISO strings for dates, values for enums (never a dataclass)."""
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if is_dataclass(v) and not isinstance(v, type):
        return {f.name: _to_primitive(getattr(v, f.name)) for f in fields(v)}
    return v


def _train_label(leg: LegView) -> str:
    return f"{leg.planned_departure:%H:%M} {leg.board}-{leg.alight} {leg.train_number}"


def _occ(view: RuntimeView, pid: str, d: Direction) -> OccurrenceView | None:
    return shown_and_others(view, pid, d)[0]


def _status_attrs(view: RuntimeView, pid: str, d: Direction) -> Attrs:
    occ, others = shown_and_others(view, pid, d)
    if occ is None:
        return {
            "service_date": None,
            "not_travelling": False,
            "route_changed": False,
            "others": [],
            "extra_legs": [],
            "retained": [],
            "active_disruptions": [],
            "disruptions_checked_at": None,
            "disruptions_coverage": "unknown",
            "recovery_offer": None,
            "selected_recovery": None,
            "original_train_state": None,
        }
    info = next(i for i in view.directions if i.profile_id == pid and i.direction is d)
    return {
        "service_date": occ.key.service_date.isoformat(),
        "not_travelling": occ.not_travelling,
        "original_train_state": occ.direction_state.value,
        "recovery_offer": occ.recovery_offer.fields() if occ.recovery_offer else None,
        "selected_recovery": (
            occ.selected_recovery.fields(view.at) if occ.selected_recovery else None
        ),
        "active_disruptions": [
            {"id": d.id, "title": d.title, "stations": sorted(d.stations), "url": d.url}
            for d in occ.disruptions
        ],
        "disruptions_checked_at": _to_primitive(occ.disruptions_checked_at),
        "disruptions_coverage": occ.disruptions_coverage,
        "replacement_plans": [plan.fields() for plan in occ.replacement_plans],
        "train_context": None if occ.train_context is None else snapshot_payload(occ.train_context),
        "route_changed": occ.route_changed,
        "others": [
            {
                "service_date": o.key.service_date.isoformat(),
                "state": o.direction_state.value,
                "trains": [_train_label(x) for x in o.legs],
            }
            for o in others
        ],
        "extra_legs": [_train_label(x) for x in occ.legs[info.max_transfers + 2 :]],
        "retained": [[_train_label(x) for x in j] for j in occ.retained],
    }


def _leg_time(stop: Stop, obs: TimeObs | None, planned: datetime | None) -> datetime | None:
    return obs.value if obs and obs.provenance is Provenance.REPORTED and obs.value else planned


def _next_good_train(view: RuntimeView, pid: str, d: Direction) -> Connection | None:
    occ = _occ(view, pid, d)
    return occ.next_good if occ else None


def _first_departure(leg: Leg) -> datetime | None:
    b = leg.board
    return _leg_time(b, b.departure, b.planned_departure)


def _advised(view: RuntimeView, pid: str, d: Direction) -> datetime | None:
    occ = _occ(view, pid, d)
    a = occ.advice if occ else None
    return a.advised_train.planned_departure if a and a.advised_train and not a.withdrawn else None


def _works(view: RuntimeView, pid: str, d: Direction) -> tuple[WorksDay, ...]:
    return view.works.get(direction_key(pid, d), ())


def _health_attrs(view: RuntimeView, pid: str) -> Attrs:
    out: Attrs = {"store_ok": view.store_ok, "requests": view.requests}
    out["lookahead_last_ok"] = {
        d.value: _to_primitive(view.lookahead_last_ok.get(direction_key(pid, d))) for d in Direction
    }
    for direction in Direction:
        occ = shown_and_others(view, pid, direction)[0]
        out[f"{direction.value}_last_ok_at"] = _to_primitive(occ.health.last_ok_at) if occ else None
        out[f"{direction.value}_consecutive_failures"] = (
            occ.health.consecutive_failures if occ else 0
        )
    return out


def _leg_state(view: RuntimeView, pid: str, d: Direction, n: int) -> str:
    occ = _occ(view, pid, d)
    return occ.legs[n - 1].state.value if occ and n <= len(occ.legs) else "none"


def _leg_attrs(view: RuntimeView, pid: str, d: Direction, n: int) -> Attrs:
    occ = _occ(view, pid, d)
    return _to_primitive(occ.legs[n - 1]) if occ and n <= len(occ.legs) else {}


def _transfer(view: RuntimeView, pid: str, d: Direction, n: int) -> TransferView | None:
    occ = _occ(view, pid, d)
    return occ.transfers[n - 1] if occ and n <= len(occ.transfers) else None


def _next_attrs(view: RuntimeView, pid: str, d: Direction) -> Attrs:
    conn = _next_good_train(view, pid, d)
    if conn is None:
        return {}
    last = conn.legs[-1].alight
    return {
        "platform": conn.legs[0].board.actual_track or conn.legs[0].board.planned_track,
        "arrival": _to_primitive(_leg_time(last, last.arrival, last.planned_arrival)),
        "transfers": len(conn.legs) - 1,
        "crowd_forecast": conn.legs[0].crowd_forecast,
        "boarding_notes": list(conn.legs[0].board.accessibility_notes),
        "alighting_notes": list(last.accessibility_notes),
        "exit_side": last.exit_side,
    }


DIRECTION_SPECS: dict[str, Spec[str, Direction]] = {
    "status": (
        lambda v, p, d: (
            (
                "recovery_active"
                if o.selected_recovery is not None and o.selected_recovery.status == "active"
                else o.direction_state.value
            )
            if (o := _occ(v, p, d))
            else "none"
        ),
        _status_attrs,
        None,
    ),
    "lock": (
        lambda v, p, d: o.commitment.value if (o := _occ(v, p, d)) and o.commitment else "none",
        lambda v, p, d: (
            {"source": _to_primitive(o.lock_source), "trains": [_train_label(x) for x in o.legs]}
            if (o := _occ(v, p, d))
            else {}
        ),
        None,
    ),
    "next_connection": (
        lambda v, p, d: _first_departure(c.legs[0]) if (c := _next_good_train(v, p, d)) else None,
        _next_attrs,
        SensorDeviceClass.TIMESTAMP,
    ),
    "advice": (
        _advised,
        lambda v, p, d: (
            {"sent_at": _to_primitive(o.advice.sent_at) if o.advice else None}
            if (o := _occ(v, p, d))
            else {}
        ),
        SensorDeviceClass.TIMESTAMP,
    ),
    "works_ahead": (
        lambda v, p, d: len(_works(v, p, d)),
        lambda v, p, d: {
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
}
LEG_SPEC: Spec[str, Direction, int] = (_leg_state, _leg_attrs, None)
TRANSFER_SPEC: Spec[str, Direction, int] = (
    lambda v, p, d, n: t.state.value if (t := _transfer(v, p, d, n)) else "none",
    lambda v, p, d, n: (
        {"station": t.station, "margin_min": t.margin_min, "minimum": t.minimum}
        if (t := _transfer(v, p, d, n))
        else {}
    ),
    None,
)
HEALTH_SPEC: Spec[str] = (health_state, _health_attrs, None)


def _bind[*Where](spec: Spec[*Where], *where: *Where) -> Bound:
    value_fn, attrs_fn, device_class = spec
    return lambda v: value_fn(v, *where), lambda v: attrs_fn(v, *where), device_class


class TccSensor(TccEntity, SensorEntity):
    def __init__(
        self,
        coordinator: TccCoordinator,
        profile_id: str,
        profile_name: str,
        key: str,
        spec: Bound,
        direction: Direction | None = None,
        n: int | None = None,
    ) -> None:
        super().__init__(coordinator, profile_id, profile_name, key, direction, n)
        self._value_fn, self._attrs_fn, device_class = spec
        self._attr_device_class = device_class
        if key == "health":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> Value:
        if self._attr_translation_key == "health":
            return health_state(
                self.coordinator.data,
                self.profile_id,
                notifications_paused=self.coordinator.notification_paused(),
            )
        return self._value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> Attrs:
        attrs = self._attrs_fn(self.coordinator.data)
        if self._attr_translation_key == "health":
            attrs.update(
                notifications_paused=self.coordinator.notification_paused(),
                notifications_quiet=self.coordinator.notification_quiet(),
            )
        return attrs


def wanted(coordinator: TccCoordinator, view: RuntimeView) -> dict[str, Callable[[], TccEntity]]:
    out: dict[str, Callable[[], TccEntity]] = {}

    def add(
        pid: str, name: str, key: str, spec: Bound, d: Direction | None = None, n: int | None = None
    ) -> None:
        out[unique_id(pid, key, d, n)] = lambda: TccSensor(coordinator, pid, name, key, spec, d, n)

    for i in view.directions:
        pid, name, d = i.profile_id, i.profile_name, i.direction
        for key, spec in DIRECTION_SPECS.items():
            add(pid, name, key, _bind(spec, pid, d), d)
        for n in range(1, i.max_transfers + 3):
            add(pid, name, "leg", _bind(LEG_SPEC, pid, d, n), d, n)
        for n in range(1, i.max_transfers + 2):
            add(pid, name, "transfer", _bind(TRANSFER_SPEC, pid, d, n), d, n)
        # once per profile: the same ID from both directions
        add(pid, name, "health", _bind(HEALTH_SPEC, pid))
    return out


async def async_setup_entry(
    hass: HomeAssistant, entry: TccConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    entry.async_on_unload(
        sync_entities(coordinator, "sensor", async_add_entities, lambda v: wanted(coordinator, v))
    )
