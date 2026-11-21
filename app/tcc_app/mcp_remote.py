"""Opt-in Streamable HTTP for native agents on an encrypted private network."""

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn import Config, Server

from .mcp_server import build_tools

Authenticate = Callable[[str], bool]
RemoteDispatch = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]


def remote_app(
    dispatch: RemoteDispatch, authenticate: Authenticate, allowed_hosts: tuple[str, ...]
) -> ASGIApp:
    if not allowed_hosts or any(
        not host or host == ":*" or "*" in host.removesuffix(":*") for host in allowed_hosts
    ):
        raise ValueError("Remote MCP needs an explicit list of allowed hosts.")

    token_for_request: ContextVar[str] = ContextVar("mcp_agent_token")

    async def relay(request: dict[str, Any]) -> dict[str, Any]:
        return await dispatch(request, token_for_request.get())

    app = build_tools(relay, relay).streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=32 * 1024,
        transport_security=TransportSecuritySettings(allowed_hosts=list(allowed_hosts)),
    )

    async def authenticated(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        auth = Headers(scope=scope).getlist("authorization")
        scheme, _, token = auth[0].partition(" ") if len(auth) == 1 else ("", "", "")
        if scheme.lower() != "bearer" or not token or not authenticate(token):
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                scope, receive, send
            )
            return
        saved = token_for_request.set(token)
        try:
            await app(scope, receive, send)
        finally:
            token_for_request.reset(saved)

    return authenticated


class _EmbeddedServer(Server):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The companion owns process signals and shuts down every listener.
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await super().startup(sockets)
        except SystemExit as error:
            raise RuntimeError("Remote MCP could not start.") from error
        self.ready.set()


@asynccontextmanager
async def serve_remote(
    dispatch: RemoteDispatch,
    authenticate: Authenticate,
    *,
    host: str,
    port: int,
    allowed_hosts: tuple[str, ...],
) -> AsyncIterator[int]:
    server = _EmbeddedServer(
        Config(
            remote_app(dispatch, authenticate, allowed_hosts),
            log_config=None,
            access_log=False,
            proxy_headers=False,
            ws="none",
            lifespan="on",
            timeout_graceful_shutdown=5,
        )
    )
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as listener:
        listener.bind((host, port))
        task = asyncio.create_task(server.serve(sockets=[listener]), name="remote-mcp")
        ready = asyncio.create_task(server.ready.wait())
        try:
            await asyncio.wait((task, ready), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
                raise RuntimeError("Remote MCP exited during startup.")
            yield int(listener.getsockname()[1])
        finally:
            ready.cancel()
            server.should_exit = True
            await task
