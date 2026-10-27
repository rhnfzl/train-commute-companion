import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_compose_example_and_dockerfile_carry_no_secret_or_personal_value():
    compose = (ROOT / "examples/docker-compose.yml").read_text()
    assert (
        "./config:/config:ro" in compose and "tcc-data:/data" in compose and "env_file" in compose
    )
    assert not re.search(r"(NS_API_KEY|TELEGRAM_BOT_TOKEN|OWNTRACKS_SECRET)\s*[:=]\s*\S", compose)
    docker = (ROOT / "Dockerfile").read_text()
    assert "USER 10001" in docker and "HEALTHCHECK" in docker and "uv sync --locked" in docker
    assert ".env" in (ROOT / ".gitignore").read_text().split()
    assert ".env" in (ROOT / ".dockerignore").read_text().split()


def test_documented_setup_builds_from_examples_and_recreates_on_env_changes():
    compose = (ROOT / "examples/docker-compose.yml").read_text()
    build = re.search(r"^\s+build:\s*(\S+)", compose, re.MULTILINE)
    assert build is not None
    assert (ROOT / "examples" / build[1] / "Dockerfile").is_file()  # compose resolves from its dir
    docker = (ROOT / "docs/docker.md").read_text()
    setup = docker[docker.index("## First run") : docker.index("## Commands")]
    assert "`examples/` directory" in setup and "cd examples" in setup
    assert "cp profiles.yaml config/" in setup  # ./config next to the compose file
    assert "examples/config/" in (ROOT / ".gitignore").read_text().split()
    for doc in ("docs/docker.md", "docs/location-owntracks.md"):
        text = (ROOT / doc).read_text()
        assert not re.search(r"\band restart\b|compose restart", text), doc  # keeps the old env
        assert "docker compose up -d --force-recreate tcc" in text, doc
