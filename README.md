# train-commute-companion

A small typed Python core that follows your usual NS train journey and tells you when something changes, such as a delay, a cancelled train or a broken transfer.

Data: NS. You bring your own NS API key; none is bundled.

Home Assistant: a custom integration (HACS) wraps the core and sends alerts to your phone. See [docs/home-assistant.md](docs/home-assistant.md).

Docker: a standalone container sends alerts and buttons through Telegram. See [docs/docker.md](docs/docker.md).
