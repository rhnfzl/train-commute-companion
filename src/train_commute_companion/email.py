"""Send private commute updates through an encrypted SMTP connection."""

import asyncio
import base64
import contextlib
import hashlib
import logging
import ssl
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from enum import StrEnum
from typing import Literal

from .model import AlertEvent
from .notify import DestinationReservations
from .webhook import url_id

log = logging.getLogger(__name__)
TIMEOUT_S = 15.0
REST_S = 60


@dataclass(frozen=True, slots=True, repr=False)
class EmailSettings:
    host: str
    port: int
    security: Literal["ssl", "starttls"]
    sender: str
    recipient: str
    username: str = ""
    password: str = ""

    def __post_init__(self) -> None:
        if not self.host or any(c.isspace() or ord(c) < 32 for c in self.host):
            raise ValueError("SMTP_HOST must be a hostname")
        if any(c in self.host for c in "/@\\"):
            raise ValueError("SMTP_HOST must be a hostname")
        if not 1 <= self.port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if self.security not in ("ssl", "starttls"):
            raise ValueError("SMTP_SECURITY must be ssl or starttls")
        for name, value in (("SMTP_FROM", self.sender), ("SMTP_TO", self.recipient)):
            try:
                if not value.isascii() or any(c.isspace() or ord(c) < 32 for c in value):
                    raise ValueError
                address = Address(addr_spec=value)
                if not address.username or not address.domain or address.addr_spec != value:
                    raise ValueError
            except (ValueError, HeaderParseError):
                raise ValueError(f"{name} must be one ASCII email address") from None
        if bool(self.username) != bool(self.password):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must both be set")
        if any(c in self.username + self.password for c in "\0\r\n"):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD contain invalid characters")


def message(settings: EmailSettings, event: AlertEvent, msg_id: str) -> bytes:
    result = EmailMessage(policy=SMTP)
    result["From"] = settings.sender
    result["To"] = settings.recipient
    result["Subject"] = f"Train Commute Companion: {event.severity.value} update"
    result["Date"] = format_datetime(event.created_at)
    digest = hashlib.sha256(msg_id.encode()).hexdigest()
    result["Message-ID"] = f"<{digest}@train-commute-companion.invalid>"
    result.set_content(
        f"{event.profile_name}\n\n{event.text}\n\nData: NS\n", cte="quoted-printable"
    )
    return result.as_bytes()


class SmtpError(Exception):
    """A rejected or malformed SMTP reply. Server text stays private."""


async def _reply(reader: asyncio.StreamReader, expected: int) -> list[bytes]:
    lines = []
    for _ in range(100):
        line = await reader.readline()
        if len(line) > 4096 or len(line) < 5 or not line.endswith(b"\r\n"):
            raise SmtpError("invalid SMTP reply")
        if line[:3] != str(expected).encode() or line[3:4] not in (b" ", b"-"):
            raise SmtpError("SMTP request rejected")
        lines.append(line[4:-2])
        if line[3:4] == b" ":
            return lines
    raise SmtpError("SMTP reply exceeds limit")


async def _command(
    writer: asyncio.StreamWriter, reader: asyncio.StreamReader, command: bytes, expected: int
) -> list[bytes]:
    writer.write(command + b"\r\n")
    await writer.drain()
    return await _reply(reader, expected)


class SmtpPhase(StrEnum):
    CONNECTED = "connected"
    GREETED = "greeted"
    IDENTIFIED_CLEAR = "identified_clear"
    NEGOTIATING_TLS = "negotiating_tls"
    TLS_READY = "tls_ready"
    IDENTIFIED_TLS = "identified_tls"
    AUTHENTICATED = "authenticated"
    SENDER_ACCEPTED = "sender_accepted"
    RECIPIENT_ACCEPTED = "recipient_accepted"
    DATA_READY = "data_ready"
    ACCEPTED = "accepted"


class SmtpSession:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, encrypted: bool
    ) -> None:
        self.reader, self.writer = reader, writer
        self.encrypted = encrypted
        self.phase = SmtpPhase.CONNECTED

    def expect(self, phases: SmtpPhase | tuple[SmtpPhase, ...]) -> None:
        allowed = (phases,) if isinstance(phases, SmtpPhase) else phases
        if self.phase not in allowed:
            raise SmtpError("SMTP operation is not allowed in this phase")

    async def command(
        self,
        expected: SmtpPhase | tuple[SmtpPhase, ...],
        following: SmtpPhase,
        command: bytes,
        reply: int,
    ) -> list[bytes]:
        self.expect(expected)
        lines = await _command(self.writer, self.reader, command, reply)
        self.phase = following
        return lines

    async def greet(self) -> None:
        self.expect(SmtpPhase.CONNECTED)
        await _reply(self.reader, 220)
        self.phase = SmtpPhase.GREETED

    async def identify(self) -> list[bytes]:
        return await self.command(
            (SmtpPhase.GREETED, SmtpPhase.TLS_READY),
            SmtpPhase.IDENTIFIED_TLS if self.encrypted else SmtpPhase.IDENTIFIED_CLEAR,
            b"EHLO tcc.invalid",
            250,
        )

    async def secure(self, context: ssl.SSLContext, host: str) -> None:
        await self.command(SmtpPhase.IDENTIFIED_CLEAR, SmtpPhase.NEGOTIATING_TLS, b"STARTTLS", 220)
        await self.writer.start_tls(context, server_hostname=host, ssl_handshake_timeout=TIMEOUT_S)
        self.encrypted = True
        self.phase = SmtpPhase.TLS_READY

    async def data(self, content: bytes) -> None:
        self.expect(SmtpPhase.DATA_READY)
        escaped = b"\r\n".join(
            b"." + line if line.startswith(b".") else line for line in content.split(b"\r\n")
        )
        self.writer.write(escaped.rstrip(b"\r\n") + b"\r\n.\r\n")
        await self.writer.drain()
        await _reply(self.reader, 250)
        self.phase = SmtpPhase.ACCEPTED

    def quit(self) -> None:
        self.expect(SmtpPhase.ACCEPTED)
        # The server's DATA receipt survives a peer closing before QUIT.
        with contextlib.suppress(OSError):
            self.writer.write(b"QUIT\r\n")


