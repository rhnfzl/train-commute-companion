"""The delivery port: shells implement it; the core only asks whether a message got through."""

from collections.abc import Callable, Collection
from datetime import datetime
from typing import Protocol, TypedDict

from train_commute_companion.model import AlertEvent


class Notifier(Protocol):
    async def send(self, event: AlertEvent) -> bool:
        """True when the message got through. False or an exception is a failed attempt, which
        the runtime retries."""
        ...


class WebhookPort(Protocol):
    """Outbound webhooks. Never counts as delivered: the phone does (7.12 rule 16)."""

    urls: tuple[str, ...]
    # url -> end of its rest, in UTC, for every URL whose last try failed; written with the
    # state (by `url_id`) so a restart keeps the rest and the outage (rule 22)
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
