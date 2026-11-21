"""MCP tools for the companion. The stdio entry point relays to the private app socket."""

import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .web import ask_readout, mcp_socket_path

NOT_RUNNING = "The commute app is not running, or its MCP socket is missing."
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
CHANGE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)
Dispatch = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def _call(dispatch: Dispatch, request: dict[str, Any]) -> dict[str, Any]:
    reply = await dispatch(request)
    if "error" in reply:
        raise ToolError(str(reply["error"]))
    return reply


def build_tools(read: Dispatch, travel: Dispatch | None = None) -> MCPServer:
    server = MCPServer("train-commute-companion")

    @server.tool(
        description="What /status shows for one profile: its journeys, warnings and health.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_status(profile: str) -> dict[str, Any]:
        return await _call(read, {"tool": "get_status", "profile": profile})

    if travel is None:

        @server.tool(
            description="The next connections of one profile's outbound or return journey.",
            annotations=READ_ONLY,
            structured_output=True,
        )
        async def get_next_connections(
            profile: str, direction: Literal["outbound", "return"]
        ) -> dict[str, Any]:
            return await _call(
                read, {"tool": "get_next_connections", "profile": profile, "direction": direction}
            )
    else:

        @server.tool(
            name="get_next_connections",
            description=(
                "Read current connections by regular profile ID or name. To preview a saved "
                "travel plan, send its ID as plan_id or profile. Find IDs with list_travel_plans. "
                "An optional date (YYYY-MM-DD) selects the outbound occurrence in the Netherlands. "
                "Next-day returns use that occurrence's following day. If you omit the date, "
                "the tool checks the next scheduled journey. Lookahead covers the next 30 days."
            ),
            annotations=READ_ONLY.model_copy(update={"open_world_hint": True}),
            structured_output=True,
        )
        async def get_next_connections_or_preview(
            direction: Literal["outbound", "return"],
            profile: str | None = None,
            plan_id: str | None = None,
            date: str | None = None,
        ) -> dict[str, Any]:
            request = {"tool": "get_next_connections", "direction": direction}
            request.update(
                (key, value)
                for key, value in (("profile", profile), ("plan_id", plan_id), ("date", date))
                if value is not None
            )
            return await _call(travel, request)

    @server.tool(
        description="Planned works ahead on one profile's journeys, with how fresh the check is.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_works_ahead(profile: str) -> dict[str, Any]:
        return await _call(read, {"tool": "get_works_ahead", "profile": profile})

    if travel is not None:
        add_travel_tools(server, travel)

    return server


def add_travel_tools(server: MCPServer, dispatch: Dispatch) -> None:
    @server.tool(
        description=(
            "List regular commute profiles and saved one-off, temporary and ongoing travel plans. "
            "Use profile_id for a regular commute or plan_id for a saved plan in get_next_connections."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_travel_plans() -> dict[str, Any]:
        return await _call(dispatch, {"tool": "list_travel_plans"})

    @server.tool(
        description=(
            "Manage one-off, temporary or ongoing NS travel plans. Supply change.action "
            "'create' or 'edit' with a plan (and plan_id for edit), 'cancel' with plan_id, "
            "or 'undo' with the undo_id from a saved change. A plan needs origin and destination "
            "(station codes or names), start_date (YYYY-MM-DD) and outbound. Optional fields "
            "are name, profile_id, return, weekdays (ISO numbers, Monday=1), interval_weeks, "
            "monthly ({ordinal: 1 to 5, weekday: ISO number} instead of weekdays), end_date, "
            "end_after (number of trips, instead of end_date), and skip_dates. Omit weekdays and "
            "monthly for a single date. Each journey uses mode 'exact' with departure and optional train_number, "
            "or 'window' or 'on_leave' with earliest, latest and optional arrive_by. "
            "You can omit the bounds for an arrival deadline or a full-day on_leave trip. "
            "Supply change.preference_name to reuse a confirmed preference. "
            "Times are HH:MM in the Netherlands. Set return.day_offset to 1 for a next-day return. "
            "If the result reports a conflict, ask the traveller before resubmitting with "
            "change.conflict_policy 'replace' or 'keep'. Ask about any missing details. Use a "
            "fresh request_id for each new change and reuse it for retries of the same change. "
            "A pending request needs approval in Telegram. Check it with get_travel_change."
        ),
        annotations=CHANGE,
        structured_output=True,
    )
    async def change_travel_plan(change: dict[str, Any], request_id: str) -> dict[str, Any]:
        return await _call(
            dispatch, {"tool": "change_travel_plan", "change": change, "request_id": request_id}
        )

    @server.tool(
        description="Check the result of an earlier travel plan or preference change.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_travel_change(request_id: str) -> dict[str, Any]:
        return await _call(dispatch, {"tool": "get_travel_change", "request_id": request_id})

    @server.tool(
        description="List confirmed travel preferences.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_travel_preferences() -> dict[str, Any]:
        return await _call(dispatch, {"tool": "list_travel_preferences"})

    @server.tool(
        description=(
            "Change a travel preference when the traveller explicitly asks. Use action 'save' "
            "with a preference containing name, origin, destination and optional arrival_by "
            "(HH:MM) or return_on_leave (boolean), or action 'forget' with the preference name. "
            "Use a fresh request_id for a new change and the same ID for retries. A pending "
            "request needs approval in Telegram. Check it with get_travel_change."
        ),
        annotations=CHANGE,
        structured_output=True,
    )
    async def change_travel_preference(change: dict[str, Any], request_id: str) -> dict[str, Any]:
        return await _call(
            dispatch,
            {"tool": "change_travel_preference", "change": change, "request_id": request_id},
        )


def build(socket: Path, *, token: str | None = None) -> MCPServer:
    async def relay(request: dict[str, Any]) -> dict[str, Any]:
        try:
            return await ask_readout(socket, request)
        except (OSError, TimeoutError, ValueError):
            raise ToolError(NOT_RUNNING) from None

    async def relay_travel(request: dict[str, Any]) -> dict[str, Any]:
        return await relay({**request, "token": token})

    return build_tools(relay, relay_travel if token else None)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    build(mcp_socket_path(os.environ), token=os.getenv("TCC_MCP_AGENT_TOKEN")).run()


if __name__ == "__main__":
    main()
