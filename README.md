# Train Commute Companion

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![Validate](https://github.com/rhnfzl/train-commute-companion/actions/workflows/validate.yml/badge.svg)](https://github.com/rhnfzl/train-commute-companion/actions/workflows/validate.yml)
[![GitHub release](https://img.shields.io/github/v/release/rhnfzl/train-commute-companion)](https://github.com/rhnfzl/train-commute-companion/releases)
[![PyPI](https://img.shields.io/pypi/v/train-commute-companion)](https://pypi.org/project/train-commute-companion/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![License](https://img.shields.io/github/license/rhnfzl/train-commute-companion)](LICENSE)

Follows your usual NS train journey and tells you when something changes: a delay, a cancelled train, a platform change or a transfer you can no longer make.

Data: NS. You bring your own NS API key; none is bundled. This project is not made by or linked to NS.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rhnfzl&repository=train-commute-companion&category=integration)

## What it does

- **Follows your trains.** You describe your usual trips once, in a small YAML file. On a travel day it picks your train and watches it.
- **Tells you what changed.** Delays, cancellations, platform changes, a train cut short, a new route, a transfer at risk or broken, and a train you missed.
- **Suggests what to do.** When your train no longer works, it names the best train you can still catch.
- **Looks ahead.** Before the day, it warns you about planned works on your route.
- **Loud only when it matters.** Alerts you must not miss ring; small updates arrive silently.
- **Says when NS is unclear.** When NS gives two different answers about the same train, you get both, so you can decide.

## Ways to run it

| Way | What you get | Guide |
| --- | --- | --- |
| Home Assistant (HACS) | Alerts on your phone through the companion app, sensors and buttons | [docs/home-assistant.md](docs/home-assistant.md) |
| Docker | Alerts and buttons in a private Telegram chat | [docs/docker.md](docs/docker.md) |
| Webhooks | Each alert as signed JSON to your own URL, from Docker or Home Assistant | [docs/webhooks.md](docs/webhooks.md) |
| MCP | Three read-only tools (status, next connections, planned works) for a local MCP client | [docs/mcp.md](docs/mcp.md) |

Location (optional) helps it know when you leave and where you are: [Home Assistant](docs/location-ha.md), [OwnTracks](docs/location-owntracks.md), [Telegram](docs/location-telegram.md).

## Quick start (Home Assistant)

1. In HACS, add `https://github.com/rhnfzl/train-commute-companion` as a custom repository of type Integration, or use the button above.
2. Download Train Commute Companion and restart Home Assistant.
3. Copy [examples/profiles.yaml](examples/profiles.yaml) to `<config>/train_commute_companion/profiles.yaml` and fill in your own stations and trains.
4. Add the integration under Settings, Devices and services, and enter your NS API key.

The full guide is in [docs/home-assistant.md](docs/home-assistant.md).

## The Python core

The core is a typed Python library on PyPI. It has no Home Assistant code.

```bash
pip install train-commute-companion
tcc check-profile profiles.yaml
```

## Development

```bash
uv run --extra app pytest -q        # core and app tests (the extra installs the MCP SDK)
uv run ruff check src tests
uv run mypy --strict src
```

## License

[Apache-2.0](LICENSE)
