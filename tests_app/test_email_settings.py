from datetime import UTC

import pytest
from tcc_app.email_settings import email_settings


def test_disabled_and_partial_settings():
    assert email_settings({}) is None
    with pytest.raises(ValueError, match="SMTP_HOST"):
        email_settings({"SMTP_PASSWORD": "do-not-print"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("SMTP_SECURITY", "none"),
        ("SMTP_FROM", "a@b\r\nBcc: x@y"),
        ("SMTP_TO", "first@example.com,second@example.com"),
        ("SMTP_TO", "a@"),
        ("SMTP_TO", "@private-domain"),
        ("SMTP_TO", "a@@private-domain"),
        ("SMTP_FROM", "a@"),
        ("SMTP_PORT", "0"),
    ],
)
def test_invalid_email_settings_do_not_echo_values(field, value):
    env = {
        "SMTP_HOST": "mail.example.com",
        "SMTP_SECURITY": "starttls",
        "SMTP_FROM": "sender@example.com",
        "SMTP_TO": "receiver@example.com",
    }
    env[field] = value
    with pytest.raises(ValueError) as error:
        email_settings(env)
    assert field in str(error.value)
    assert value not in str(error.value)


def test_settings_credentials_are_not_represented():
    settings = email_settings(
        {
            "SMTP_HOST": "mail.example.com",
            "SMTP_SECURITY": "ssl",
            "SMTP_FROM": "sender@example.com",
            "SMTP_TO": "receiver@example.com",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": "private-password",
        }
    )
    assert settings.port == 465
    assert "private-password" not in repr(settings)


async def test_durable_email_retry_does_not_repeat_successful_phone_or_webhook(
    tmp_path, monkeypatch
):
    import json

    import httpx
    import runtime_seam
    from builders import make_connection
    from lifecycle_fixtures import L1X, L2
    from runtime_rig import Rig
    from tcc_app.email_settings import CopyPorts

    from train_commute_companion import email
    from train_commute_companion.model import EventKind
    from train_commute_companion.runtime import Runtime
    from train_commute_companion.webhook_send import WebhookPoster

    rig = Rig(tmp_path)
    webhook_events, email_events = [], []
    fail = True

    async def mail(settings, content):
        email_events.append(content)
        if fail:
            raise OSError("offline")

    def hook(request):
        webhook_events.append(json.loads(request.content))
        return httpx.Response(200)

    monkeypatch.setattr(email, "send", mail)
    settings = email.EmailSettings("localhost", 465, "ssl", "s@example.com", "r@example.com")
    async with httpx.AsyncClient(transport=httpx.MockTransport(hook)) as client:

        def runtime():
            web = WebhookPoster(
                client, ("https://receiver.invalid",), "a-secret-for-tests", rig.clock.now
            )
            poster = email.EmailPoster(settings, rig.clock.now)
            return Runtime(
                profile_path=rig.path,
                store=rig.store,
                provider=rig.provider,
                notifier=rig.notifier,
                clock=rig.clock,
                require_stations=False,
                webhooks=CopyPorts(web, poster),
            )

        rig.rt = runtime()
        await rig.rt.start()
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        rig.clock.set("07:52")
        rig.provider.world = [make_connection([L1X, L2])]
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        count = lambda: sum(e["kind"] == "cancelled" for e in webhook_events)
        assert count() == 1
        assert sum(e.kind is EventKind.CANCELLED for e in rig.notifier.attempts) == 1
        assert runtime_seam.owed_copies(rig.rt)
        # Recreate both destination adapters and restore the encoded state file.
        fail = False
        rig.rt = runtime()
        await rig.rt.start()
        rig.clock.jump(2)
        await rig.rt.watchdog_once()
        await rig.rt.webhooks_idle()
        assert not runtime_seam.owed_copies(rig.rt)
        assert count() == 1
        assert sum(e.kind is EventKind.CANCELLED for e in rig.notifier.attempts) == 1
        assert any(b"cancelled" in body.lower() for body in email_events)