async def send(settings: EmailSettings, content: bytes) -> None:
    """Bound the whole conversation, including DNS, TLS, and slow SMTP replies."""
    writer = None
    try:
        async with asyncio.timeout(TIMEOUT_S):
            context = ssl.create_default_context()
            reader, writer = await asyncio.open_connection(
                settings.host,
                settings.port,
                ssl=context if settings.security == "ssl" else None,
                server_hostname=settings.host if settings.security == "ssl" else None,
                limit=8192,
            )
            session = SmtpSession(reader, writer, encrypted=settings.security == "ssl")
            await session.greet()
            capabilities = await session.identify()
            if settings.security == "starttls":
                if not any(line.upper() == b"STARTTLS" for line in capabilities):
                    raise SmtpError("SMTP server does not offer STARTTLS")
                await session.secure(context, settings.host)
                capabilities = await session.identify()
            if settings.username:
                auth = [line.upper().split() for line in capabilities]
                if not any(parts[0:1] == [b"AUTH"] and b"PLAIN" in parts[1:] for parts in auth):
                    raise SmtpError("SMTP server does not offer AUTH PLAIN")
                token = base64.b64encode(
                    ("\0" + settings.username + "\0" + settings.password).encode()
                )
                await session.command(
                    SmtpPhase.IDENTIFIED_TLS, SmtpPhase.AUTHENTICATED, b"AUTH PLAIN " + token, 235
                )
            await session.command(
                (SmtpPhase.IDENTIFIED_TLS, SmtpPhase.AUTHENTICATED),
                SmtpPhase.SENDER_ACCEPTED,
                f"MAIL FROM:<{settings.sender}>".encode(),
                250,
            )
            await session.command(
                SmtpPhase.SENDER_ACCEPTED,
                SmtpPhase.RECIPIENT_ACCEPTED,
                f"RCPT TO:<{settings.recipient}>".encode(),
                250,
            )
            await session.command(SmtpPhase.RECIPIENT_ACCEPTED, SmtpPhase.DATA_READY, b"DATA", 354)
            await session.data(content)
            session.quit()
    finally:
        if writer is not None:
            writer.close()
            # Closing a stuck TLS peer must not delay the core loop or shutdown.
            transport = writer.transport
            transport.abort()


class EmailPoster:
    """A durable auxiliary destination; SMTP acceptance never proves phone delivery."""

    def __init__(self, settings: EmailSettings, now: Callable[[], datetime]) -> None:
        self.settings, self._now = settings, now
        identity = "|".join(
            (
                settings.host,
                str(settings.port),
                settings.security,
                settings.sender,
                settings.recipient,
                settings.username,
            )
        )
        destination = "email:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        self.urls: tuple[str, ...] = (destination,)
        self.channels: dict[str, Literal["primary", "email", "webhook"]] = {destination: "email"}
        self.destination_ids = {destination: url_id(destination)}
        self._attempts = DestinationReservations()
        self.down = self._attempts.down
        self.on_reserve: Callable[[], None] = lambda: None

    def due(self, urls: Collection[str]) -> frozenset[str]:
        return self._attempts.due(set(urls) & set(self.urls), self._now())

    @property
    def failing(self) -> bool:
        return self._attempts.failing(self.urls)

    async def post(
        self, event: AlertEvent, msg_id: str, urls: Collection[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        tried = self._attempts.reserve(
            set(urls) & set(self.urls), self._now(), timedelta(seconds=REST_S)
        )
        if not tried:
            return tried, frozenset()
        destination = self.urls[0]
        accepted = False
        try:
            self.on_reserve()
            await send(self.settings, message(self.settings, event, msg_id))
            accepted = True
        except (OSError, TimeoutError, SmtpError, ValueError):
            log.warning("Email delivery failed; retry scheduled")
            return tried, frozenset()
        finally:
            self._attempts.settle(destination, accepted)
        self.on_reserve()
        return tried, tried
