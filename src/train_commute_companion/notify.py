"""The delivery port: shells implement it; the core only asks whether a message got through."""

from collections.abc import Collection
from typing import Any, Protocol

from train_commute_companion.model import AlertEvent


class Notifier(Protocol):
    async def send(self, event: AlertEvent) -> bool: ...


class WebhookPort(Protocol):
    """Outbound webhooks. Never counts as delivered: the phone does (7.12 rule 16)."""

    urls: tuple[str, ...]

    async def post(
        self, event: AlertEvent, msg_id: str, urls: Collection[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        """One try per given URL: the URLs tried and the URLs that accepted. A resting URL, or
        every URL once stopped, is in neither."""
        ...


def event_fields(event: AlertEvent) -> dict[str, Any]:
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
