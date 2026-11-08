"""The delivery port: shells implement it; the core only asks whether a message got through."""

from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, TypedDict

from train_commute_companion.calendar import to_utc
from train_commute_companion.model import AlertEvent


class DestinationPhase(StrEnum):
    READY = "ready"
    IN_FLIGHT = "in_flight"
    RESTING = "resting"


class DestinationReservations:
    """Reserve a receiver until its request settles, even if wall time jumps forward."""

    def __init__(self) -> None:
        self.down: dict[str, datetime] = {}
        self._active: dict[str, bool] = {}  # whether this was its first, not failed, attempt

    def phase(self, destination: str, now: datetime) -> DestinationPhase:
        if destination in self._active:
            return DestinationPhase.IN_FLIGHT
        until = self.down.get(destination)
        if until is not None and to_utc(now) < to_utc(until):
            return DestinationPhase.RESTING
        return DestinationPhase.READY

    def due(self, destinations: Collection[str], now: datetime) -> frozenset[str]:
        return frozenset(d for d in destinations if self.phase(d, now) is DestinationPhase.READY)

    def reserve(
        self, destinations: Collection[str], now: datetime, rest: timedelta
    ) -> frozenset[str]:
        ready = self.due(destinations, now)
        for d in ready:
            self._active[d] = d not in self.down
            self.down[d] = to_utc(now) + rest
        return ready

    def settle(self, destination: str, accepted: bool) -> None:
        if destination not in self._active:
            raise ValueError("a destination result needs an active reservation")
        self._active.pop(destination)
        if accepted:
            self.down.pop(destination, None)

    def failing(self, destinations: Collection[str]) -> bool:
        return any(d in self.down and not self._active.get(d, False) for d in destinations)


class Notifier(Protocol):
    async def send(self, event: AlertEvent) -> bool:
        """True when the message got through. False or an exception is a failed attempt, which
        the runtime retries."""
        ...


class WebhookPort(Protocol):
    """Outbound webhooks. Never counts as delivered: the phone does (7.12 rule 16)."""

    urls: tuple[str, ...]
    channels: dict[str, Literal["primary", "email", "webhook"]]
    destination_ids: dict[str, str]
    # url -> end of its rest, in UTC, for every URL whose last try failed; written with the
    # state by authentication-scoped destination ID, so a restart keeps its rest and outage
    down: dict[str, datetime]
    # called right after a try reserved its rests in `down`, before its request leaves, and
    # again after a try that cleared one, so the runtime writes them at once
    on_reserve: Callable[[], None]

    def due(self, urls: Collection[str]) -> frozenset[str]:
        """The given URLs a post now would try: none resting, none once stopped."""
        ...

    async def post(
        self, event: AlertEvent, msg_id: str, urls: Collection[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        """One try per given URL: the URLs tried and the URLs that accepted. A resting URL, or
        every URL once stopped, is in neither."""
        ...


def destination_id(port: WebhookPort, destination: str) -> str:
    """Each adapter defines its immutable receiver scope; no caller reconstructs it."""
    return port.destination_ids[destination]


class EventFields(TypedDict):
    """The JSON fields of an alert, in the order they are sent."""

    profile_id: str
    profile_name: str
    direction: str | None
    service_date: str | None
    leg_index: int | None
    kind: str  # an EventKind value, or "test" in a webhook setup test
    severity: str
    train_operating_date: str | None
    train_number: str | None
    train_departure: str | None
    text: str
    key: str
    created_at: str


def event_fields(event: AlertEvent) -> EventFields:
    """The JSON fields of an alert, shared by the HA bus event and the webhook body."""
    t = event.train
    return {
        "profile_id": event.profile_id,
        "profile_name": event.profile_name,
        "direction": event.direction.value if event.direction else None,
        "service_date": event.service_date.isoformat() if event.service_date else None,
        "leg_index": event.leg_index,
        "kind": event.kind.value,
        "severity": event.severity.value,
        "train_operating_date": t.operating_date.isoformat() if t else None,
        "train_number": t.train_number if t else None,
        "train_departure": t.planned_departure.isoformat() if t else None,
        "text": event.text,
        "key": event.key,
        "created_at": event.created_at.isoformat(),
    }