def test_app_settings_reject_partial_smtp_configuration():
    from tcc_app.main import Settings

    with pytest.raises(ValueError, match="SMTP_SECURITY"):
        Settings.from_env(
            {"NS_API_KEY": "test", "TELEGRAM_BOT_TOKEN": "test", "SMTP_HOST": "localhost"}
        )


async def test_app_build_attaches_native_email_without_webhook_url_parsing(tmp_path):
    import httpx
    from runtime_rig import FakeClock, Provider, at
    from tcc_app.main import Settings, build

    from train_commute_companion.email import EmailPoster

    settings = Settings.from_env(
        {
            "NS_API_KEY": "test",
            "TELEGRAM_BOT_TOKEN": "test",
            "SMTP_HOST": "localhost",
            "SMTP_SECURITY": "ssl",
            "SMTP_FROM": "s@example.com",
            "SMTP_TO": "r@example.com",
            "TCC_STATE_DIR": str(tmp_path),
        }
    )
    clock = FakeClock(at("07:50"))
    client = httpx.AsyncClient()
    app = build(settings, clock=clock, provider=Provider(clock, []), client=client)
    assert isinstance(app.webhooks, EmailPoster)
    assert tuple(app.webhooks.channels.values()) == ("email",)
    assert app.webhooks.urls[0].startswith("email:")
    assert "r@example.com" not in app.webhooks.urls[0]
    await client.aclose()


async def test_email_receipt_does_not_acknowledge_failed_phone(tmp_path, monkeypatch):
    from builders import make_connection
    from lifecycle_fixtures import L1X, L2
    from runtime_rig import Rig

    from train_commute_companion import email
    from train_commute_companion.model import EventKind
    from train_commute_companion.runtime import Runtime

    rig = Rig(tmp_path)
    sent = []

    async def mail(settings, content):
        sent.append(content)

    monkeypatch.setattr(email, "send", mail)
    settings = email.EmailSettings("localhost", 465, "ssl", "s@example.com", "r@example.com")
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        webhooks=email.EmailPoster(settings, rig.clock.now),
    )
    await rig.rt.start()
    await rig.rt.poll_once()
    await rig.rt.webhooks_idle()
    sent.clear()
    rig.notifier.fails = lambda event: event.kind is EventKind.CANCELLED
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([L1X, L2])]
    await rig.rt.poll_once()
    await rig.rt.webhooks_idle()
    assert rig.rt.view().undelivered == 1
    assert sum(b"cancelled" in body.lower() for body in sent) == 1
    rig.clock.jump(2)
    await rig.rt.watchdog_once()
    await rig.rt.webhooks_idle()
    assert rig.rt.view().undelivered == 1
    assert sum(b"cancelled" in body.lower() for body in sent) == 1


async def test_old_copy_never_acquires_new_email_recipient_while_webhook_pending(
    tmp_path, monkeypatch
):
    import json

    import httpx
    import runtime_seam
    from builders import make_connection
    from lifecycle_fixtures import L1X, L2
    from runtime_rig import Rig
    from tcc_app.email_settings import CopyPorts

    from train_commute_companion import email
    from train_commute_companion.runtime import Runtime
    from train_commute_companion.webhook_send import WebhookPoster

    rig = Rig(tmp_path)
    emails = []

    async def mail(settings, content):
        emails.append((settings.recipient, content))
        raise OSError("offline")

    monkeypatch.setattr(email, "send", mail)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:

        def runtime(recipient):
            web = WebhookPoster(client, ("https://receiver.invalid",), "test-secret", rig.clock.now)
            port = (
                CopyPorts(
                    web,
                    email.EmailPoster(
                        email.EmailSettings("localhost", 465, "ssl", "s@example.com", recipient),
                        rig.clock.now,
                    ),
                )
                if recipient
                else web
            )
            return Runtime(
                profile_path=rig.path,
                store=rig.store,
                provider=rig.provider,
                notifier=rig.notifier,
                clock=rig.clock,
                require_stations=False,
                webhooks=port,
            )

        rig.rt = runtime("old@example.com")
        await rig.rt.start()
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        rig.clock.set("07:52")
        rig.provider.world = [make_connection([L1X, L2])]
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        assert runtime_seam.owed_copies(rig.rt)
        assert any(recipient == "old@example.com" for recipient, _ in emails)
        old_ids = {
            line
            for _, body in emails
            for line in body.splitlines()
            if line.startswith(b"Message-ID:")
        }
        # Disabling SMTP does not erase copies still owed to the failing webhook.
        rig.rt = runtime(None)
        await rig.rt.start()
        rig.clock.jump(2)
        await rig.rt.watchdog_once()
        await rig.rt.webhooks_idle()
        assert runtime_seam.owed_copies(rig.rt)
        rig.rt = runtime("new@example.com")
        await rig.rt.start()
        rig.clock.jump(2)
        await rig.rt.watchdog_once()
        await rig.rt.webhooks_idle()
        assert not any(
            line in old_ids
            for recipient, body in emails
            if recipient == "new@example.com"
            for line in body.splitlines()
            if line.startswith(b"Message-ID:")
        )
        assert all(c.target_ids is not None for c in runtime_seam.owed_copies(rig.rt).values())
        assert "old@example.com" not in rig.store.text
        assert "new@example.com" not in rig.store.text
        assert json.loads(rig.store.text)


