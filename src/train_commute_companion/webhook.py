"""Outbound webhook payload and Standard Webhooks signature. Pure: no network, clock or logging;
the sender (`runtime.WebhookPoster`) owns delivery."""

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from train_commute_companion.model import AlertEvent, Severity
from train_commute_companion.notify import event_fields

SCHEMA_VERSION = 1
MIN_SECRET_LEN = 16
ATTRIBUTION = "Data: NS"
PROBE_LOUD = "Train Commute Companion: webhook test, loud."
PROBE_SILENT = "Train Commute Companion: webhook test, silent."
_SECRET_RULE = "must be at least 16 characters, or whsec_ and base64 of at least 16 bytes"
_TOKEN_RULE = "must be at least 16 characters of printable ASCII without spaces"
# the `event_fields` keys, in order; tests/test_webhook.py pins them to it
_FIELD_KEYS = (
    "profile_id",
    "profile_name",
    "direction",
    "service_date",
    "leg_index",
    "kind",
    "severity",
    "train_operating_date",
    "train_number",
    "train_departure",
    "text",
    "key",
    "created_at",
)


def _encode(fields: dict[str, Any], test: bool) -> bytes:
    payload = {"version": SCHEMA_VERSION, "attribution": ATTRIBUTION, "test": test, **fields}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def body(event: AlertEvent) -> bytes:
    return _encode(event_fields(event), False)


def probe_body(severity: Severity, created_at: datetime) -> bytes:
    """A setup test send: shaped like an alert, marked `"test": true`."""
    fields: dict[str, Any] = dict.fromkeys(_FIELD_KEYS)
    fields |= {
        "kind": "test",
        "severity": severity.value,
        "text": PROBE_LOUD if severity is Severity.LOUD else PROBE_SILENT,
        "created_at": created_at.isoformat(),
    }
    return _encode(fields, True)


def event_id(obligation_id: str) -> str:
    return "tcc_" + hashlib.sha256(obligation_id.encode()).hexdigest()[:32]


def probe_id(severity: Severity, created_at: datetime) -> str:
    return event_id(f"test|{severity.value}|{created_at.isoformat()}")


@dataclass(frozen=True, slots=True)
class WebhookTestResult:
    """One URL's setup test send: each field the HTTP status or the error class name."""

    host: str
    loud: int | str
    silent: int | str

    @property
    def ok(self) -> bool:
        return all(isinstance(s, int) and 200 <= s <= 299 for s in (self.loud, self.silent))

    def summary(self) -> str:
        return f"{self.host} (loud {self.loud}, silent {self.silent})"


def signing_key(secret: str) -> bytes:
    """Raises ValueError (binascii.Error included) for a `whsec_` secret that is not base64."""
    if secret.startswith("whsec_"):
        return base64.b64decode(secret[6:], validate=True)
    return secret.encode()


def signature(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    mac = hmac.new(signing_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def headers(
    secret: str, msg_id: str, timestamp: int, body: bytes, token: str = ""
) -> dict[str, str]:
    h = {
        "content-type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature(secret, msg_id, timestamp, body),
        "idempotency-key": msg_id,
    }
    if token:
        h["authorization"] = f"Bearer {token}"
    return h


def _valid_url(text: str) -> str | None:
    try:
        url = httpx.URL(text)
    except (httpx.InvalidURL, ValueError):
        return None
    port_ok = url.port is None or 1 <= url.port <= 65535
    if url.scheme in ("http", "https") and url.host and port_ok and not url.userinfo:
        return str(url)
    return None


def parse_urls(text: str) -> tuple[str, ...]:
    """Comma or whitespace separated URLs; an error names the entry, never its value (a path or
    query can carry a route token)."""
    urls: list[str] = []
    for n, part in enumerate((p for p in re.split(r"[,\s]+", text) if p), 1):
        url = _valid_url(part)
        if url is None:
            raise ValueError(
                f"entry {n} is not an http or https URL without a user name or password"
            ) from None
        if url not in urls:
            urls.append(url)
    return tuple(urls)


def check_secret(secret: str) -> None:
    if len(secret) < MIN_SECRET_LEN:
        raise ValueError(_SECRET_RULE)
    if secret.startswith("whsec_"):
        try:
            key = signing_key(secret)
        except ValueError:
            raise ValueError(_SECRET_RULE) from None
        if len(key) < MIN_SECRET_LEN:
            raise ValueError(_SECRET_RULE)


def check_token(token: str) -> None:
    # it goes into an HTTP header: httpx raises UnicodeEncodeError on non-ASCII there
    if len(token) < MIN_SECRET_LEN or not (token.isascii() and token.isprintable()) or " " in token:
        raise ValueError(_TOKEN_RULE)


def url_id(url: str) -> str:
    """How the store names a URL: never the URL itself, which can carry a route token."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]
