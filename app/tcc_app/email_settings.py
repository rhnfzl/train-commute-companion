"""Email configuration and independent receipts for optional delivery destinations."""

from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Literal, Protocol, cast

from train_commute_companion.email import EmailSettings
from train_commute_companion.model import AlertEvent
from train_commute_companion.notify import WebhookPort


class CopyPort(WebhookPort, Protocol):
    @property
    def failing(self) -> bool: ...


def email_settings(env: Mapping[str, str]) -> EmailSettings | None:
    names = ("HOST", "PORT", "SECURITY", "FROM", "TO", "USERNAME", "PASSWORD")
    if not any(env.get("SMTP_" + name, "") for name in names):
        return None

    def required(name: str) -> str:
        value = env.get(name, "")
        if not value:
            raise ValueError(f"{name} is not set")
        return value

    host, security = required("SMTP_HOST"), required("SMTP_SECURITY")
    try:
        port = int(env.get("SMTP_PORT", "") or ("465" if security == "ssl" else "587"))
    except ValueError:
        raise ValueError("SMTP_PORT must be a whole number") from None
    return EmailSettings(
        host,
        port,
        cast(Literal["ssl", "starttls"], security),
        required("SMTP_FROM"),
        required("SMTP_TO"),
        env.get("SMTP_USERNAME", ""),
        env.get("SMTP_PASSWORD", ""),
    )


class CopyPorts:
    """Keep separate destination receipts while sharing the durable retry clock."""

    def __init__(self, *ports: CopyPort) -> None:
        self.ports = ports
        self.urls = tuple(url for port in ports for url in port.urls)
        if len(set(self.urls)) != len(self.urls):
            raise ValueError("copy destinations must be distinct across transports")
        self.channels = {url: channel for port in ports for url, channel in port.channels.items()}
        self.destination_ids = {
            url: identity for port in ports for url, identity in port.destination_ids.items()
        }
        self.down: dict[str, datetime] = {}
        for port in ports:
            port.down = self.down
        self.on_reserve = lambda: None

    @property
    def on_reserve(self) -> Callable[[], None]:
        return self._on_reserve

    @on_reserve.setter
    def on_reserve(self, callback: Callable[[], None]) -> None:
        self._on_reserve = callback
        for port in self.ports:
            port.on_reserve = callback

    def due(self, urls: Collection[str]) -> frozenset[str]:
        return frozenset(
            url for port in self.ports for url in port.due(set(urls).intersection(port.urls))
        )

    @property
    def failing(self) -> bool:
        return any(port.failing for port in self.ports)

    async def post(
        self, event: AlertEvent, msg_id: str, urls: Collection[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        tried: set[str] = set()
        accepted: set[str] = set()
        for port in self.ports:
            selected = set(urls).intersection(port.urls)
            if selected:
                attempts, receipts = await port.post(event, msg_id, selected)
                tried.update(attempts)
                accepted.update(receipts)
        return frozenset(tried), frozenset(accepted)


async def test_email(settings: EmailSettings) -> tuple[str, str]:
    """Test both labels without changing durable receipts or phone confirmation."""
    from datetime import UTC

    from train_commute_companion.email import SmtpError, message, send
    from train_commute_companion.model import EventKind, Severity

    results = []
    for severity in (Severity.LOUD, Severity.SILENT):
        now = datetime.now(UTC)
        event = AlertEvent(
            "test",
            "Email setup test",
            None,
            None,
            None,
            EventKind.ALL_GOOD,
            severity,
            None,
            "This is an email setup test.",
            "email-test",
            now,
        )
        try:
            await send(
                settings, message(settings, event, f"test-{severity.value}-{now.isoformat()}")
            )
        except (OSError, TimeoutError, SmtpError, ValueError):
            results.append("failed")
        else:
            results.append("accepted by SMTP server")
    return results[0], results[1]


def main() -> int:
    """A separate email test keeps SMTP acceptance apart from phone confirmation."""
    import argparse
    import asyncio
    import os

    parser = argparse.ArgumentParser(description="Send loud and silent email setup tests")
    parser.add_argument("--test", action="store_true", required=True)
    parser.parse_args()
    try:
        settings = email_settings(os.environ)
        if settings is None:
            raise ValueError("SMTP_HOST is not set")
    except ValueError as error:
        print(f"{error}; refusing to send")
        return 1
    loud, silent = asyncio.run(test_email(settings))
    print(f"Email: loud {loud}; silent {silent}. Check your inbox and spam folder.")
    return int("failed" in (loud, silent))


if __name__ == "__main__":
    raise SystemExit(main())
