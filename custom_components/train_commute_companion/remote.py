"""The optional HTTPS connection to the Docker runtime."""

import re
import ssl
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast, overload
from urllib.parse import urlsplit

import httpx
from homeassistant.core import HomeAssistant

from train_commute_companion.home_assistant_bridge import PATH, read_result, request
from train_commute_companion.runtime import Runtime

from .const import CONF_REMOTE_CA, CONF_REMOTE_TOKEN, CONF_REMOTE_URL
from .host import RuntimeHost, RuntimeNotRunning
from .validate import check_path


class RemoteUnavailable(RuntimeNotRunning):
    """The remote runtime did not accept the connection or command."""


class RemoteRuntime:
    def __init__(self, origin: str, token: str, verify: ssl.SSLContext) -> None:
        self._endpoint = origin + PATH
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            verify=verify,
            timeout=120,
            trust_env=False,
            follow_redirects=False,
        )

    @property
    def alive(self) -> bool:
        return not self._client.is_closed

    async def stop(self) -> None:
        await self._client.aclose()

    async def command(self, method: str, **arguments: Any) -> Any:
        if not self.alive:
            raise RemoteUnavailable("The Docker connection is closed.")
        try:
            response = await self._client.post(self._endpoint, json=request(method, arguments))
            response.raise_for_status()
            return read_result(method, response.json())
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise RemoteUnavailable("The Docker runtime could not handle the request.") from None


async def open_remote(hass: HomeAssistant, options: Mapping[str, Any]) -> RemoteRuntime:
    origin = options[CONF_REMOTE_URL].strip().rstrip("/")
    token = options.get(CONF_REMOTE_TOKEN, "").strip()
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.path
        or "?" in origin
        or "#" in origin
        or not re.fullmatch(r"[0-9a-f]{64}", token)
    ):
        raise ValueError("Provide an HTTPS origin and a bearer token.")
    # Accessing port validates its syntax and range before the client can send a credential.
    if parsed.port == 0:
        raise ValueError("Provide a valid HTTPS port.")
    ca = options.get(CONF_REMOTE_CA, "").strip()
    if ca and await check_path(hass, ca) is not None:
        raise ValueError("Use a readable CA file in Home Assistant's allowed folders.")

    verify = await hass.async_add_executor_job(
        lambda: ssl.create_default_context(cafile=ca or None)
    )
    return RemoteRuntime(origin, token, verify)


@overload
async def invoke[T](
    host: RuntimeHost | RemoteRuntime, action: Callable[..., Awaitable[T]], **arguments: Any
) -> T: ...


@overload
async def invoke[T](
    host: RuntimeHost | RemoteRuntime, action: Callable[..., T], **arguments: Any
) -> T: ...


async def invoke[T](
    host: RuntimeHost | RemoteRuntime, action: Callable[..., Awaitable[T] | T], **arguments: Any
) -> T:
    # Call sites choose Runtime methods in code. Service data only supplies named arguments.
    name = action.__name__
    if isinstance(host, RemoteRuntime):
        return cast(T, await host.command(name, **arguments))

    async def local(runtime: Runtime) -> T:
        method = cast(Callable[..., Awaitable[T] | T], getattr(runtime, name))
        value = method(**arguments)
        return await value if isinstance(value, Awaitable) else value

    return await host.run(local)
