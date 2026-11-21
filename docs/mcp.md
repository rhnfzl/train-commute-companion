# MCP for journey status and travel plans

An MCP client can check your trains. A connected agent can also manage one-off, temporary and recurring travel plans. Run the client on the Docker host for the simplest setup. You can also enable a connection over your encrypted private network.

Data: NS.

## Local status tools

The default relay offers three read-only tools. Each takes a profile ID or name.

- `get_status(profile)`: what `/status` shows for that profile: each journey being followed, its legs, and the warnings (NS data not fresh, state file failing, key rejected, alerts undelivered, delivery not confirmed).
- `get_next_connections(profile, direction)`: the next connections for `outbound` or `return`.
- `get_works_ahead(profile)`: planned works ahead on that profile's journeys, and how fresh that check is.

Every answer carries a `text` field with the same wording as Telegram, ending with `Data: NS`. A leg reads "on time per NS", and a warning that `/status` shows is in every answer. Use Telegram to choose a train already being followed, say you are leaving, or skip a day.

## How it runs

The app listens on a private socket, `mcp.sock`, in its data volume (`/data`). The MCP client starts the `mcp` Compose service in its own short-lived container. Its data volume is read-only and its only default setting is `TCC_STATE_DIR`. The relay calls the running app for every answer. If the app stops, the relay reports that it cannot reach the app.

Anyone with Docker access on that host can read your profile names, stations and times. Keep the socket private. Never put a network bridge or proxy in front of it. Use the separate authenticated listener for [private-network access](#private-network-access).

## Claude Code

```
claude mcp add --transport stdio commute -- docker compose -f /path/to/docker-compose.yml run --rm --no-deps -T mcp
```

## Claude Desktop

Add an entry to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "commute": {
      "command": "docker",
      "args": [
        "compose", "-f", "/path/to/docker-compose.yml",
        "run", "--rm", "--no-deps", "-T", "mcp"
      ]
    }
  }
}
```

## Connect an agent to manage plans

1. Generate a unique token for each agent with `openssl rand -hex 32`.
2. Set `TCC_MCP_AGENTS` in the app's private `.env`. Use a JSON object whose keys are agent names and values are tokens.
3. Apply it with `docker compose up -d --force-recreate tcc`.
4. Set `TCC_MCP_AGENT_TOKEN` in the MCP client's environment. Pass just that variable through to the relay.

```dotenv
TCC_MCP_AGENTS='{"laptop":"<paste-generated-token>"}'
```

Choose names of up to 24 letters, digits, underscores or hyphens. Use distinct tokens of 32 to 128 printable ASCII characters without spaces. Keep tokens in private environment settings and out of command arguments.

```sh
docker compose -f /path/to/docker-compose.yml run --rm --no-deps -T -e TCC_MCP_AGENT_TOKEN mcp
```

This is the command for the MCP client to start. With the Claude Desktop example above, put `"-e", "TCC_MCP_AGENT_TOKEN"` just before `"mcp"`. Set the variable in your client configuration or its launch environment. The relay gets only this agent's token and the state directory.

- `list_travel_plans()` lists saved travel plans.
- `change_travel_plan(change, request_id)` proposes a create, edit, cancel or undo.
- `get_travel_change(request_id)` checks a plan or preference change.
- `list_travel_preferences()` lists confirmed travel preferences.
- `change_travel_preference(change, request_id)` requests saving or forgetting a preference.

Use a new `request_id` for each change. Reuse it if you retry that same change. A conflict response asks you to resolve the overlap. A pending response means Telegram approval is still needed. Success means the companion has saved the change.

Send `/agents` in Telegram to see or change each agent's policy. Plan writes default to **Ask**. On an approval message, **Allow once** approves only that change. **Always allow** also changes the agent's policy for future plan requests. **Deny** rejects this request. To block future writes, set **Deny** in `/agents`. Permitted preference edits still ask for approval. The companion still requires you to resolve a schedule conflict.

To revoke a token, remove its entry from `TCC_MCP_AGENTS` and recreate the app. The default read-only local relay stays available to users with Docker access.

### Example plan request

This plan goes to Utrecht on Tuesdays for three weeks and skips the middle Tuesday. Adjust the example dates and stations for your trip.

```json
{
  "request_id": "utrecht-tuesdays-1",
  "change": {
    "action": "create",
    "plan": {
      "name": "Utrecht Tuesdays",
      "origin": "RTD",
      "destination": "UT",
      "start_date": "2026-12-01",
      "end_date": "2026-12-15",
      "weekdays": [2],
      "skip_dates": ["2026-12-08"],
      "outbound": {"mode": "window", "earliest": "07:00", "latest": "08:30", "arrive_by": "09:00"},
      "return": {"mode": "on_leave", "earliest": "17:00", "latest": "22:00"}
    }
  }
}
```

If the agent reports a conflict, decide which trips to follow. The agent can submit a revised request with `conflict_policy` set to `replace` or `keep`. The companion checks that choice and shows any approval still needed.

## Private-network access

Enable Streamable HTTP when the client and app run on different machines. The companion can run on any supported Docker host, such as a small ARM board, a home lab server, or a VM. The client can run on any machine with a compatible MCP client and a route to that host.

Use an encrypted private network or terminate HTTPS at a trusted local proxy. Keep the listener off the public internet. Every HTTP request must carry a registered agent's bearer token, including requests for read-only tools.

Set up `TCC_MCP_AGENTS` as above. This example uses an encrypted private-network address of `100.101.102.103`. Put your Docker host's actual address in its private `.env`.

```dotenv
TCC_MCP_PRIVATE_IP=100.101.102.103
```

Save this optional override beside `docker-compose.yml` as `compose.mcp-remote.yaml`.

```yaml
services:
  tcc:
    environment:
      TCC_MCP_REMOTE_PORT: "8765"
      TCC_MCP_REMOTE_BIND: "0.0.0.0"
      TCC_MCP_REMOTE_ALLOWED_HOSTS: "${TCC_MCP_PRIVATE_IP:?Set the encrypted private-network IP}:8765"
    ports:
      - "${TCC_MCP_PRIVATE_IP:?Set the encrypted private-network IP}:8765:8765"
```

The bind address is inside the container. The published port is bound only to the host's specified private-network interface.

```sh
docker compose -f docker-compose.yml -f compose.mcp-remote.yaml up -d --force-recreate tcc
```

Set the MCP client's transport to Streamable HTTP. Set the URL to `http://100.101.102.103:8765/mcp` using your host's private IP. Send `Authorization: Bearer <agent-token>` on each request. Use HTTPS if your local proxy terminates TLS. The allowed host must match the address the client sends, including the port when it is non-default.

Use both Compose files for later updates while remote access is enabled. To turn it off, recreate the app with only `docker-compose.yml` and leave `TCC_MCP_REMOTE_PORT` unset in `.env`.
