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


def test_mcp_relay_gets_the_state_directory_and_no_secret():
    services = yaml.safe_load((ROOT / "examples/docker-compose.yml").read_text())["services"]
    assert "env_file" not in services["mcp"]
    assert set(services["mcp"]["environment"]) == {"TCC_STATE_DIR"}


def test_single_companion_env_template_has_required_placeholders_and_private_defaults():
    template = (ROOT / "examples/.env.example").read_text()
    assignments = dict(
        line.split("=", 1) for line in template.splitlines() if line and not line.startswith("#")
    )
    assert assignments["NS_API_KEY"].startswith("replace-with-")
    assert assignments["TELEGRAM_BOT_TOKEN"].startswith("replace-with-")
    assert assignments["TELEGRAM_CHAT_ID"] == ""
    assert assignments["TCC_HEALTH_TOKEN"] == ""
    assert assignments["OWNTRACKS_SECRET"] == ""
    assert "NS-App" in template and "private chat" in template
    assert "auto" in template.lower() and "/data/health-token" in template


def test_documented_setup_builds_from_examples_and_recreates_on_env_changes():
    compose = yaml.safe_load((ROOT / "examples/docker-compose.yml").read_text())["services"]
    assert "build" not in compose["tcc"]  # a failed pull must not fall back to a missing Dockerfile
    docker = (ROOT / "docs/docker.md").read_text()
    build = re.search(r"docker build -t (\S+) (\S+)\n", docker)
    assert build is not None and build[1] == compose["tcc"]["image"] == compose["mcp"]["image"]
    assert (ROOT / "examples" / build[2] / "Dockerfile").is_file()  # run from examples/
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
    assert project["dependencies"] == ["PyYAML>=6.0.1", "httpx>=0.27", "tzdata"]
    assert project["optional-dependencies"]["app"] == ["mcp>=2.3,<3", "segno>=1.6.6,<2"]
    for f in (ROOT / "src").rglob("*.py"):
        assert not re.search(r"^\s*(import|from) mcp\b", f.read_text(), re.MULTILINE), f
    code = "import sys, tcc_app.main; print('mcp' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": "app"}, check=True, timeout=60)  # fmt: skip
    assert out.stdout.strip() == "False"


def test_dockerfile_installs_the_app_extra():
    docker = (ROOT / "Dockerfile").read_text()
    syncs = [x for x in docker.splitlines() if "uv sync --locked" in x]
    assert len(syncs) == 2 and all("--extra app" in x for x in syncs)
    smoke = next(x for x in docker.splitlines() if x.startswith("RUN python -c"))
    assert "import tcc_app.mcp_server" in smoke


def test_mcp_doc_keeps_the_default_relay_local_and_read_only():
    doc = (ROOT / "docs/mcp.md").read_text()
    local = doc.split("## Connect an agent", 1)[0]
    for name in ("get_status", "get_next_connections", "get_works_ahead"):
        assert name in local
    # the relay runs in its own container: `exec` would need the app running and die with it,
    # so the fixed "app is not running" error could never be answered
    assert local.count("run --rm --no-deps -T mcp") == 1
    assert '"run", "--rm", "--no-deps", "-T", "mcp"' in local and "exec" not in local
    assert "three read-only tools" in local
    assert "ports:" not in local and "0.0.0.0" not in local
    assert "Never put a network bridge or proxy in front of it" in local
    assert "run --rm --no-deps -T -e TCC_MCP_AGENT_TOKEN mcp" in doc
    assert "mcp.md" in (ROOT / "docs/docker.md").read_text()


def test_mcp_remote_doc_publishes_only_on_the_encrypted_private_interface():
    compose = (ROOT / "examples/docker-compose.yml").read_text()
    assert "TCC_MCP_REMOTE_PORT" not in compose and "8765" not in compose
    doc = (ROOT / "docs/mcp.md").read_text().split("## Private-network access", 1)[1]
    override = yaml.safe_load(doc.split("```yaml\n", 1)[1].split("```", 1)[0])["services"]["tcc"]
    private_ip = "${TCC_MCP_PRIVATE_IP:?Set the encrypted private-network IP}"
    assert override["environment"]["TCC_MCP_REMOTE_PORT"] == "8765"
    assert override["environment"]["TCC_MCP_REMOTE_BIND"] == "0.0.0.0"
    assert override["environment"]["TCC_MCP_REMOTE_ALLOWED_HOSTS"] == f"{private_ip}:8765"
    assert override["ports"] == [f"{private_ip}:8765:8765"]
    assert "encrypted private network" in doc and "Bearer <agent-token>" in doc


def test_the_mcp_relay_service_has_no_app_healthcheck():
    # the image's check probes the app's port; a relay container would show unhealthy
    svc = yaml.safe_load((ROOT / "examples/docker-compose.yml").read_text())["services"]
    mcp = svc["mcp"]
    assert mcp["healthcheck"] == {"disable": True} and mcp["profiles"] == ["mcp"]
    assert mcp["command"] == ["python", "-m", "tcc_app.mcp_server"]
    assert "tcc-data:/data:ro" in mcp["volumes"] and mcp["image"] == svc["tcc"]["image"]
