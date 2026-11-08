from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "examples/compose.household.yaml"


def test_household_services_have_separate_configuration_state_and_local_ports():
    config = yaml.safe_load(COMPOSE.read_text())
    services = config["services"]
    assert set(services) == {"alice", "bob", "alice-mcp", "bob-mcp"}
    paths = set()
    for index, person in enumerate(("alice", "bob")):
        app, relay = services[person], services[f"{person}-mcp"]
        assert app["build"] == ".."
        assert app["env_file"] == [f"./config/household/{person}/.env"]
        assert app["volumes"] == [
            f"./config/household/{person}/profiles:/config:ro",
            f"{person}-data:/data",
        ]
        assert app["ports"] == [f"127.0.0.1:{8081 + index}:8080"]
        assert app["networks"] == [person]
        assert app["environment"] == {
            "TCC_STATE_DIR": "/data",
            "TCC_PROFILES": "/config/profiles.yaml",
        }
        assert app["env_file"][0] not in paths
        paths.add(app["env_file"][0])
        assert relay["volumes"] == [f"{person}-data:/data:ro"]
        assert relay["network_mode"] == "none"
        assert "env_file" not in relay and "ports" not in relay
        assert relay["environment"] == {"TCC_STATE_DIR": "/data"}
        assert relay["profiles"] == ["mcp"] and relay["healthcheck"]["disable"]
        assert relay["command"] == ["python", "-m", "tcc_app.mcp_server"]
    assert set(config["volumes"]) == {"alice-data", "bob-data"}
    assert set(config["networks"]) == {"alice", "bob"}


def test_household_defaults_do_not_grant_host_or_shared_secret_access():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    for service in services.values():
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert not service.get("privileged")
        assert not service.get("pid") and not service.get("ipc")
        assert service.get("network_mode") != "host"
        assert all("docker.sock" not in volume for volume in service["volumes"])
    template = (ROOT / "examples/household.env.example").read_text()
    assignments = dict(
        line.split("=", 1) for line in template.splitlines() if line and not line.startswith("#")
    )
    assert assignments["TELEGRAM_CHAT_ID"] == ""
    assert assignments["OWNTRACKS_SECRET"] == ""
    assert assignments["TCC_HEALTH_TOKEN"] == ""
    assert all(not name.startswith(("SMTP_", "TCC_WEBHOOK")) for name in assignments)
    assert all("${" not in str(service) for service in services.values())
