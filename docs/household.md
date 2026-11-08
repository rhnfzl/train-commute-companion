# Separate companions for a household

Each traveller gets a separate companion, private Telegram bot and stored journey data. Their commands and location fixes reach only their companion.

This deployment uses the existing single-traveller app. It does not add shared-runtime accounts or a shared household interface. Keep `traveller: me`, or omit it, in each private profile file.

The names `alice` and `bob` below are example service labels. They do not identify actual people.

## Prepare each companion

Run these commands from this checkout's `examples/` directory:

```sh
cd examples
mkdir -p config/household/alice/profiles config/household/bob/profiles
cp household.env.example config/household/alice/.env
cp household.env.example config/household/bob/.env
cp profiles.yaml config/household/alice/profiles/profiles.yaml
cp profiles.yaml config/household/bob/profiles/profiles.yaml
chmod 700 config/household config/household/alice config/household/bob
chmod 600 config/household/alice/.env config/household/bob/.env
```

The existing `examples/config/` ignore rule excludes these private files from Git. Do not commit them.

Create two separate Telegram bots with BotFather. Edit each `.env` with its own bot token and NS API key. The NS key can be shared only when its owner permits that use and its quota covers both companions.

Never reuse a bot token. Two pollers for one bot can consume each other's updates and cause Telegram conflicts.

Leave both `TELEGRAM_CHAT_ID` values blank until each chat is identified. Set each private profile file to its owner's stations and schedule.

Validate and start the apps:

```sh
docker compose -f compose.household.yaml config --quiet
docker compose -f compose.household.yaml build alice bob
docker compose -f compose.household.yaml run --rm --no-deps alice tcc check-profile /config/profiles.yaml
docker compose -f compose.household.yaml run --rm --no-deps bob tcc check-profile /config/profiles.yaml
docker compose -f compose.household.yaml up -d alice bob
```

Each traveller now messages their own bot from a private chat. Read each chat ID separately:

```sh
docker compose -f compose.household.yaml logs alice
docker compose -f compose.household.yaml logs bob
```

Put the correct positive chat ID into each owner's `.env`. Never configure a group chat. Apply the settings:

```sh
docker compose -f compose.household.yaml up -d --force-recreate alice bob
```

Each traveller sends `/start` to their own bot and confirms its loud and silent test alerts. Confirmation is separate for each companion. `/status`, buttons and live location work only through that companion's configured private chat.

## Health and location

The HTTP ports bind only to the host's loopback address:

| Companion | Local health endpoint | Local OwnTracks endpoint |
| --- | --- | --- |
| `alice` | `http://127.0.0.1:8081/health` | `http://127.0.0.1:8081/owntracks` |
| `bob` | `http://127.0.0.1:8082/health` | `http://127.0.0.1:8082/owntracks` |

The health endpoint returns only `ok` without its owner's health token. OwnTracks stays disabled until `OWNTRACKS_SECRET` is set.

Generate each enabled secret independently, for example with `openssl rand -hex 32`. Use different values for both owners and for health versus OwnTracks. Store each value only in its owner's `.env`. Apply changes with the force-recreate command above.

For phone access, use a host reverse proxy with TLS. Give each traveller a separate address that forwards only `/owntracks` to their own loopback port. Keep `/health` private. The default loopback endpoints cannot receive phone updates over mobile data.

The OwnTracks user name does not select a traveller. The endpoint and its secret select the companion. Never send two people's phones to the same endpoint. See [OwnTracks](location-owntracks.md) for profile settings and phone setup.

Email and webhook copies are off in the template. Add them only to the intended owner's `.env`, with that owner's destinations and credentials. A global shell environment does not automatically enter these containers.

## Read-only MCP

Each relay mounts only its owner's data volume. Configure separate MCP clients with the corresponding command, using an absolute compose path:

```sh
docker compose -f /absolute/path/to/examples/compose.household.yaml run --rm --no-deps -T alice-mcp
docker compose -f /absolute/path/to/examples/compose.household.yaml run --rm --no-deps -T bob-mcp
```

Each relay provides the existing read-only status, connection and planned-works tools. It has no HTTP port, network access, bot credentials or access to the other data volume. The corresponding app must run for current answers. See [MCP](mcp.md) for client configuration.

## Trust boundary

The apps use separate configuration mounts, state volumes, bot credentials and Docker networks. They do not mount the Docker socket or use privileged mode.

This separates recipients and prevents accidental command, status and location mixing. It does not isolate travellers from the Docker host administrator. Anyone with Docker access can mount either volume and read its data. Do not give an untrusted traveller Docker access and claim tenant isolation.

A shared Home Assistant instance also shares entity state, services and its event bus. Separate notification targets do not create private accounts there. Use separate instances when confidentiality between users is required.

A future shared household interface requires authenticated traveller scopes for notifications, status, actions, location and MCP. That work remains separate from this process-per-traveller deployment.
