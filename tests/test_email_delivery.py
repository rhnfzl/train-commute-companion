"""Exercise the SMTP wire over localhost TLS, with no external recipient."""

import asyncio
import ssl
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import default

import pytest

from train_commute_companion import email
from train_commute_companion.model import AlertEvent, EventKind, Severity

NOW = datetime(2026, 11, 6, tzinfo=UTC)
EVENT = AlertEvent(
    "office",
    "Office\r\nBcc: x@y",
    None,
    None,
    None,
    EventKind.DELAYED,
    Severity.LOUD,
    None,
    ".Train delayed\nPrivate route UT to RTD",
    "delay",
    NOW,
)


@pytest.fixture
def tls(tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    return context


@pytest.mark.parametrize("security", ["ssl", "starttls"])
async def test_tls_smtp_envelope_auth_and_wire_message(tls, security):
    received, commands = [], []
    finished = asyncio.Event()

    async def handle(reader, writer):
        writer.write(b"220 localhost\r\n")
        await writer.drain()
        while line := await reader.readline():
            commands.append(line)
            if line.startswith(b"EHLO"):
                writer.write(b"250-localhost\r\n250-STARTTLS\r\n250 AUTH PLAIN\r\n")
            elif line == b"STARTTLS\r\n":
                writer.write(b"220 proceed\r\n")
                await writer.drain()
                await writer.start_tls(tls)
                continue
            elif line.startswith(b"AUTH"):
                assert writer.get_extra_info("ssl_object") is not None
                writer.write(b"235 authenticated\r\n")
            elif line == b"DATA\r\n":
                writer.write(b"354 continue\r\n")
                await writer.drain()
                body = await reader.readuntil(b"\r\n.\r\n")
                received.append(body[:-5] + b"\r\n")
                writer.write(b"250 accepted\r\n")
                finished.set()
            elif line == b"QUIT\r\n":
                break
            else:
                writer.write(b"250 accepted\r\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(
        handle, "127.0.0.1", 0, ssl=tls if security == "ssl" else None
    )
    async with server:
        settings = email.EmailSettings(
            "localhost",
            server.sockets[0].getsockname()[1],
            security,
            "sender@example.com",
            "recipient@example.com",
            "u",
            "p",
        )
        await email.send(settings, email.message(settings, EVENT, "stable-key"))
        await asyncio.wait_for(finished.wait(), 1)
    assert b"MAIL FROM:<sender@example.com>\r\n" in commands
    assert b"RCPT TO:<recipient@example.com>\r\n" in commands
    parsed = BytesParser(policy=default).parsebytes(received[0])
    assert parsed["To"] == "recipient@example.com"
    assert parsed["Bcc"] is None
    assert parsed["Subject"].endswith("loud update")
    assert "Private route UT to RTD" in parsed.get_content()
    assert b"\r\n..Train delayed\r\n" in received[0]
    assert (
        parsed["Message-ID"]
        == BytesParser(policy=default).parsebytes(email.message(settings, EVENT, "stable-key"))[
            "Message-ID"
        ]
    )


async def test_starttls_required_and_total_timeout(monkeypatch):
    commands = []

    async def no_tls(reader, writer):
        writer.write(b"220 localhost\r\n")
        await writer.drain()
        commands.append(await reader.readline())
        writer.write(b"250 no TLS\r\n")
        await writer.drain()
        assert await reader.read() == b""
        writer.close()

    server = await asyncio.start_server(no_tls, "127.0.0.1", 0)
    async with server:
        settings = email.EmailSettings(
            "localhost",
            server.sockets[0].getsockname()[1],
            "starttls",
            "sender@example.com",
            "recipient@example.com",
            "u",
            "p",
        )
        with pytest.raises(email.SmtpError):
            await email.send(settings, email.message(settings, EVENT, "id"))
    assert commands == [b"EHLO tcc.invalid\r\n"]

    closed = asyncio.Event()

    async def stall(reader, writer):
        await reader.read()
        writer.close()
        closed.set()

    server = await asyncio.start_server(stall, "127.0.0.1", 0)
    context = ssl.create_default_context()
    monkeypatch.setattr(email, "TIMEOUT_S", 0.05)
    async with server:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )

        async def established(*args, **kwargs):
            return reader, writer

        # This case tests an owned stream stalled on a reply, not DNS/startup timing.
        monkeypatch.setattr(asyncio, "open_connection", established)
        monkeypatch.setattr(email.ssl, "create_default_context", lambda: context)
        with pytest.raises(TimeoutError):
            await email.send(replace(settings, port=server.sockets[0].getsockname()[1]), b"")
        await asyncio.wait_for(closed.wait(), 1)


