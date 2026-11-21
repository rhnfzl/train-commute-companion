"""Deliver Home Assistant profiles through the integration's existing phone adapter."""

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import httpx

from train_commute_companion.home_assistant_bridge import VERSION
from train_commute_companion.model import AlertEvent, EventKind
from train_commute_companion.notify import NotificationPaused, Notifier
from train_commute_companion.serde import encode

log = logging.getLogger(__name__)
STATUS_TTL = 30
STATUS_MAX = 1024


@dataclass(frozen=True, slots=True)
class HaDeliverySettings:
    url: str
    profiles: frozenset[str]
    ca_file: Path | None


def delivery_settings(env: Mapping[str, str], token: str | None) -> HaDeliverySettings | None:
    url_text = env.get("TCC_HOME_ASSISTANT_NOTIFY_URL", "").strip()
    profile_text = env.get("TCC_HOME_ASSISTANT_NOTIFY_PROFILES", "").strip()
    ca_text = env.get("TCC_HOME_ASSISTANT_NOTIFY_CA", "").strip()
    if not (url_text or profile_text or ca_text):
        return None
    url = httpx.URL(url_text)
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.path != "/api/train_commute_companion/bridge-alert"
    ):
        raise ValueError("TCC_HOME_ASSISTANT_NOTIFY_URL must be the private HTTPS alert endpoint")
    profiles = frozenset(item.strip() for item in profile_text.split(","))
    if not token or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) for item in profiles):
        raise ValueError("Home Assistant delivery needs a bridge token and valid profile IDs")
    return HaDeliverySettings(url_text, profiles, Path(ca_text) if ca_text else None)


class HaDelivery:
    def __init__(
        self,
        settings: HaDeliverySettings,
        token: str,
        client: httpx.AsyncClient,
        default: Notifier,
    ) -> None:
        self._settings, self._client, self._default = settings, client, default
        self._headers = {"Authorization": f"Bearer {token}"}
        self.destination: str | None = None
        self.wake_destination: str | None = None
        self._wake_ready = False
        self._refresh_at = float("-inf")
        self._refresh_lock = asyncio.Lock()

    def routes(self, profile_id: str, kind: EventKind | None = None) -> bool:
        return profile_id in self._settings.profiles or not profile_id and kind is EventKind.OFFLINE

    async def refresh(self, *, force: bool = False) -> None:
        async with self._refresh_lock:
            if not force and monotonic() < self._refresh_at:
                return
            self.destination = self.wake_destination = None
            self._wake_ready = False
            try:
                async with (
                    asyncio.timeout(3),
                    self._client.stream(
                        "GET",
                        self._settings.url,
                        headers=self._headers,
                        follow_redirects=False,
                    ) as response,
                ):
                    response.raise_for_status()
                    body = bytearray()
                    async for part in response.aiter_bytes(chunk_size=STATUS_MAX + 1):
                        body.extend(part)
                        if len(body) > STATUS_MAX:
                            raise ValueError
                reply = json.loads(body)
                if (
                    not isinstance(reply, dict)
                    or type(reply.get("version")) is not int
                    or (reply["version"] != VERSION)
                ):
                    raise ValueError
                destination = reply["destination"]
                wake = reply["wake_destination"]
                ready = reply["wake_ready"]
                if (
                    destination is not None
                    and (
                        not isinstance(destination, str)
                        or not re.fullmatch(r"ha-mobile:[a-f0-9]{64}", destination)
                    )
                    or wake is not None
                    and wake != destination
                    or type(ready) is not bool
                    or ready
                    and wake is None
                ):
                    raise ValueError
                self.destination, self.wake_destination, self._wake_ready = destination, wake, ready
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, RecursionError) as error:
                log.warning("Home Assistant alert status is unavailable (%s)", type(error).__name__)
            finally:
                self._refresh_at = monotonic() + STATUS_TTL

    async def wake_ready(self) -> bool:
        await self.refresh()
        return self._wake_ready

    async def send(self, event: AlertEvent) -> bool:
        if not self.routes(event.profile_id, event.kind):
            return await self._default.send(event)
        await self.refresh()
        destination = (
            self.wake_destination if event.kind is EventKind.WAKE_ALARM else self.destination
        )
        return await self.send_to(event, destination)

    async def send_to(self, event: AlertEvent, destination: str | None) -> bool:
        current = self.wake_destination if event.kind is EventKind.WAKE_ALARM else self.destination
        if (
            not self.routes(event.profile_id, event.kind)
            or destination is None
            or current != destination
            or event.kind is EventKind.WAKE_ALARM
            and not self._wake_ready
        ):
            raise NotificationPaused
        try:
            response = await self._client.post(
                self._settings.url,
                json={"version": VERSION, "event": encode(event)},
                headers={**self._headers, "X-TCC-Destination": destination},
                follow_redirects=False,
            )
            if response.status_code == 412:
                self.destination = self.wake_destination = None
                self._wake_ready = False
                self._refresh_at = float("-inf")
                raise NotificationPaused
            response.raise_for_status()
            reply = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.warning("Home Assistant alert delivery failed (%s)", type(error).__name__)
            return False
        if isinstance(reply, dict) and reply.get("paused") is True:
            raise NotificationPaused
        return isinstance(reply, dict) and reply.get("accepted") is True
