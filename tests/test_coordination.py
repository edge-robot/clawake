import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from clawake.cli import app
from clawake.config import Inventory, load_inventory
from clawake.services.deployment import apply_artifacts, plan_deployment
from clawake.services.render import render_inventory
from clawake.services.runtime_config import STATE_FILE
from clawake.services.systemd import CommandResult

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


@pytest.fixture
def team(tmp_path):
    data = load_inventory(Path("robotics-team/team.yml")).model_dump()
    data["hosts"][0]["quadlet_root"] = str(tmp_path / "quadlets")
    for member in data["instances"]:
        member["workspace_path"] = str(tmp_path / member["name"] / "workspace")
        member["team_definition_path"] = str(tmp_path / member["name"] / "role")
        member["env_files"] = [str(tmp_path / (member["name"] + ".env"))]
    return Inventory.model_validate(data)


def config_path(member):
    return Path(member.workspace_path) / ".openclaw/openclaw.json"


@pytest.mark.parametrize("first,second", [
    ("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"),
    ("127.0.0.1", "0.0.0.0"), ("::", "127.0.0.1"),
    ("::1", "0:0:0:0:0:0:0:1"), ("::ffff:127.0.0.1", "127.0.0.1"),
])
def test_overlapping_host_ports_rejected(team, first, second):
    data = team.model_dump()
    one, two = data["instances"][:2]
    one["ports"][0]["bind_address"] = first
    two["ports"][0].update(bind_address=second, host_port=one["ports"][0]["host_port"])
    with pytest.raises(ValueError, match="Port collision"):
        Inventory.model_validate(data)


def test_separate_addresses_and_container_ports_are_allowed(team):
    data = team.model_dump()
    one, two = data["instances"][:2]
    two["ports"][0].update(bind_address="127.0.0.2", host_port=one["ports"][0]["host_port"])
    Inventory.model_validate(data)


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d.update(unknown=True), "Extra inputs"),
    (lambda d: d["instances"][0]["openclaw"]["subagents"].update(max_concurent=4), "Extra inputs"),
    (lambda d: d["instances"][0].update(networks=["missing"]), "network"),
    (lambda d: d["instances"][0].update(networks=[]), "shared network"),
    (lambda d: d["instances"][0]["openclaw"]["a2a"]["peers"]["engineering"].update(
        instance="missing"), "A2A target"),
    (lambda d: d["instances"][0]["openclaw"]["a2a"]["peers"]["engineering"].update(
        outbound_token_env="WRONG_DIRECTION"), "reciprocal token"),
    (lambda d: d["instances"][1].update(container_name="robotics-product-owner"), "container"),
    (lambda d: d["networks"][0].update(name="../escape"), "pattern"),
])
def test_invalid_topology_rejected(team, mutate, match):
    data = team.model_dump()
    mutate(data)
    with pytest.raises(ValueError, match=match):
        Inventory.model_validate(data)


def test_plan_is_read_only_and_shared_network_is_unique(team, tmp_path):
    changes = plan_deployment(team, team.instances, TEMPLATES)
    assert not list(tmp_path.iterdir())
    assert len([c for c in changes if c.role == "shared_network"]) == 1
    member_plan = plan_deployment(team, [team.instances[1]], TEMPLATES)
    assert len([c for c in member_plan if c.role == "shared_network"]) == 1
    containers = [c.content for c in changes if c.destination.suffix == ".container"]
    assert all("Network=robotics-coordination.network" in text for text in containers)
    assert all("/team-shared" not in text for text in containers)


