"""The Docker entry point: settings from env, the runtime, the bot and the web server on one loop,
supervised (7.12 rule 12): a part that ends is a fault and the process exits for a restart."""

import asyncio
import functools
import json
import logging
import os
import re
import secrets
import signal
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import httpx

from train_commute_companion.calendar import Clock, SystemClock
from train_commute_companion.email import EmailPoster, EmailSettings
from train_commute_companion.model import present
from train_commute_companion.providers.base import Provider
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.providers.weather import WeatherProvider
from train_commute_companion.runtime import LOOKAHEAD_AT, Runtime
from train_commute_companion.store import JsonFileStore
from train_commute_companion.webhook import check_secret, check_token, parse_urls
from train_commute_companion.webhook_send import WebhookPoster, send_webhook_tests

from . import owntracks
from .bot import Bot
from .commands import answer
from .email_settings import CopyPort, CopyPorts, email_settings
from .follow import Follower
from .ntfy import NTFY_DESTINATION_PREFIX, NtfyNotifier, NtfyProof, NtfySettings, ntfy_settings
from .telegram import TelegramApi, TelegramNotifier
from .travel_actions import TravelActions, agent_tokens
from .travel_language import LanguageSettings, TravelLanguage, language_settings
from .travel_preferences import TravelPreferences
from .web import health_report, mcp_socket_path, start_readout, start_web, state_dir

log = logging.getLogger(__name__)

PORT = 8080


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set")
    return value


def _optional(env: Mapping[str, str], name: str) -> str | None:
    return env.get(name, "").strip() or None


def _chat_id(env: Mapping[str, str]) -> int | None:
    raw = _optional(env, "TELEGRAM_CHAT_ID")
    if raw is None:
        return None
    try:
        chat_id = int(raw)
    except ValueError:
        raise ValueError("TELEGRAM_CHAT_ID must be a whole number") from None
    if chat_id < 1:  # group and channel ids are negative; there every member could act
        raise ValueError("TELEGRAM_CHAT_ID must be a private chat, not a group")
    return chat_id


def _lookahead_at(env: Mapping[str, str]) -> time:
    raw = _optional(env, "TCC_LOOKAHEAD_AT")
    if raw is None:
        return LOOKAHEAD_AT
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        raise ValueError("TCC_LOOKAHEAD_AT must be HH:MM") from None


@dataclass(frozen=True, slots=True)
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
    mcp_socket: Path | None = None  # None: MCP off
    email: EmailSettings | None = None
    knmi_key: str | None = field(default=None, repr=False)
    mcp_agents: dict[str, str] = field(default_factory=dict, repr=False)
    mcp_remote_port: int | None = None
    mcp_remote_bind: str = "127.0.0.1"
    mcp_remote_hosts: tuple[str, ...] = ()
    travel_language: LanguageSettings | None = None
    ntfy: NtfySettings | None = None

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "Settings":
        """Errors name the variable, never its value."""
        ns_key = _required(env, "NS_API_KEY")
        ntfy = ntfy_settings(env)
        bot_token = _optional(env, "TELEGRAM_BOT_TOKEN") or ""
        if not bot_token and ntfy is None:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set; configure Telegram or ntfy phone push")
        chat_id = _chat_id(env)
        if chat_id is not None and not bot_token:
            raise ValueError("TELEGRAM_CHAT_ID requires TELEGRAM_BOT_TOKEN")
        lookahead_at = _lookahead_at(env)
        health_token = _optional(env, "TCC_HEALTH_TOKEN")
        try:
            webhook_urls = parse_urls(env.get("TCC_WEBHOOK_URLS", ""))
        except ValueError as e:
            raise ValueError(f"TCC_WEBHOOK_URLS {e}") from None
        webhook_secret = webhook_token = ""
        if webhook_urls:  # a secret or token without URLs is ignored
            webhook_secret = _required(env, "TCC_WEBHOOK_SECRET")
            webhook_token = _optional(env, "TCC_WEBHOOK_TOKEN") or ""
        owntracks_secret = _optional(env, "OWNTRACKS_SECRET")
        agents = agent_tokens(env.get("TCC_MCP_AGENTS", ""))
        remote_port, remote_bind, remote_hosts = _remote_mcp_settings(env, agents)
        for name, value, check in (
            ("OWNTRACKS_SECRET", owntracks_secret, check_token),
            ("TCC_HEALTH_TOKEN", health_token, check_token),
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
            owntracks_secret,
            Path(_optional(env, "TCC_PROFILES") or "/config/profiles.yaml"),
            state_dir(env),
            lookahead_at,
            health_token,
            webhook_urls,
            webhook_secret,
            webhook_token,
            mcp_socket_path(env),
            email_settings(env),
            _optional(env, "KNMI_API_KEY"),
            agents,
            remote_port,
            remote_bind,
            remote_hosts,
            language_settings(env),
            ntfy,
        )


