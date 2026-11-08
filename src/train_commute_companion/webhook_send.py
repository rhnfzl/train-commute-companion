"""Webhook delivery over httpx: the sender behind the `WebhookPort` and the setup test sends.
The payload and signature are `webhook.py`'s, which stays pure."""

import asyncio
import logging
from collections.abc import Callable, Collection, Sequence
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Literal

import httpx

from train_commute_companion.model import AlertEvent, Severity
from train_commute_companion.notify import DestinationReservations
from train_commute_companion.webhook import (
    WebhookTestResult,
    body,
    headers,
    probe_body,
    probe_id,
    transport_id,
)

log = logging.getLogger(__name__)

# one try is cut at TRY_DEADLINE_S in all (httpx.Timeout bounds each read only); a URL that
# failed rests REST_S
TRY_DEADLINE_S = 5.0
TIMEOUT = httpx.Timeout(TRY_DEADLINE_S)
REST_S = 60

_IN_TRY: ContextVar[bool] = ContextVar("tcc_webhook_try", default=False)
# httpx logs each request URL at INFO and httpcore the headers at DEBUG; a logger's filter does
# not cover its child loggers, so each one is named
_QUIETED = ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy")


class _InTryFilter(logging.Filter):
    """Drops every record emitted inside a webhook try: a URL path, a token or a signature
    must never reach a log. Records of other clients and other tasks pass."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _IN_TRY.get()


_FILTER = _InTryFilter()


def _install_log_filter() -> None:
    for name in _QUIETED:
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)


async def _try_once(
    client: httpx.AsyncClient, url: str, body: bytes, hdrs: dict[str, str]
) -> int | str:
    """One POST: the status, or the error's class name (never its text, which names the URL).
    The body of the answer is never read: leaving the block closes it, so a trickling or huge
    answer cannot hold the pass past the deadline."""
    token = _IN_TRY.set(True)
    try:
        async with (
            asyncio.timeout(TRY_DEADLINE_S),
            client.stream(
                "POST", url, content=body, headers=hdrs, timeout=TIMEOUT, follow_redirects=False
            ) as r,
        ):
            return r.status_code
    except (httpx.HTTPError, TimeoutError) as e:  # TimeoutError: the deadline
        return type(e).__name__
    finally:
        _IN_TRY.reset(token)


def _accepted(r: int | str) -> bool:
    return isinstance(r, int) and 200 <= r <= 299


class WebhookPoster:
    """The webhook port over httpx: one try per URL per call, every URL concurrently; a URL
    that failed rests REST_S. One warning per outage, never anything about the phone."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        urls: Sequence[str],
        secret: str,
        now: Callable[[], datetime],
        *,
        token: str = "",
        stopped: Callable[[], bool] = lambda: False,
    ) -> None:
        _install_log_filter()
        self._client, self.urls, self._secret, self._now = client, tuple(urls), secret, now
        self.channels: dict[str, Literal["primary", "email", "webhook"]] = {
            url: "webhook" for url in self.urls
        }
        self._token, self._stopped = token, stopped
        self.destination_ids = {url: transport_id(url, secret, token) for url in self.urls}
        self._attempts = DestinationReservations()
        self.down = self._attempts.down
        self.on_reserve: Callable[[], None] = lambda: None

    @property
    def failing(self) -> bool:
        return self._attempts.failing(self.urls)

    def due(self, urls: Collection[str]) -> frozenset[str]:
        if self._stopped():
            return frozenset()
        return self._attempts.due(set(urls) & set(self.urls), self._now())

    async def post(
        self, event: AlertEvent, msg_id: str, urls: Collection[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        start = self._now()
        due = self.due(urls)
        todo = list(dict.fromkeys(u for u in urls if u in due))
        if not todo:
            return frozenset(), frozenset()
        b = body(event)
        hdrs = headers(self._secret, msg_id, int(start.timestamp()), b, self._token)
        was_down = {u for u in todo if u in self.down}
        # reserved before the request: a cut-off try has an unknown result and counts as a try
        reserved = self._attempts.reserve(todo, start, timedelta(seconds=REST_S))
        todo = [u for u in todo if u in reserved]
        accepted: set[str] = set()
        try:
            self.on_reserve()
            results = await asyncio.gather(*(_try_once(self._client, u, b, hdrs) for u in todo))
            accepted = {u for u, result in zip(todo, results, strict=True) if _accepted(result)}
        finally:
            for u in todo:
                self._attempts.settle(u, u in accepted)
        ok = set()
        for u, r in zip(todo, results, strict=True):
            host = httpx.URL(u).host
            if _accepted(r):
                ok.add(u)
                if u in was_down:
                    log.info("webhook to %s works again", host)
            elif u in was_down:
                log.debug("webhook to %s failed again (%s)", host, r)
            else:
                log.warning("webhook to %s failed (%s); next try after %d s", host, r, REST_S)
        if ok:
            self.on_reserve()  # a restart never brings back a rest this try cleared
        return frozenset(todo), frozenset(ok)


async def send_webhook_tests(
    client: httpx.AsyncClient,
    urls: Sequence[str],
    secret: str,
    now: Callable[[], datetime],
    *,
    token: str = "",
) -> tuple[WebhookTestResult, ...]:
    """Setup test sends: a signed loud and a signed silent test to every
    URL, concurrently. Never stored, never retried, and apart from any `WebhookPoster`'s rest
    and `failing`."""
    _install_log_filter()

    async def one(url: str) -> WebhookTestResult:
        got: list[int | str] = []
        for sev in (Severity.LOUD, Severity.SILENT):
            t = now()
            b = probe_body(sev, t)
            hdrs = headers(secret, probe_id(sev, t), int(t.timestamp()), b, token)
            got.append(await _try_once(client, url, b, hdrs))
        res = WebhookTestResult(httpx.URL(url).host, got[0], got[1])
        if res.ok:
            log.info("webhook test to %s: accepted", res.host)
        else:
            log.info("webhook test to %s: not accepted (loud %s, silent %s)", res.host, *got)
        return res

    return tuple(await asyncio.gather(*(one(u) for u in urls)))