def test_runtime_merge_preserves_operator_config_and_secret_references(team, monkeypatch):
    member = team.instances[0]
    path = config_path(member)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "models": {"providers": {"custom": {"apiKey": "private-existing-key"}}},
        "agents": {"entries": {"main": {"model": "custom/model"}}},
        "bindings": [{"agentId": "main", "match": {"channel": "telegram"}}],
    }))
    monkeypatch.setenv("A2A_ENGINEERING_TO_PRODUCT", "must-not-be-expanded")
    plan = plan_deployment(team, team.instances, TEMPLATES)
    assert "private-existing-key" not in repr(plan)
    apply_artifacts(plan)
    payload = json.loads(path.read_text())
    assert payload["models"]["providers"]["custom"]["apiKey"] == "private-existing-key"
    assert payload["agents"]["entries"]["main"]["model"] == "custom/model"
    assert payload["channels"]["a2a"]["peers"]["engineering"] == {
        "token": "${A2A_ENGINEERING_TO_PRODUCT}",
        "outboundToken": "${A2A_PRODUCT_TO_ENGINEERING}",
        "url": "http://robotics-engineer:18789/a2a/v1",
    }
    assert [b["match"]["channel"] for b in payload["bindings"]] == ["telegram", "a2a"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "private-existing-key" not in path.with_name(STATE_FILE).read_text()
    assert plan_deployment(team, team.instances, TEMPLATES) == []


def test_removed_peers_are_revoked_but_unmanaged_channels_survive(team):
    apply_artifacts(plan_deployment(team, team.instances, TEMPLATES))
    po, engineer = team.instances[:2]
    path = config_path(po)
    payload = json.loads(path.read_text())
    payload["channels"]["telegram"] = {"enabled": True}
    path.write_text(json.dumps(payload))
    del po.openclaw.a2a.peers["engineering"]
    engineer.openclaw.a2a.peers.clear()
    engineer.openclaw.a2a.enabled = False
    apply_artifacts(plan_deployment(team, team.instances, TEMPLATES))
    updated = json.loads(path.read_text())
    assert "engineering" not in updated["channels"]["a2a"]["peers"]
    assert "partnerships" in updated["channels"]["a2a"]["peers"]
    assert updated["channels"]["telegram"] == {"enabled": True}
    engineer_config = json.loads(config_path(engineer).read_text())
    assert engineer_config["channels"]["a2a"]["enabled"] is False
    assert engineer_config["plugins"]["entries"]["a2a"]["enabled"] is False
    assert engineer_config["bindings"] == []


def test_conflicting_unmanaged_settings_are_not_overwritten(team):
    path = config_path(team.instances[0])
    path.parent.mkdir(parents=True)
    original = '{"tools": {"sessions": {"visibility": "self"}}}'
    path.write_text(original)
    with pytest.raises(ValueError, match="unmanaged OpenClaw field"):
        plan_deployment(team, team.instances, TEMPLATES)
    assert path.read_text() == original


def test_apply_refuses_stale_plan_before_any_writes(team):
    plan = plan_deployment(team, team.instances, TEMPLATES)
    path = config_path(team.instances[0])
    path.parent.mkdir(parents=True)
    path.write_text('{"operator": "edited"}')
    with pytest.raises(ValueError, match="changed since planning"):
        apply_artifacts(plan)
    assert not Path(team.hosts[0].quadlet_root).exists()


@pytest.mark.parametrize("member,remove_network", [("robotics-engineer", False), (None, True)])
def test_teardown_keeps_network_for_other_members(team, tmp_path, monkeypatch, member,
                                                 remove_network):
    from clawake import cli
    removed = []

    class Service:
        def stop(self, *args, **kwargs):
            return CommandResult([], 0, "", "")

        disable = stop
        remove_container = stop
        daemon_reload = stop

        def remove_quadlet(self, path, **kwargs):
            removed.append(Path(path).name)
            return CommandResult([], 0, "", "")

    monkeypatch.setattr(cli, "SystemdService", Service)
    cfg = tmp_path / "inventory.yml"
    cfg.write_text(yaml.safe_dump(team.model_dump()))
    args = ["teardown", "-c", str(cfg), "--execute"]
    if member:
        args += ["-m", member]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert ("robotics-coordination.network" in removed) is remove_network


def test_setup_and_upgrade_preview_same_runtime_plan(team, tmp_path):
    cfg = tmp_path / "inventory.yml"
    cfg.write_text(yaml.safe_dump(team.model_dump()))
    member = team.instances[0]
    runner = CliRunner()
    setup = runner.invoke(app, ["setup", "-c", str(cfg), "-m", member.name])
    upgrade = runner.invoke(app, ["upgrade", "-c", str(cfg), "-m", member.name,
                                  "--to", member.image.tag])
    assert setup.exit_code == upgrade.exit_code == 0
    assert [line for line in setup.output.splitlines() if "runtime:" in line] == [
        line for line in upgrade.output.splitlines() if "runtime:" in line
    ]
    assert not config_path(member).exists()


def test_setup_missing_a2a_secrets_does_not_write(team, tmp_path):
    cfg = tmp_path / "inventory.yml"
    cfg.write_text(yaml.safe_dump(team.model_dump()))
    result = CliRunner().invoke(app, ["setup", "-c", str(cfg), "--execute"])
    assert result.exit_code == 2
    assert "Missing environment values" in result.output
    assert not Path(team.hosts[0].quadlet_root).exists()
    assert not config_path(team.instances[0]).exists()


def test_mismatched_peer_credentials_fail_before_writes(team, tmp_path):
    for index, member in enumerate(team.instances):
        lines = ["OPENCLAW_GATEWAY_TOKEN=gateway"]
        for peer in member.openclaw.a2a.peers.values():
            lines.extend(f"{key}=different-{index}" for key in
                         (peer.inbound_token_env, peer.outbound_token_env))
        Path(member.env_files[0]).write_text("\n".join(lines))
    cfg = tmp_path / "inventory.yml"
    cfg.write_text(yaml.safe_dump(team.model_dump()))
    result = CliRunner().invoke(app, ["setup", "-c", str(cfg), "--execute"])
    assert result.exit_code == 2
    assert "token mismatch" in result.output
    assert "different-" not in result.output
    assert not Path(team.hosts[0].quadlet_root).exists()


def test_ipv6_publish_port_is_bracketed(team):
    team.instances[0].ports[0].bind_address = "::1"
    plan = plan_deployment(team, [team.instances[0]], TEMPLATES)
    text = next(c.content for c in plan if c.destination.suffix == ".container")
    assert "PublishPort=[::1]:19089:18789/tcp" in text


def test_quadlet_generator_accepts_shared_network(team, tmp_path):
    generator = Path("/usr/lib/podman/quadlet")
    if not generator.exists():
        pytest.skip("Podman Quadlet generator not installed")
    root = tmp_path / "rendered"
    render_inventory(team, root, TEMPLATES)
    result = subprocess.run(
        [str(generator), "-user", "-dryrun"],
        env={**os.environ, "QUADLET_UNIT_DIRS": str(root)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "unsupported key" not in result.stderr.lower()
    assert "robotics-coordination-network.service" in result.stdout
    assert "--network robotics-coordination" in result.stdout


def test_lead_policy_changes_are_applied_without_losing_model(team):
    apply_artifacts(plan_deployment(team, team.instances, TEMPLATES))
    member = team.instances[0]
    path = config_path(member)
    payload = json.loads(path.read_text())
    payload["agents"]["defaults"]["model"] = {"primary": "operator/model"}
    path.write_text(json.dumps(payload))
    member.openclaw.subagents.max_concurrent = 2
    plan = plan_deployment(team, [member], TEMPLATES)
    assert "agents/defaults/subagents/maxConcurrent" in plan[0].changed_keys
    apply_artifacts(plan)
    updated = json.loads(path.read_text())
    assert updated["agents"]["defaults"]["subagents"]["maxConcurrent"] == 2
    assert updated["agents"]["defaults"]["model"] == {"primary": "operator/model"}


def test_upgrade_replans_after_migration_and_preserves_operator_settings(
    team, tmp_path, monkeypatch,
):
    from clawake import cli

    apply_artifacts(plan_deployment(team, team.instances, TEMPLATES))
    member = team.instances[0]
    member.openclaw.subagents.max_concurrent = 2
    for item in team.instances:
        values = {"OPENCLAW_GATEWAY_TOKEN": "test-gateway"}
        for peer in item.openclaw.a2a.peers.values():
            for key in (peer.inbound_token_env, peer.outbound_token_env):
                values[key] = "test-" + key
        Path(item.env_files[0]).write_text("\n".join(f"{k}={v}" for k, v in values.items()))
    cfg = tmp_path / "inventory.yml"
    cfg.write_text(yaml.safe_dump(team.model_dump()))

    class Service:
        def stop(self, *args, **kwargs):
            return CommandResult([], 0, "", "")

        restart = stop
        daemon_reload = stop

    def migrate(instance, image):
        path = config_path(instance)
        payload = json.loads(path.read_text())
        payload["models"] = {"providers": {"migrated": {"apiKey": "secret-marker"}}}
        path.write_text(json.dumps(payload))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "SystemdService", Service)
    monkeypatch.setattr(cli, "check_image_availability", lambda image: None)
    monkeypatch.setattr(cli, "run_doctor", migrate)
    monkeypatch.setattr(cli, "wait_for_health", lambda instance: (True, "healthy"))
    monkeypatch.setattr(cli, "verify_runtime", lambda instance, image: SimpleNamespace(
        healthy=True, version="2026.9.4", image_name="verified", error="",
    ))
    result = CliRunner().invoke(app, ["upgrade", "-c", str(cfg), "-m", member.name,
                                    "--to", member.image.tag, "--execute"])
    assert result.exit_code == 0, result.output
    assert "secret-marker" not in result.output
    payload = json.loads(config_path(member).read_text())
    assert payload["agents"]["defaults"]["subagents"]["maxConcurrent"] == 2
    assert payload["models"]["providers"]["migrated"]["apiKey"] == "secret-marker"