async def test_connection_timeout_before_transport_ownership(monkeypatch):
    entered = asyncio.Event()
    context = ssl.create_default_context()

    async def connecting(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(email, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(asyncio, "open_connection", connecting)
    monkeypatch.setattr(email.ssl, "create_default_context", lambda: context)
    settings = email.EmailSettings("localhost", 465, "ssl", "s@example.com", "r@example.com")
    with pytest.raises(TimeoutError):
        await email.send(settings, b"private text")
    assert entered.is_set()


async def test_poster_retry_and_safe_logging(monkeypatch, caplog):
    now = NOW
    poster = email.EmailPoster(
        email.EmailSettings("localhost", 465, "ssl", "sender@example.com", "recipient@example.com"),
        lambda: now,
    )
    attempts = []

    async def transport(settings, content):
        attempts.append(content)
        if len(attempts) == 1:
            raise OSError("private-password recipient@example.com")

    monkeypatch.setattr(email, "send", transport)
    tried, accepted = await poster.post(EVENT, "stable-key", poster.urls)
    assert tried and not accepted and poster.failing
    assert "private-password" not in caplog.text and "recipient@example.com" not in caplog.text
    assert await poster.post(EVENT, "stable-key", poster.urls) == (frozenset(), frozenset())
    now += timedelta(seconds=61)
    assert (await poster.post(EVENT, "stable-key", poster.urls))[1] == frozenset(poster.urls)
    assert not poster.failing
    assert attempts[0] == attempts[1]


async def test_untrusted_certificate_never_sends_credentials(tls, monkeypatch):
    # Remove the test trust root. The self-signed server must fail verification.
    monkeypatch.delenv("SSL_CERT_FILE")
    received = []

    async def handle(reader, writer):
        received.append(await reader.read())
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)
    async with server:
        settings = email.EmailSettings(
            "localhost",
            server.sockets[0].getsockname()[1],
            "ssl",
            "s@example.com",
            "r@example.com",
            "secret-user",
            "secret-pass",
        )
        with pytest.raises(ssl.SSLCertVerificationError):
            await email.send(settings, b"private commute")
    assert not received


@pytest.mark.parametrize(
    "reply",
    [
        b"250-" + b"x" * 4097 + b"\r\n",
        b"250-more\r\n" * 101,
        b"250-first\r\n550 rejected private text\r\n",
    ],
)
async def test_reply_limits_and_consistent_codes(reply):
    reader = asyncio.StreamReader()
    reader.feed_data(reply)
    reader.feed_eof()
    with pytest.raises(email.SmtpError) as error:
        await email._reply(reader, 250)
    assert "private text" not in str(error.value)


async def test_setup_test_reports_each_severity_without_confirming_phone(monkeypatch):
    from tcc_app.email_settings import test_email

    attempts = []

    async def transport(settings, content):
        attempts.append(content)
        if len(attempts) == 1:
            raise email.SmtpError("private server text")

    monkeypatch.setattr(email, "send", transport)
    settings = email.EmailSettings("localhost", 465, "ssl", "s@example.com", "r@example.com")
    assert await test_email(settings) == ("failed", "accepted by SMTP server")
    parsed = [BytesParser(policy=default).parsebytes(body) for body in attempts]
    assert [str(body["Subject"]).split()[-2] for body in parsed] == ["loud", "silent"]


async def test_data_acceptance_survives_quit_write_failure(monkeypatch):
    class Writer:
        def __init__(self):
            self.transport = self
            self.closed = False

        def write(self, data):
            if data == b"QUIT\r\n":
                raise OSError("peer closed after accepting DATA")

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        def abort(self):
            pass

    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(
        b"220 ready\r\n250 hello\r\n250 sender\r\n250 recipient\r\n354 data\r\n250 accepted\r\n"
    )

    async def connect(*args, **kwargs):
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    settings = email.EmailSettings("localhost", 465, "ssl", "s@example.com", "r@example.com")
    await email.send(settings, b"private text")
    assert writer.closed


async def test_smtp_cannot_send_credentials_before_encrypted_identification():
    class Writer:
        def __init__(self):
            self.sent = []

        def write(self, data):
            self.sent.append(data)

        async def drain(self):
            pass

    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(b"220 ready\r\n250 hello\r\n")
    session = email.SmtpSession(reader, writer, encrypted=False)
    await session.greet()
    await session.identify()
    with pytest.raises(email.SmtpError, match="phase"):
        await session.command(
            email.SmtpPhase.IDENTIFIED_TLS,
            email.SmtpPhase.AUTHENTICATED,
            b"AUTH PLAIN private",
            235,
        )
    assert session.phase is email.SmtpPhase.IDENTIFIED_CLEAR
    assert writer.sent == [b"EHLO tcc.invalid\r\n"]