async def test_cancellation_during_smtp_retains_webhook_receipt_across_restart(
    tmp_path, monkeypatch
):
    import asyncio
    import json

    import httpx
    import runtime_seam
    from runtime_rig import Rig
    from tcc_app.email_settings import CopyPorts

    from train_commute_companion import email
    from train_commute_companion.runtime import Runtime
    from train_commute_companion.webhook import transport_id
    from train_commute_companion.webhook_send import WebhookPoster

    rig = Rig(tmp_path)
    blocked, release = asyncio.Event(), asyncio.Event()
    hooks = []

    async def mail(settings, content):
        blocked.set()
        await release.wait()

    def hook(request):
        hooks.append(json.loads(request.content))
        return httpx.Response(200)

    monkeypatch.setattr(email, "send", mail)
    async with httpx.AsyncClient(transport=httpx.MockTransport(hook)) as client:

        def runtime():
            web = WebhookPoster(client, ("https://receiver.invalid",), "test-secret", rig.clock.now)
            settings = email.EmailSettings(
                "localhost", 465, "ssl", "s@example.com", "r@example.com"
            )
            return Runtime(
                profile_path=rig.path,
                store=rig.store,
                provider=rig.provider,
                notifier=rig.notifier,
                clock=rig.clock,
                require_stations=False,
                webhooks=CopyPorts(web, email.EmailPoster(settings, rig.clock.now)),
            )

        rig.rt = runtime()
        await rig.rt.start()
        await rig.rt.poll_once()
        await asyncio.wait_for(blocked.wait(), 1)
        # Allow the successful webhook receipt to be processed while SMTP remains blocked.
        for _ in range(10):
            await asyncio.sleep(0)
        await runtime_seam.stop_webhooks(rig.rt)
        saved = rig.store.load()
        assert saved.webhook_copies
        assert all(
            transport_id("https://receiver.invalid", "test-secret") in c.done
            for c in saved.webhook_copies
        )
        count = len(hooks)
        release.set()
        rig.rt = runtime()
        await rig.rt.start()
        rig.clock.jump(2)
        await rig.rt.watchdog_once()
        await rig.rt.webhooks_idle()
        assert len(hooks) == count
        assert not runtime_seam.owed_copies(rig.rt)


def test_copy_ports_refuse_ambiguous_duplicate_destinations():
    from datetime import datetime

    from tcc_app.email_settings import CopyPorts

    from train_commute_companion.email import EmailPoster, EmailSettings

    settings = EmailSettings(
        "mail.invalid", 465, "ssl", "sender@example.com", "recipient@example.com"
    )
    port = EmailPoster(settings, lambda: datetime.now(UTC))
    with pytest.raises(ValueError, match="distinct across transports"):
        CopyPorts(port, port)
