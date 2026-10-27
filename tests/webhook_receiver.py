"""A test receiver that verifies each delivery with the recipe documented in docs/webhooks.md."""

import json
import re
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import httpx

DOCS = Path(__file__).resolve().parent.parent / "docs" / "webhooks.md"


@cache
def doc_verify() -> Callable[..., bool]:
    """The `verify` function from the first python block under `## Verify a delivery`."""
    section = DOCS.read_text(encoding="utf-8").split("\n## Verify a delivery", 1)[1]
    match = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert match, "no python block under ## Verify a delivery"
    scope: dict[str, Any] = {}
    exec(match.group(1), scope)  # noqa: S102 (our own docs, so receivers copy tested code)
    return scope["verify"]  # type: ignore[no-any-return]


class Receiver:
    def __init__(self, secret: str, now: Callable[[], float] | None, status: int = 200) -> None:
        self.secret = secret
        self.now = now  # None: bound later by the HA helpers, before the runtime starts
        self.status = status
        self.requests: list[httpx.Request] = []
        self.posts: list[dict[str, Any]] = []
        self.bad = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.now is None:
            raise AssertionError("Receiver.now was never bound to a fake clock")
        self.requests.append(request)
        body = request.content
        if doc_verify()(self.secret, dict(request.headers), body, now=self.now()):
            self.posts.append(json.loads(body))
        else:
            self.bad += 1
        return httpx.Response(self.status)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
