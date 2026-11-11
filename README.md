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
- **Summarizes the week.** An optional [works digest](docs/weekly-works-digest.md) checks seven calendar days and labels unknown coverage.
- **Tracks replacement transport.** Follow reviewed bus, walk and metro plans with explicit boarding and arrival confirmations. [Replacement transport](docs/replacement-transport.md).
- **Shows crowd forecasts.** NS forecasts appear with your trains and alternatives; changed forecasts can send silent updates. [Crowd forecasts](docs/crowd-forecasts.md).
- **Changes plans and alerts.** Resume a skipped journey and control quiet or paused notifications in [Home Assistant](docs/home-assistant.md).
- **Pauses for holidays.** Set an inclusive date range and monitoring resumes afterward. [Holiday pauses](docs/holiday-pauses.md).
- **Uses weekday departure times.** Set different first trains within one profile while preserving already-followed journeys. [Weekday schedules](docs/weekday-schedules.md).
- **Shows train context.** Optional NS composition, listed capacity and candidate vehicle coordinates include source limits. [Train context](docs/train-context.md).
- **Loud only when it matters.** Alerts you must not miss ring; small updates arrive silently.
- **Says when NS is unclear.** When NS gives two different answers about the same train, you get both, so you can decide.

## Ways to run it

| Way | What you get | Guide |
| --- | --- | --- |
| Home Assistant (HACS) | Alerts on your phone through the companion app, sensors and buttons | [docs/home-assistant.md](docs/home-assistant.md) |
| Docker | Alerts and buttons in a private Telegram chat | [docs/docker.md](docs/docker.md) |
| Household | A separate Docker companion and private bot for each traveller | [docs/household.md](docs/household.md) |
| Webhooks | Each alert as signed JSON to your own URL, from Docker or Home Assistant | [docs/webhooks.md](docs/webhooks.md) |
| Email | Optional encrypted SMTP copies alongside Telegram in Docker | [docs/email.md](docs/email.md) |
| Today | Private read-only journey and service status in a browser, from Docker | [docs/docker.md](docs/docker.md#today-web-status) |
| MCP | Three read-only tools (status, next connections, planned works) for a local MCP client | [docs/mcp.md](docs/mcp.md) |

Location (optional) helps it know when you leave and where you are: [Home Assistant](docs/location-ha.md), [OwnTracks](docs/location-owntracks.md), [Telegram](docs/location-telegram.md).

Profiles can request [accessible journeys](docs/accessibility.md) and disable [notification categories](docs/notification-preferences.md). These settings also appear in the Home Assistant profile form.

Save an [explicit arrival](docs/compensation-reminders.md) to compare it with the original plan and optionally check NS compensation rules. The companion does not decide eligibility. Today also offers deliberate calendar and offline route-preview downloads.

## Quick start (Home Assistant)

1. In HACS, add `https://github.com/rhnfzl/train-commute-companion` as a custom repository of type Integration, or use the button above.
2. Download Train Commute Companion and restart Home Assistant.
3. Add the integration under Settings, Devices and services, and enter your NS API key.
4. Enable guided setup to choose stations and trains in forms, or select an existing [profiles file](examples/profiles.yaml).

The full guide is in [docs/home-assistant.md](docs/home-assistant.md).

## The Python core

The core is a typed Python library on PyPI. It has no Home Assistant code.

It is released for its two shells, the Home Assistant integration and the Docker app, so its module APIs can change between versions. The supported entry point is the `tcc` command: `check-profile`, `replay` and `run`.

```bash
pip install train-commute-companion
tcc check-profile profiles.yaml
```

## Development

```bash
uv run --extra app pytest -q        # core and app tests (the extra installs the MCP SDK)
uv run --extra app mypy src app/tcc_app
# the published floors: Python 3.12, httpx 0.27, PyYAML 6.0.1 and mcp 2.3
uv run --no-cache --isolated --no-project --python 3.12 --with '.[app]' --with httpx==0.27.0 --with PyYAML==6.0.1 --with mcp==2.3.0 --with pytest --with pytest-asyncio pytest -q tests tests_app
uv run ruff check src tests tests_ha tests_app tests_live custom_components app

# Home Assistant integration: Python 3.14.2+, in its own environment
export UV_PROJECT_ENVIRONMENT=.venv-ha
uv run --python 3.14 --group ha pytest tests_ha -q
uv run --python 3.14 --group ha mypy --explicit-package-bases --namespace-packages -p custom_components.train_commute_companion
```

The [Validate workflow](.github/workflows/validate.yml) runs these same commands, and a release runs them before it publishes. Each night it also runs `tests_ha` against the newest Home Assistant test package, because the integration calls private `mobile_app` code.

[Live NS checks](docs/live-testing.md) run separately with your own key and capture alerts locally.

## License

[Apache-2.0](LICENSE)
