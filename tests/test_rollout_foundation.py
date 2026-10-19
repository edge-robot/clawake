"""Regression tests for the integration of the Onshape MCP line and generic MCP provisioning.

The fixture mirrors the shape of a deployed two-member team with synthetic values only.
Golden files pin the rendered Quadlets and the planned OpenClaw runtime configuration;
regenerate them with ``CLAWAKE_UPDATE_GOLDEN=1 uv run pytest tests/test_rollout_foundation.py``
and review every changed expectation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clawake.config import Inventory, load_inventory
from clawake.services.gateway_config import ensure_plugin_runtime_config
from clawake.services.render import render_instance_assets, render_shared_network
from clawake.services.runtime_config import STATE_FILE, desired_fields, plan_runtime_files

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rollout-foundation"
INVENTORY = FIXTURE_ROOT / "team.yml"
GOLDEN = FIXTURE_ROOT / "golden"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
HOSTED_ONSHAPE = {"url": "https://fs-mcp.labs.onshape.app/mcp", "enabled": False}


def _raw() -> dict:
    return yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))


def _redirected(tmp_path: Path, raw: dict | None = None) -> Inventory:
    """Fixture inventory whose runtime state directories live below tmp_path."""
    data = raw if raw is not None else _raw()
    for member in data["instances"]:
        member["state_path"] = str(tmp_path / member["name"] / "state")
    return Inventory.model_validate(data)


def _apply(plan) -> None:
    for path, content, _previous, _keys in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _config_path(inventory: Inventory, name: str) -> Path:
    member = next(item for item in inventory.instances if item.name == name)
    return member.runtime_state_path / "openclaw.json"


def _member(inventory: Inventory, name: str):
    return next(item for item in inventory.instances if item.name == name)


def build_golden(state_root: Path) -> dict[str, str]:
    """Render every golden artifact in memory; runtime state is planned below state_root."""
    inventory = load_inventory(INVENTORY)
    artifacts: dict[str, str] = {}
    for network in inventory.networks:
        artifacts[f"quadlet/{network.quadlet_path}"] = render_shared_network(network)
    for instance in inventory.instances:
        for path, content in render_instance_assets(instance, TEMPLATES).items():
            artifacts[f"quadlet/{path}"] = content
    redirected = _redirected(state_root)
    for instance in redirected.instances:
        artifacts[f"runtime/{instance.name}.desired.json"] = (
            json.dumps(desired_fields(redirected, instance), indent=2, sort_keys=True) + "\n"
        )
        for path, content, _previous, _keys in plan_runtime_files(redirected, instance):
            artifacts[f"runtime/{instance.name}.{path.name}"] = content
        # Plugin grants need an operator-owned tools.allow; seed a synthetic one.
        config_path = instance.runtime_state_path / "openclaw.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"tools": {"allow": ["read"]}}), encoding="utf-8")
        ensure_plugin_runtime_config(instance)
        for path in sorted(instance.runtime_state_path.iterdir()):
            artifacts[f"plugin-runtime/{instance.name}.{path.name}"] = path.read_text(
                encoding="utf-8"
            )
    return artifacts


def test_fixture_render_and_runtime_plan_match_golden(tmp_path: Path) -> None:
    artifacts = build_golden(tmp_path)
    if os.environ.get("CLAWAKE_UPDATE_GOLDEN") == "1":
        for relative, content in artifacts.items():
            target = GOLDEN / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    expected = {
        str(path.relative_to(GOLDEN)): path.read_text(encoding="utf-8")
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file()
    }
    assert sorted(artifacts) == sorted(expected)
    for relative, content in artifacts.items():
        assert content == expected[relative], relative


def test_fixture_keeps_onshape_flags_and_state_path() -> None:
    inventory = load_inventory(INVENTORY)
    engineer = _member(inventory, "example-engineer")
    assert engineer.onshape_mcp and engineer.onshape_mcp_enabled
    assert engineer.openclaw is not None and engineer.openclaw.mcp_servers == {}
    assert "localMcp" in engineer.plugins[0].config
    server = desired_fields(inventory, engineer)["mcp/servers/onshape"]
    assert server["url"] == "http://clawonshape-mcp:8080/mcp"
    assert server["enabled"] is True
    container = render_instance_assets(engineer, TEMPLATES)[engineer.quadlet_path]
    assert "Volume=/srv/example/engineer/state:/home/node/.openclaw" in container


# Constraint 3: both MCP models coexist, but never configure the same server name.


def _with_mcp_server(name: str) -> dict:
    data = _raw()
    engineer = next(item for item in data["instances"] if item["name"] == "example-engineer")
    engineer["openclaw"]["mcp_servers"] = {
        name: {
            "url": "https://mcp.example.test/mcp",
            "tools": ["inspect"],
            "oauth_scope": "read",
        }
    }
    return data


def test_onshape_flag_and_mcp_servers_onshape_are_rejected() -> None:
    with pytest.raises(ValidationError, match="configured by both onshape_mcp"):
        Inventory.model_validate(_with_mcp_server("onshape"))


def test_onshape_flag_and_mcp_servers_onshape_conflict_without_enabled_flag() -> None:
    data = _with_mcp_server("onshape")
    engineer = next(item for item in data["instances"] if item["name"] == "example-engineer")
    engineer["onshape_mcp_enabled"] = False
    with pytest.raises(ValidationError, match="declare it in only one place"):
        Inventory.model_validate(data)


def test_desired_fields_refuses_override_even_without_validation() -> None:
    from clawake.config import McpServerSpec

    inventory = load_inventory(INVENTORY)
    engineer = _member(inventory, "example-engineer")
    assert engineer.openclaw is not None
    # Bypass model validation on purpose: planning must still fail closed.
    engineer.openclaw.mcp_servers["onshape"] = McpServerSpec(
        url="https://mcp.example.test/mcp", tools=["inspect"], oauth_scope="read"
    )
    with pytest.raises(ValueError, match="configured by onshape_mcp and mcp_servers"):
        desired_fields(inventory, engineer)


def test_onshape_flag_and_distinct_mcp_server_coexist(tmp_path: Path) -> None:
    inventory = _redirected(tmp_path, _with_mcp_server("cad"))
    engineer = _member(inventory, "example-engineer")
    desired = desired_fields(inventory, engineer)
    assert desired["mcp/servers/onshape"]["url"] == "http://clawonshape-mcp:8080/mcp"
    assert desired["mcp/servers/cad"]["url"] == "https://mcp.example.test/mcp"
    _apply(plan_runtime_files(inventory, engineer))
    servers = json.loads(_config_path(inventory, "example-engineer").read_text())["mcp"]["servers"]
    assert sorted(servers) == ["cad", "onshape"]
    assert plan_runtime_files(inventory, engineer) == []


# Constraint 4: drift guard and hosted-connector adoption, unchanged re-run is a no-op.


def test_unchanged_rerun_of_fixture_is_a_noop(tmp_path: Path) -> None:
    inventory = _redirected(tmp_path)
    for member in inventory.instances:
        _apply(plan_runtime_files(inventory, member))
    for member in inventory.instances:
        assert plan_runtime_files(inventory, member) == []


def test_hosted_connector_is_adopted_and_then_guarded(tmp_path: Path) -> None:
    inventory = _redirected(tmp_path)
    engineer = _member(inventory, "example-engineer")
    config_path = _config_path(inventory, "example-engineer")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"mcp": {"servers": {"onshape": HOSTED_ONSHAPE}}}))

    plan = plan_runtime_files(inventory, engineer)
    _apply(plan)
    server = json.loads(config_path.read_text())["mcp"]["servers"]["onshape"]
    assert server == desired_fields(inventory, engineer)["mcp/servers/onshape"]
    ledger = json.loads((config_path.parent / STATE_FILE).read_text())
    assert ledger["fields"]["mcp/servers/onshape"] == server

    # Unchanged re-run after adoption: no plan, the drift guard does not fire.
    assert plan_runtime_files(inventory, engineer) == []

    # Changing the adopted server outside Clawake trips the drift guard.
    payload = json.loads(config_path.read_text())
    payload["mcp"]["servers"]["onshape"]["enabled"] = False
    config_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Managed field changed outside Clawake"):
        plan_runtime_files(inventory, engineer)


def test_hosted_connector_reappearing_after_adoption_is_drift(tmp_path: Path) -> None:
    inventory = _redirected(tmp_path)
    engineer = _member(inventory, "example-engineer")
    config_path = _config_path(inventory, "example-engineer")
    _apply(plan_runtime_files(inventory, engineer))
    payload = json.loads(config_path.read_text())
    payload["mcp"]["servers"]["onshape"] = HOSTED_ONSHAPE
    config_path.write_text(json.dumps(payload))
    # Adoption is one-time only; a managed field is never silently re-adopted.
    with pytest.raises(ValueError, match="Managed field changed outside Clawake"):
        plan_runtime_files(inventory, engineer)


def test_enabled_or_unknown_connector_is_not_adopted(tmp_path: Path) -> None:
    inventory = _redirected(tmp_path)
    engineer = _member(inventory, "example-engineer")
    config_path = _config_path(inventory, "example-engineer")
    config_path.parent.mkdir(parents=True)
    for server in [
        {**HOSTED_ONSHAPE, "enabled": True},
        {"url": "https://mcp.example.test/mcp", "enabled": False},
    ]:
        config_path.write_text(json.dumps({"mcp": {"servers": {"onshape": server}}}))
        with pytest.raises(ValueError, match="Refusing to overwrite unmanaged"):
            plan_runtime_files(inventory, engineer)


def test_toggling_onshape_enabled_flag_is_not_drift(tmp_path: Path) -> None:
    data = _raw()
    engineer_raw = next(item for item in data["instances"] if item["name"] == "example-engineer")
    engineer_raw["onshape_mcp_enabled"] = False
    inventory = _redirected(tmp_path, data)
    _apply(plan_runtime_files(inventory, _member(inventory, "example-engineer")))
    engineer_raw["onshape_mcp_enabled"] = True
    inventory = _redirected(tmp_path, data)
    engineer = _member(inventory, "example-engineer")
    plan = plan_runtime_files(inventory, engineer)
    assert any("mcp/servers/onshape/enabled" in plan_keys for *_rest, plan_keys in plan)
    _apply(plan)
    assert plan_runtime_files(inventory, engineer) == []


# Constraint 5: global grants (no host-policy widening) and preserved unmanaged grants.


def test_global_and_agent_grants_preserve_operator_policy_together(tmp_path: Path) -> None:
    inventory = _redirected(tmp_path)
    engineer = _member(inventory, "example-engineer")
    agent_id = "example-engineer"
    config_path = _config_path(inventory, agent_id)
    state_path = config_path.with_name("clawake-managed-plugin-runtime.json")
    config_path.parent.mkdir(parents=True)
    operator = {
        "tools": {"allow": ["read", "cad_status"], "deny": ["exec"], "profile": "coding"},
        "agents": {
            "entries": {agent_id: {"tools": {"alsoAllow": ["operator_tool", "cad_inspect"]}}}
        },
    }
    config_path.write_text(json.dumps(operator))

    assert ensure_plugin_runtime_config(engineer)[1]
    payload = json.loads(config_path.read_text())
    # Grants are additive to the operator allowlists and never touch deny/profile.
    assert payload["tools"]["deny"] == ["exec"]
    assert payload["tools"]["profile"] == "coding"
    assert payload["tools"]["allow"] == [
        "read",
        "cad_status",
        *[tool for tool in engineer.global_tool_allow if tool != "cad_status"],
    ]
    assert payload["agents"]["entries"][agent_id]["tools"]["alsoAllow"] == [
        "operator_tool",
        "cad_inspect",
        *[tool for tool in engineer.agent_tool_allow[agent_id] if tool != "cad_inspect"],
    ]
    ledger = json.loads(state_path.read_text())
    assert "cad_status" not in ledger["globalToolAllow"]
    assert "cad_inspect" not in ledger["agentToolAllow"][agent_id]["tools"]

    # Unchanged re-run: no config change.
    assert not ensure_plugin_runtime_config(engineer)[1]

    # Withdrawing both grant kinds keeps exactly the operator-owned names.
    engineer.global_tool_allow = []
    engineer.agent_tool_allow = {}
    assert ensure_plugin_runtime_config(engineer)[1]
    payload = json.loads(config_path.read_text())
    assert payload["tools"] == operator["tools"]
    assert payload["agents"]["entries"][agent_id]["tools"] == {
        "alsoAllow": ["operator_tool", "cad_inspect"]
    }


def test_global_grants_never_create_a_host_allowlist(tmp_path: Path) -> None:
    inventory = _redirected(tmp_path)
    engineer = _member(inventory, "example-engineer")
    config_path = _config_path(inventory, "example-engineer")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"tools": {"profile": "coding"}}))
    with pytest.raises(ValueError, match="existing non-empty tools.allow"):
        ensure_plugin_runtime_config(engineer)
    assert json.loads(config_path.read_text()) == {"tools": {"profile": "coding"}}


def test_v2_setup_prepares_state_path_not_workspace_openclaw(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import Mock

    from clawake.services.resources import lifecycle

    data = _raw()
    data["version"] = 2
    data["hosts"][0]["quadlet_root"] = str(tmp_path / "quadlets")
    for member in data["instances"]:
        member["workspace_path"] = str(tmp_path / member["name"] / "workspace")
        member["team_definition_path"] = str(tmp_path / member["name"] / "role")
    inventory = _redirected(tmp_path, data)
    runtime = Mock()
    runtime.daemon_reload.return_value.return_code = 0
    runtime.restart.return_value.return_code = 0
    monkeypatch.setattr("clawake.services.resources.SystemdService", lambda: runtime)
    monkeypatch.setattr("clawake.services.resources.wait_ready", lambda resource: True)
    monkeypatch.setattr("subprocess.run", Mock(side_effect=AssertionError("no subprocess")))

    lifecycle(
        inventory,
        "setup",
        "example-lead",
        None,
        TEMPLATES,
        execute=True,
        prepare_instances=lambda instances: None,
    )

    lead = _member(inventory, "example-lead")
    assert lead.runtime_state_path.is_dir()
    assert not (Path(lead.workspace_path) / ".openclaw").exists()


def _rendered_artifacts(inventory_path: Path) -> dict[str, str]:
    """Quadlets and runtime plans of an inventory loaded through load_inventory (overlay-aware)."""
    inventory = load_inventory(inventory_path)
    artifacts: dict[str, str] = {}
    for network in inventory.networks:
        artifacts[f"quadlet/{network.quadlet_path}"] = render_shared_network(network)
    for instance in inventory.instances:
        for path, content in render_instance_assets(instance, TEMPLATES).items():
            artifacts[f"quadlet/{path}"] = content
        artifacts[f"runtime/{instance.name}.desired.json"] = (
            json.dumps(desired_fields(inventory, instance), indent=2, sort_keys=True) + "\n"
        )
        for path, content, _previous, _keys in plan_runtime_files(inventory, instance):
            artifacts[f"runtime/{instance.name}.{path.name}"] = content
    return artifacts


def test_split_inventory_and_overlay_render_identically_to_fixture(tmp_path: Path) -> None:
    """ADR-004: inventory + runtime pin overlay is byte-identical to the fully pinned fixture."""
    split = _raw()
    overlay: dict = {"version": 1, "cluster": split["cluster"]["name"], "members": {}}
    for member in split["instances"]:
        image = {
            key: member["image"].pop(key)
            for key in ("tag", "digest", "known_good_tag", "known_good_digest")
        }
        plugins = {
            plugin["id"]: {
                "artifact_path": plugin.pop("artifact_path"),
                "sha256": plugin.pop("sha256"),
            }
            for plugin in member["plugins"]
        }
        overlay["members"][member["name"]] = {"image": image, "plugins": plugins}
    split["runtime_pins"] = "private/pins.json"
    split_path = tmp_path / "team.yml"
    split_path.write_text(yaml.safe_dump(split, sort_keys=False), encoding="utf-8")
    pins = tmp_path / "private" / "pins.json"
    pins.parent.mkdir()
    pins.write_text(json.dumps(overlay, indent=2), encoding="utf-8")
    pins.chmod(0o600)
    assert "sha256" not in split_path.read_text(encoding="utf-8")

    expected = _rendered_artifacts(INVENTORY)
    actual = _rendered_artifacts(split_path)

    assert sorted(actual) == sorted(expected)
    for relative, content in expected.items():
        assert actual[relative] == content, relative
    for relative, content in expected.items():
        if relative.startswith("quadlet/"):
            assert content == (GOLDEN / relative).read_text(encoding="utf-8"), relative
