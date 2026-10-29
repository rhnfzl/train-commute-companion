import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

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


def test_core_and_app_process_never_import_the_sdk():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == ["PyYAML>=6", "httpx>=0.27"]
    assert project["optional-dependencies"]["app"] == ["mcp>=2.3,<3"]
    for f in (ROOT / "src").rglob("*.py"):
        assert not re.search(r"^\s*(import|from) mcp\b", f.read_text(), re.MULTILINE), f
    code = "import sys, tcc_app.main; print('mcp' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": "app"}, check=True)  # fmt: skip
    assert out.stdout.strip() == "False"


def test_dockerfile_installs_the_app_extra():
    docker = (ROOT / "Dockerfile").read_text()
    syncs = [x for x in docker.splitlines() if "uv sync --locked" in x]
    assert len(syncs) == 2 and all("--extra app" in x for x in syncs)
    smoke = next(x for x in docker.splitlines() if x.startswith("RUN python -c"))
    assert "import tcc_app.mcp_server" in smoke


def test_mcp_doc_is_local_only():
    doc = (ROOT / "docs/mcp.md").read_text()
    for name in ("get_status", "get_next_connections", "get_works_ahead"):
        assert name in doc
    # the relay runs in its own container: `exec` would need the app running and die with it,
    # so the fixed "app is not running" error could never be answered
    assert doc.count("run --rm --no-deps -T mcp") == 1
    assert '"run", "--rm", "--no-deps", "-T", "mcp"' in doc and "exec" not in doc
    assert "no actions" in doc.lower()
    assert "ports:" not in doc and "0.0.0.0" not in doc and "http://" not in doc
    assert "mcp.md" in (ROOT / "docs/docker.md").read_text()


def test_the_mcp_relay_service_has_no_app_healthcheck():
    # the image's check probes the app's port; a relay container would show unhealthy
    svc = yaml.safe_load((ROOT / "examples/docker-compose.yml").read_text())["services"]
    mcp = svc["mcp"]
    assert mcp["healthcheck"] == {"disable": True} and mcp["profiles"] == ["mcp"]
    assert mcp["command"] == ["python", "-m", "tcc_app.mcp_server"]
    assert "tcc-data:/data" in mcp["volumes"] and mcp["image"] == svc["tcc"]["image"]