def _remote_mcp_settings(
    env: Mapping[str, str], agents: Mapping[str, str]
) -> tuple[int | None, str, tuple[str, ...]]:
    port = _optional(env, "TCC_MCP_REMOTE_PORT")
    if port is None:
        return None, "127.0.0.1", ()
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError("TCC_MCP_REMOTE_PORT must be a port from 1 to 65535")
    if not agents:
        raise ValueError("TCC_MCP_REMOTE_PORT needs at least one TCC_MCP_AGENTS credential")
    bind = _optional(env, "TCC_MCP_REMOTE_BIND") or "127.0.0.1"
    try:
        ip_address(bind)
    except ValueError:
        raise ValueError("TCC_MCP_REMOTE_BIND must be an IP address") from None
    hosts = tuple(host.strip() for host in env.get("TCC_MCP_REMOTE_ALLOWED_HOSTS", "").split(","))
    if any(not re.fullmatch(r"[a-zA-Z0-9.\-:\[\]]{1,253}", host) for host in hosts):
        raise ValueError("TCC_MCP_REMOTE_ALLOWED_HOSTS must list exact host addresses")
    return int(port), bind, hosts


class ProcessPhase(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


def _health_token(settings: Settings) -> str:
    if settings.health_token is not None:
        return settings.health_token
    path = settings.state_dir / "health-token"
    try:
        settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            with tempfile.NamedTemporaryFile(mode="w", dir=settings.state_dir) as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
                # Publish the complete private file without replacing another owner's token.
                os.link(stream.name, path)
            log.info("Generated TCC_HEALTH_TOKEN: %s (saved privately in %s)", token, path)
            return token
        with os.fdopen(fd, encoding="ascii") as stored:
            info = os.fstat(stored.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError("unsafe token file")
            token = stored.read(257)
            if len(token) > 256:
                raise ValueError("oversized token file")
            check_token(token)
            return token
    except (OSError, ValueError):
        raise ValueError(
            "cannot load or create private health-token in TCC_STATE_DIR; "
            "check storage, file contents and owner-only permissions"
        ) from None


@dataclass
class App:
    settings: Settings
    rt: Runtime
    notifier: TelegramNotifier | NtfyNotifier
    bot: Bot | None
    follower: Follower
    webhooks: CopyPort | None = None
    phase: ProcessPhase = ProcessPhase.NEW
    ntfy_proof: NtfyProof | None = None
    travel_actions: TravelActions | None = None

    @property
    def confirmed(self) -> bool:
        if self.ntfy_proof is not None:
            return self.ntfy_proof.confirmed
        return self.bot is not None and self.bot.confirmed


def build(s: Settings, *, clock: Clock, provider: Provider, client: httpx.AsyncClient) -> App:
    api = TelegramApi(s.bot_token, client) if s.bot_token else None
    telegram = TelegramNotifier(api, s.chat_id, clock.now) if api is not None else None
    notifier = (
        NtfyNotifier(
            s.ntfy, client, clock.now, send_allowed=lambda event: rt.primary_send_allowed(event)
        )
        if s.ntfy is not None
        else telegram
    )
    if notifier is None:
        raise ValueError("configure Telegram or ntfy phone push")
    webhooks: CopyPort | None = None
    webhook_test = None
    if s.webhook_urls:  # the one client serves NS, Telegram and the webhooks
        hook = (client, s.webhook_urls, s.webhook_secret, clock.now)
        webhooks = WebhookPoster(*hook, token=s.webhook_token)
        webhook_test = functools.partial(send_webhook_tests, *hook, token=s.webhook_token)
    if s.email is not None:
        email = EmailPoster(s.email, clock.now)
        webhooks = CopyPorts(webhooks, email) if webhooks is not None else email
    rt = Runtime(
        profile_path=s.profiles,
        store=JsonFileStore(s.state_dir / "tcc-state.json"),
        provider=provider,
        notifier=notifier,
        clock=clock,
        require_stations=True,  # the list is fetched inside run_forever's start, watchdog running
        lookahead_at=s.lookahead_at,
        webhooks=webhooks,
        primary_destination=lambda: notifier.destination_id,
        legacy_adoption_destination=lambda: (
            telegram.destination_id
            if s.ntfy is None and telegram is not None and bot is not None and bot.confirmed
            else None
        ),
        weather=WeatherProvider(client, clock.now, s.knmi_key),
        primary_destination_scope=lambda destination: destination.startswith(
            NTFY_DESTINATION_PREFIX
        ),
        park_primary_pin=lambda destination: (
            destination.startswith("telegram:")
            if s.ntfy is not None
            else destination.startswith(NTFY_DESTINATION_PREFIX)
        ),
    )
    follower = Follower(rt)
    preferences = TravelPreferences(s.state_dir / "travel-preferences.json", rt.now)
    language = s.travel_language
    language_identity = (
        json.dumps([language.mode, str(language.url), language.model, language.token])
        if language is not None
        else None
    )
    actions = TravelActions(
        rt,
        s.mcp_agents,
        s.state_dir / "travel-agent-access.json",
        preferences,
        language_identity=language_identity,
    )
    bot = (
        Bot(
            api,
            rt,
            s.chat_id,
            s.state_dir / "telegram.json",
            follower,
            webhook_test,
            telegram.bind_chat if telegram is not None else None,
            travel_actions=actions,
            travel_language=TravelLanguage(language, client) if language is not None else None,
        )
        if api is not None
        else None
    )
    proof = (
        NtfyProof(notifier, s.state_dir / "ntfy-proof.json", clock.now)
        if isinstance(notifier, NtfyNotifier)
        else None
    )
    return App(s, rt, notifier, bot, follower, webhooks, ntfy_proof=proof, travel_actions=actions)


def mcp_answer(app: App) -> Callable[[object], dict[str, Any]]:
    """The read-out gets a view and the confirmed flag, never the runtime or the settings."""
    return lambda req: answer(app.rt.view(), app.confirmed, req)


async def dispatch_mcp(app: App, request: dict[str, Any], token: str | None) -> dict[str, Any]:
    if request.get("tool") in ("get_status", "get_next_connections", "get_works_ahead"):
        return mcp_answer(app)(request)
    if app.bot is None:
        return {"status": "denied", "text": "Connect Telegram to manage travel plans."}
    return await app.bot.travel_actions.dispatch(request, token, app.bot.prompt_travel_approval)


async def serve(app: App, port: int) -> int:
    """Returns 1 when any part ends (Docker restarts the container); cancelled, it cleans up
    the same way and re-raises."""
    if app.phase is not ProcessPhase.NEW:
        raise RuntimeError("a stopped or running app requires a new owner")
    app.phase = ProcessPhase.STARTING
    before = asyncio.all_tasks()
    runtime = asyncio.create_task(app.rt.run_forever(), name="runtime")
    server: asyncio.Server | None = None
    readout: asyncio.Server | None = None
    remote = AsyncExitStack()
    dispatch = functools.partial(dispatch_mcp, app)
    try:
        secret = app.settings.owntracks_secret
        server = await start_web(
            port,
            health=lambda: health_report(
                app.rt.view(),
                not runtime.done(),
                app.confirmed,
                app.webhooks.failing if app.webhooks else None,
            ),
            owntracks=owntracks.handler(app.follower) if secret else None,
            secret=secret,
            health_token=app.settings.health_token,
            today=lambda: (app.rt.view(), app.confirmed),
            delivery=app.ntfy_proof,
        )
        if app.settings.mcp_socket is not None:
            readout = await start_readout(
                app.settings.mcp_socket,
                mcp_answer(app),
                actions=dispatch if app.settings.mcp_agents and app.bot is not None else None,
            )
        actions = app.travel_actions
        if app.settings.mcp_remote_port is not None and actions is not None:
            from .mcp_remote import serve_remote

            try:
                await remote.enter_async_context(
                    serve_remote(
                        dispatch,
                        lambda token: actions.agent(token) is not None,
                        host=app.settings.mcp_remote_bind,
                        port=app.settings.mcp_remote_port,
                        allowed_hosts=app.settings.mcp_remote_hosts,
                    )
                )
            except (OSError, RuntimeError, ValueError) as error:
                log.warning(
                    "Remote MCP did not start (%s); local monitoring continues.",
                    type(error).__name__,
                )
        workers = {runtime}
        if app.bot is not None:
            workers.add(asyncio.create_task(app.bot.run(), name="telegram"))
        app.phase = ProcessPhase.RUNNING
        done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if not t.cancelled() and (e := t.exception()) is not None:
                log.error("%s stopped", t.get_name(), exc_info=e)
            else:
                log.error("%s stopped", t.get_name())
        return 1
    finally:
        app.phase = ProcessPhase.STOPPING
        try:
            try:
                await remote.aclose()
            finally:
                await _clean_up(app, before, server, readout)
        except BaseException:
            app.phase = ProcessPhase.FAILED
            raise
        else:
            app.phase = ProcessPhase.STOPPED


async def _clean_up(
    app: App,
    before: set[asyncio.Task[object]],
    server: asyncio.Server | None,
    readout: asyncio.Server | None,
) -> None:
    app.notifier.retire()
    for srv in (server, readout):
        if srv is not None:
            srv.close()
    # Server.close leaves open connection handlers running: every task begun here goes,
    # except notification saves, whose worker threads outlive cancellation and must be drained
    rest = (
        asyncio.all_tasks()
        - before
        - {asyncio.current_task()}
        - (app.bot.writes if app.bot else set())
        - (app.ntfy_proof.writes if app.ntfy_proof else set())
    )
    for t in rest:
        t.cancel()
    await asyncio.gather(*rest, return_exceptions=True)
    if app.bot is not None:
        await app.bot.close()
    if app.ntfy_proof is not None:
        await app.ntfy_proof.close()
    await app.follower.close()
    for srv in (server, readout):
        if srv is not None:
            await srv.wait_closed()
    sock = app.settings.mcp_socket
    if readout is not None and sock is not None and sock.is_socket():
        sock.unlink(missing_ok=True)  # never a regular file someone put there


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request URL at INFO, and Telegram's hold the bot token
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def _run(s: Settings) -> int:
    try:
        s = replace(s, health_token=_health_token(s))
    except ValueError as error:
        log.error("%s; refusing to start", error)
        return 1
    me = present(asyncio.current_task())  # a coroutine run by asyncio.run is always in a task
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
