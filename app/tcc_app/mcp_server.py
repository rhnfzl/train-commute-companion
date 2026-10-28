"""The read-only MCP server on stdio (9 step 5, 7.12 rule 15), run inside the container by the MCP
client: `python -m tcc_app.mcp_server`. It holds no state and reads no setting but the state
directory; each call is relayed to the running app's private socket. stdout carries only the
protocol, so logging goes to stderr."""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from tcc_app.web import ask_readout, mcp_socket_path

NOT_RUNNING = "The commute app is not running, or its MCP socket is missing."
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def build(socket: Path) -> MCPServer:
    server = MCPServer("train-commute-companion")

    async def relay(request: dict[str, Any]) -> dict[str, Any]:
        try:
            reply = await ask_readout(socket, request)
        except (OSError, TimeoutError, ValueError):
            raise ToolError(NOT_RUNNING) from None  # never stale data, never the error's text
        if "error" in reply:
            raise ToolError(str(reply["error"]))
        return reply

    @server.tool(
        description="What /status shows for one profile: its journeys, warnings and health.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_status(profile: str) -> dict[str, Any]:
        return await relay({"tool": "get_status", "profile": profile})

    @server.tool(
        description="The next connections of one profile's outbound or return journey.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_next_connections(
        profile: str, direction: Literal["outbound", "return"]
    ) -> dict[str, Any]:
        return await relay(
            {"tool": "get_next_connections", "profile": profile, "direction": direction}
        )

    @server.tool(
        description="Planned works ahead on one profile's journeys, with how fresh the check is.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_works_ahead(profile: str) -> dict[str, Any]:
        return await relay({"tool": "get_works_ahead", "profile": profile})

    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    build(mcp_socket_path(os.environ)).run()


if __name__ == "__main__":
    main()
