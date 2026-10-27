"""The Docker entry point: settings from env, the runtime, the bot and the web server on one loop,
supervised (7.12 rule 12): a part that ends is a fault and the process exits for a restart."""

import asyncio
import functools
import logging
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import httpx

from train_commute_companion.providers.base import Provider
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.runtime import (
    LOOKAHEAD_AT,
    Clock,
    Runtime,
    SystemClock,
    WebhookPoster,
    send_webhook_tests,
)
from train_commute_companion.store import JsonFileStore
from train_commute_companion.webhook import check_secret, check_token, parse_urls

from . import owntracks
from .bot import Bot
from .follow import Follower
from .telegram import TelegramApi, TelegramNotifier
from .web import health_report, start_web

log = logging.getLogger(__name__)

PORT = 8080


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set")
    return value


def _optional(env: Mapping[str, str], name: str) -> str | None:
    return env.get(name, "").strip() or None


@dataclass(frozen=True)
class Settings:
    ns_key: str
    bot_token: str
    chat_id: int | None
    owntracks_secret: str | None
    profiles: Path
    state_dir: Path
    lookahead_at: time = LOOKAHEAD_AT
    health_token: str | None = None
    webhook_urls: tuple[str, ...] = ()
    webhook_secret: str = ""  # empty: unset
    webhook_token: str = ""

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "Settings":
        """Errors name the variable, never its value."""
        ns_key = _required(env, "NS_API_KEY")
        bot_token = _required(env, "TELEGRAM_BOT_TOKEN")
        chat_id = None
        if (raw := _optional(env, "TELEGRAM_CHAT_ID")) is not None:
            try:
                chat_id = int(raw)
            except ValueError:
                raise ValueError("TELEGRAM_CHAT_ID must be a whole number") from None
        lookahead_at = LOOKAHEAD_AT
        if (raw := _optional(env, "TCC_LOOKAHEAD_AT")) is not None:
            try:
                lookahead_at = datetime.strptime(raw, "%H:%M").time()
            except ValueError:
                raise ValueError("TCC_LOOKAHEAD_AT must be HH:MM") from None
        health_token = _optional(env, "TCC_HEALTH_TOKEN")
        # headers are parsed as stripped Latin-1, so anything else could never match
        if health_token is not None and not (
            len(health_token) >= 16
            and health_token.isascii()
            and health_token.isprintable()
            and " " not in health_token
        ):
            raise ValueError(
                "TCC_HEALTH_TOKEN must be at least 16 characters of printable ASCII without spaces"
            )
        try:
            webhook_urls = parse_urls(env.get("TCC_WEBHOOK_URLS", ""))
        except ValueError as e:
            raise ValueError(f"TCC_WEBHOOK_URLS {e}") from None
        webhook_secret = webhook_token = ""
        if webhook_urls:  # a secret or token without URLs is ignored
            webhook_secret = _required(env, "TCC_WEBHOOK_SECRET")
            webhook_token = _optional(env, "TCC_WEBHOOK_TOKEN") or ""
            for name, value, check in (
                ("TCC_WEBHOOK_SECRET", webhook_secret, check_secret),
                ("TCC_WEBHOOK_TOKEN", webhook_token, check_token),
            ):
                try:
                    if value:
                        check(value)
                except ValueError as e:
                    raise ValueError(f"{name} {e}") from None
        return Settings(
            ns_key,
            bot_token,
            chat_id,
            _optional(env, "OWNTRACKS_SECRET"),
            Path(_optional(env, "TCC_PROFILES") or "/config/profiles.yaml"),
            Path(_optional(env, "TCC_STATE_DIR") or "/data"),
            lookahead_at,
            health_token,
            webhook_urls,
            webhook_secret,
            webhook_token,
        )


@dataclass
class App:
    settings: Settings
    rt: Runtime
    notifier: TelegramNotifier
    bot: Bot
    follower: Follower
    webhooks: WebhookPoster | None = None


def build(s: Settings, *, clock: Clock, provider: Provider, client: httpx.AsyncClient) -> App:
    api = TelegramApi(s.bot_token, client)
    notifier = TelegramNotifier(api, s.chat_id, clock.now)
    webhooks, webhook_test = None, None
    if s.webhook_urls:  # the one client serves NS, Telegram and the webhooks
        hook = (client, s.webhook_urls, s.webhook_secret, clock.now)
        webhooks = WebhookPoster(*hook, token=s.webhook_token)
        webhook_test = functools.partial(send_webhook_tests, *hook, token=s.webhook_token)
    rt = Runtime(
        profile_path=s.profiles,
        store=JsonFileStore(s.state_dir / "tcc-state.json"),
        provider=provider,
        notifier=notifier,
        clock=clock,
        known_stations=None,
        lookahead_at=s.lookahead_at,
        webhooks=webhooks,
    )
    # the station list is fetched inside run_forever's start, with the watchdog running
    rt.require_stations = True
    # NsProvider fills `positions` with the station list; a test provider may have none
    follower = Follower(rt, lambda: getattr(provider, "positions", {}))
    bot = Bot(api, rt, s.chat_id, s.state_dir / "telegram.json", follower, webhook_test)
    return App(s, rt, notifier, bot, follower, webhooks)


async def serve(app: App, port: int) -> int:
    """Returns 1 when any part ends (Docker restarts the container); cancelled, it cleans up
    the same way and re-raises."""
    before = asyncio.all_tasks()
    runtime = asyncio.create_task(app.rt.run_forever(), name="runtime")
    server: asyncio.Server | None = None
    try:
        secret = app.settings.owntracks_secret
        server = await start_web(
            port,
            health=lambda: health_report(
                app.rt.view(),
                not runtime.done(),
                app.bot.confirmed,
                app.webhooks.failing if app.webhooks else None,
            ),
            owntracks=owntracks.handler(app.follower) if secret else None,
            secret=secret,
            health_token=app.settings.health_token,
        )
        bot = asyncio.create_task(app.bot.run(), name="telegram")
        done, _ = await asyncio.wait((runtime, bot), return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if not t.cancelled() and (e := t.exception()) is not None:
                log.error("%s stopped", t.get_name(), exc_info=e)
            else:
                log.error("%s stopped", t.get_name())
        return 1
    finally:
        if server is not None:
            server.close()
        # run_forever's gather leaves the watchdog running when start raises, and
        # Server.close leaves open connection handlers running: every task begun here goes
        rest = asyncio.all_tasks() - before - {asyncio.current_task()}
        for t in rest:
            t.cancel()
        await asyncio.gather(*rest, return_exceptions=True)
        await app.bot.close()
        await app.follower.close()
        if server is not None:
            await server.wait_closed()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request URL at INFO, and Telegram's hold the bot token
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def _run(s: Settings) -> int:
    me = asyncio.current_task()
    assert me is not None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, me.cancel)
    async with httpx.AsyncClient() as client:  # one client for NS, Telegram and webhooks
        clock = SystemClock()
        app = build(s, clock=clock, provider=NsProvider(s.ns_key, client, clock.now), client=client)
        try:
            return await serve(app, PORT)
        except asyncio.CancelledError:
            log.info("stopped")
            return 0


def main() -> int:
    try:
        s = Settings.from_env(os.environ)
    except ValueError as e:
        print(f"{e}; refusing to start")
        return 1
    setup_logging()
    return asyncio.run(_run(s))
