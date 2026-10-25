"""The delivery port: shells implement it; the core only asks whether a message got through."""

from typing import Protocol

from train_commute_companion.model import AlertEvent


class Notifier(Protocol):
    async def send(self, event: AlertEvent) -> bool: ...
