import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_compose_example_and_dockerfile_carry_no_secret_or_personal_value():
    compose = (ROOT / "examples/docker-compose.yml").read_text()
    assert "./config:/config:ro" in compose and "tcc-data:/data" in compose and "env_file" in compose
    assert not re.search(r"(NS_API_KEY|TELEGRAM_BOT_TOKEN|OWNTRACKS_SECRET)\s*[:=]\s*\S", compose)
    docker = (ROOT / "Dockerfile").read_text()
    assert "USER 10001" in docker and "HEALTHCHECK" in docker and "uv sync --locked" in docker
    assert ".env" in (ROOT / ".gitignore").read_text().split()
    assert ".env" in (ROOT / ".dockerignore").read_text().split()
