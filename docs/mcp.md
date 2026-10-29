# MCP (read-only)

The Docker app can answer a local MCP client, such as Claude Code or Claude Desktop, about the journeys it follows. It offers exactly three tools, and they only read what `/status` shows. There are no actions over MCP: to pick a train, say you are leaving or skip a day, use Telegram.

Data: NS.

## The tools

Each tool takes a profile, by its id or its name. A wrong or missing name gives the same error text Telegram gives.

- `get_status(profile)`: what `/status` shows for that profile: each journey being followed, its legs, and the warnings (NS data not fresh, state file failing, key rejected, alerts undelivered, delivery not confirmed).
- `get_next_connections(profile, direction)`: the next connections for `outbound` or `return`.
- `get_works_ahead(profile)`: planned works ahead on that profile's journeys, and how fresh that check is.

Every answer carries a `text` field with the same wording as Telegram, ending with `Data: NS`. A leg reads "on time per NS", never just "ok", and a warning that `/status` shows is in every answer.

## How it runs

The app listens on a private socket, `mcp.sock`, in its data volume (`/data`), readable only by the app's own user. The MCP client starts a small stdio server in its own short-lived container, the compose service `mcp`, sharing the data volume; that server relays each tool call to the socket and holds nothing itself. It does not depend on the app container, so it keeps answering when the app is stopped or crashes. The app must be running for real answers: when it is not, every tool answers "The commute app is not running, or its MCP socket is missing." and never old data.

## Claude Code

```
claude mcp add --transport stdio commute -- docker compose -f /path/to/train-commute-companion/examples/docker-compose.yml run --rm --no-deps -T mcp
```

## Claude Desktop

Add an entry to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "commute": {
      "command": "docker",
      "args": [
        "compose", "-f", "/path/to/train-commute-companion/examples/docker-compose.yml",
        "run", "--rm", "--no-deps", "-T", "mcp"
      ]
    }
  }
}
```

## Local only

- There is no network port for MCP. The tools are reached only through `docker` on the machine that runs the container.
- Anyone who can run `docker` on that machine can read your journeys: profile names, stations and times. No secret (NS key, bot token, chat id, webhook values) is ever in an answer.
- Never put a network bridge or proxy in front of it.
