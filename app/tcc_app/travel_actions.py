"""The companion authorizes agent changes before passing them to the plan owner."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .travel_preferences import TravelPreferences

log = logging.getLogger(__name__)
POLICY_LABELS = {"ask": "Ask", "always": "Always allow", "deny": "Deny"}
APPROVAL_TTL = timedelta(minutes=10)
LANGUAGE_SOURCE = "Telegram language"


class TravelRuntime(Protocol):
    def now(self) -> datetime: ...
    def list_travel_plans(self) -> dict[str, Any]: ...
    def preview_travel_plan(self, change: object) -> dict[str, Any]: ...
    def save_travel_plan(
        self, change: object, review_token: str, request_id: str
    ) -> dict[str, Any]: ...
    def travel_change_receipt(self, request_id: str) -> dict[str, Any] | None: ...


Prompt = Callable[[str, str], Awaitable[bool]]


def agent_tokens(raw: str) -> dict[str, str]:
    try:
        agents = json.loads(raw or "{}")
        if not isinstance(agents, dict) or len(agents) > 16:
            raise ValueError
        if any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name)
            or not isinstance(token, str)
            or not re.fullmatch(r"[!-~]{32,128}", token)
            for name, token in agents.items()
        ) or len(set(agents.values())) != len(agents):
            raise ValueError
    except (TypeError, ValueError, RecursionError):
        raise ValueError(
            "TCC_MCP_AGENTS must map distinct agent names to distinct 32–128 character tokens"
        ) from None
    return agents


def save_private_json(path: Path, data: object) -> bool:
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temp = Path(stream.name)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError as error:
        log.warning("Could not save travel settings (%s)", type(error).__name__)
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    return True


def result(status: str, text: str) -> dict[str, Any]:
    return {"status": status, "text": text}


@dataclass(frozen=True)
class Agent:
    name: str
    digest: str

    @property
    def key(self) -> str:
        return f"{self.name}:{self.digest}"


@dataclass(frozen=True)
class Pending:
    id: str
    key: str
    agent: Agent
    tool: str
    submitted: dict[str, Any]
    change: dict[str, Any]
    review: str
    expires: datetime


class TravelActions:
    def __init__(
        self,
        rt: TravelRuntime,
        tokens: Mapping[str, str],
        path: Path,
        preferences: TravelPreferences | None = None,
        *,
        language_identity: str | None = None,
    ) -> None:
        self.rt = rt
        self.preferences = preferences
        self._agents = {
            name: Agent(name, hashlib.sha256(token.encode()).hexdigest())
            for name, token in tokens.items()
        }
        self._language = (
            Agent(LANGUAGE_SOURCE, hashlib.sha256(language_identity.encode()).hexdigest())
            if language_identity is not None
            else None
        )
        self._path = path
        self._safe = True
        self._policies: dict[str, str] = {}
        self._pending: dict[str, Pending] = {}
        self._closed: dict[str, tuple[datetime, dict[str, Any]]] = {}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or any(
                not isinstance(value, str) or value not in POLICY_LABELS for value in data.values()
            ):
                raise ValueError
            self._policies = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            log.warning("Travel agent permissions are unreadable. Agent changes are disabled.")
            self._safe = False

    def agent(self, token: str | None) -> Agent | None:
        if not token or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        return next(
            (
                agent
                for agent in self._agents.values()
                if secrets.compare_digest(agent.digest, digest)
            ),
            None,
        )

    def policy(self, agent: Agent) -> str:
        return self._policies.get(agent.key, "ask") if self._safe else "deny"

    def language_policy(self) -> str:
        return self.policy(self._language) if self._language else "ask"

    def policies(self) -> dict[str, str]:
        sources = list(self._agents.values())
        if self._language:
            sources.append(self._language)
        return {agent.name: self.policy(agent) for agent in sources}

    def set_policy(self, name: str, policy: str) -> dict[str, Any]:
        agent = self._language if name == LANGUAGE_SOURCE else self._agents.get(name)
        if agent is None or policy not in POLICY_LABELS:
            return result("invalid", "Unknown agent or permission.")
        if not self._safe:
            return result("retry", "Repair the agent permissions file before changing access.")
        sources = list(self._agents.values())
        if self._language:
            sources.append(self._language)
        candidate = {current.key: self.policy(current) for current in sources}
        candidate[agent.key] = policy
        if not save_private_json(self._path, candidate):
            return result("retry", "Permission was not saved. Try again.")
        self._policies = candidate
        if policy == "deny":
            for pending in tuple(self._pending.values()):
                if pending.agent == agent:
                    self._close(pending, result("denied", "Agent changes are blocked."))
        return result("saved", f"{name}: {POLICY_LABELS[policy]}.")

    def _close(self, pending: Pending, reply: dict[str, Any]) -> dict[str, Any]:
        self._pending.pop(pending.key, None)
        self._closed[pending.key] = (self.rt.now() + APPROVAL_TTL, reply)
        return reply

    def _receipt(self, key: str, tool: str) -> dict[str, Any] | None:
        plan = self.rt.travel_change_receipt(key)
        preference = self.preferences.receipt(key) if self.preferences is not None else None
        if (plan and tool == "change_travel_preference") or (
            preference and tool == "change_travel_plan"
        ):
            return result("invalid", "This request ID already names a different change.")
        return plan or preference or self._closed.get(key, (None, None))[1]

    def can_always(self, approval_id: str) -> bool:
        return any(
            item.id == approval_id
            and item.tool == "change_travel_plan"
            and self.policy(item.agent) == "ask"
            for item in self._pending.values()
        )

    def _prune(self) -> None:
        now = self.rt.now()
        for pending in tuple(self._pending.values()):
            if pending.expires <= now:
                self._close(pending, result("expired", "Travel approval expired."))
        self._closed = {key: item for key, item in self._closed.items() if item[0] > now}
        while len(self._closed) > 128:
            self._closed.pop(next(iter(self._closed)))

    async def dispatch(
        self, request: dict[str, Any], token: str | None, prompt: Prompt
    ) -> dict[str, Any]:
        agent = self.agent(token)
        if agent is None:
            return result("denied", "Connect a registered agent to use travel plan tools.")
        tool = request.get("tool")
        if tool == "list_travel_plans":
            return self.rt.list_travel_plans()
        prefs = self.preferences
        if tool == "list_travel_preferences":
            return (
                prefs.list() if prefs is not None else result("retry", "Preferences unavailable.")
            )
        if tool not in ("change_travel_plan", "get_travel_change", "change_travel_preference"):
            return result("invalid", "Unknown travel tool.")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9._:-]{1,96}", request_id
        ):
            return result("invalid", "Use a request ID of 1 to 96 letters, numbers or . _ : -")
        key = hashlib.sha256(f"{agent.key}:{request_id}".encode()).hexdigest()[:48]
        self._prune()
        submitted = request.get("change")
        saved = self._receipt(key, tool)
        if saved is not None:
            if saved.get("status") == "saved" and tool != "get_travel_change":
                if not isinstance(submitted, dict):
                    return result("invalid", "Supply the original change to retry this request ID.")
                # Both stores compare the payload to the durable receipt before checking the review.
                if tool == "change_travel_preference" and prefs is not None:
                    return prefs.save(submitted, "", key)
                change = submitted if prefs is None else prefs.with_defaults(submitted)
                return self.rt.save_travel_plan(change, "", key)
            return saved
        pending = self._pending.get(key)
        if pending is not None:
            if tool != "get_travel_change" and (
                tool != pending.tool or request.get("change") != pending.submitted
            ):
                return result("invalid", "This request ID already names a different change.")
            return result("pending", "Waiting for your approval in Telegram.")
        if tool == "get_travel_change":
            return result("unknown", "No current request with that ID.")
        if self.policy(agent) == "deny":
            return result("denied", "This agent may read plans. Changes are blocked.")
        if not isinstance(submitted, dict):
            return result("invalid", "Supply a travel plan change.")
        conflict = None
        if tool == "change_travel_preference":
            if prefs is None:
                return result("retry", "Preferences unavailable.")
            change, review = submitted, prefs.preview(submitted)
        else:
            change = submitted if prefs is None else prefs.with_defaults(submitted)
            if "conflict_policy" in change:
                unchecked = {k: v for k, v in change.items() if k != "conflict_policy"}
                original = self.rt.preview_travel_plan(unchecked)
                if original.get("status") == "conflict":
                    conflict = original["text"]
            review = self.rt.preview_travel_plan(change)
        if review.get("status") != "ready":
            return review
        if tool == "change_travel_plan" and not conflict and self.policy(agent) == "always":
            return self.rt.save_travel_plan(change, review["review_token"], key)
        if len(self._pending) >= 16:
            return result("retry", "Too many changes are waiting for approval.")
        item = Pending(
            secrets.token_hex(12),
            key,
            agent,
            tool,
            deepcopy(submitted),
            deepcopy(change),
            review["review_token"],
            self.rt.now() + APPROVAL_TTL,
        )
        self._pending[key] = item
        # A Telegram tap can be processed before the send response comes back.
        what = "a travel plan" if tool == "change_travel_plan" else "a travel preference"
        details = f"{conflict}\n{review['text']}" if conflict else review["text"]
        delivered = False
        try:
            delivered = await prompt(f"{agent.name} asks to change {what}.\n{details}", item.id)
        finally:
            if not delivered and self._pending.get(key) == item:
                self._pending.pop(key)
        if (closed := self._receipt(key, tool)) is not None:
            return closed
        if not delivered:
            return result("retry", "Telegram could not deliver the approval. No plan changed.")
        return result("pending", "Waiting for your approval in Telegram.")

    def decide(self, approval_id: str, decision: str) -> dict[str, Any]:
        pending = next((item for item in self._pending.values() if item.id == approval_id), None)
        if pending is None:
            return result("expired", "That travel approval expired or was already answered.")
        if pending.expires <= self.rt.now():
            return self._close(pending, result("expired", "Travel approval expired."))
        if decision == "deny" or self.policy(pending.agent) == "deny":
            return self._close(pending, result("denied", "Change denied."))
        if decision != "once" and (decision != "always" or not self.can_always(approval_id)):
            return result("invalid", "Unknown approval choice.")
        if pending.tool == "change_travel_preference":
            assert self.preferences is not None
            reply = self.preferences.save(pending.change, pending.review, pending.key)
        else:
            reply = self.rt.save_travel_plan(pending.change, pending.review, pending.key)
        if reply.get("status") == "retry":
            return reply
        if decision == "always" and reply.get("status") == "saved":
            policy = self.set_policy(pending.agent.name, "always")
            reply = {**reply, "text": f"{reply['text']}\n{policy['text']}"}
        return self._close(pending, reply)
