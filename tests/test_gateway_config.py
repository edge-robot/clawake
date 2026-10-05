import json
from pathlib import Path

import yaml

from clawake.config import PluginSpec, load_inventory
from clawake.services.gateway_config import (
    ensure_control_ui_config,
    ensure_plugin_runtime_config,
    ensure_workspace_config,
)


def _instance(tmp_path: Path, *, bind_address: str = "127.0.0.1"):
    inventory_path = tmp_path / "team.yml"
    workspace = tmp_path / "workspace"
    role = tmp_path / "role"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "cluster": {"name": "c", "mode": "single_host", "primary_host": "h"},
                "hosts": [{"name": "h"}],
                "instances": [
                    {
                        "name": "one",
                        "host": "h",
                        "role": "developer",
                        "workspace_path": str(workspace),
                        "team_definition_path": str(role),
                        "quadlet_path": "one.container",
                        "container_name": "one",
                        "image": {"repository": "example.invalid/openclaw", "tag": "1"},
                        "ports": [
                            {
                                "bind_address": bind_address,
                                "host_port": 18989,
                                "container_port": 18789,
                                "protocol": "tcp",
                            },
                            {
                                "bind_address": bind_address,
                                "host_port": 18990,
                                "container_port": 18790,
                                "protocol": "tcp",
                            },
                        ],
                        "gateway_runtime": {"enabled": True, "bind": "lan"},
                        "dashboard": {"friendly_name": "One"},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return load_inventory(inventory_path).instances[0]


def test_creates_minimal_local_control_ui_config(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    config_path, changed = ensure_control_ui_config(instance)

    assert changed is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload == {
        "gateway": {
            "controlUi": {
                "allowedOrigins": [
                    "http://127.0.0.1:18989",
                    "http://localhost:18989",
                ],
            }
        }
    }
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_configures_managed_agent_workspace(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    config_path, changed = ensure_workspace_config(instance)

    assert changed is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload == {"agents": {"defaults": {"workspace": "/workspace"}}}
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_workspace_config_repairs_stale_default_and_preserves_agent_entries(
    tmp_path: Path,
) -> None:
    instance = _instance(tmp_path)
    config_path = Path(instance.workspace_path) / ".openclaw" / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"workspace": "/home/node/.openclaw/workspace"},
                    "entries": {"main": {}},
                }
            }
        ),
        encoding="utf-8",
    )

    _, changed = ensure_workspace_config(instance)

    assert changed is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["agents"] == {
        "defaults": {"workspace": "/workspace"},
        "entries": {"main": {}},
    }


def test_workspace_config_is_idempotent(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    _, first_changed = ensure_workspace_config(instance)
    _, second_changed = ensure_workspace_config(instance)

    assert first_changed is True
    assert second_changed is False


def test_merges_origins_without_overwriting_existing_settings(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    config_path = Path(instance.workspace_path) / ".openclaw" / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "gateway": {
                    "controlUi": {
                        "allowedOrigins": ["https://care.example"],
                        "allowInsecureAuth": False,
                    }
                },
                "plugins": {"entries": {"codex": {"enabled": True}}},
            }
        ),
        encoding="utf-8",
    )

    _, changed = ensure_control_ui_config(instance)

    assert changed is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["gateway"]["controlUi"] == {
        "allowedOrigins": [
            "https://care.example",
            "http://127.0.0.1:18989",
            "http://localhost:18989",
        ],
        "allowInsecureAuth": False,
    }
    assert payload["plugins"] == {"entries": {"codex": {"enabled": True}}}


def test_is_idempotent(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    _, first_changed = ensure_control_ui_config(instance)
    _, second_changed = ensure_control_ui_config(instance)

    assert first_changed is True
    assert second_changed is False


def test_does_not_enable_insecure_auth_for_non_loopback_binding(tmp_path: Path) -> None:
    instance = _instance(tmp_path, bind_address="192.0.2.10")

    config_path, changed = ensure_control_ui_config(instance)

    assert changed is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["gateway"]["controlUi"] == {"allowedOrigins": ["http://192.0.2.10:18989"]}


def test_atomically_merges_plugin_config_and_agent_tool_acl(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    instance.plugins = [
        PluginSpec(
            id="pflege-vault",
            artifact_path=str(tmp_path / "vault.tgz"),
            sha256=f"sha256:{'0' * 64}",
            enabled=True,
            config={
                "vaultPath": "/home/node/.openclaw/pflege-vault/vault.json",
                "masterKey": {
                    "source": "store",
                    "provider": "default",
                    "id": "PFLEGE_VAULT_MASTER_KEY",
                },
            },
        )
    ]
    instance.agent_tool_allow = {"main": ["pflege_vault"]}
    config_path = Path(instance.workspace_path) / ".openclaw" / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "entries": {
                        "main": {
                            "model": "example/model",
                            "tools": {"alsoAllow": ["existing_tool"]},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    _, first_changed = ensure_plugin_runtime_config(instance)
    _, second_changed = ensure_plugin_runtime_config(instance)

    assert first_changed is True
    assert second_changed is False
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["plugins"]["entries"]["pflege-vault"] == {
        "enabled": True,
        "config": instance.plugins[0].config,
    }
    assert payload["agents"]["entries"]["main"] == {
        "model": "example/model",
        "tools": {"alsoAllow": ["existing_tool", "pflege_vault"]},
    }


def test_removes_stale_managed_agent_tool_grants(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    instance.agent_tool_allow = {"main": ["pflege_vault"]}

    config_path, first_changed = ensure_plugin_runtime_config(instance)
    instance.agent_tool_allow = {}
    _, second_changed = ensure_plugin_runtime_config(instance)

    assert first_changed is True
    assert second_changed is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["agents"]["entries"]["main"]["tools"]["alsoAllow"] == []


def test_preserves_unmanaged_agent_tool_grants_during_reconciliation(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    instance.agent_tool_allow = {"main": ["pflege_vault"]}
    config_path, _ = ensure_plugin_runtime_config(instance)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["agents"]["entries"]["main"]["tools"]["alsoAllow"].append("operator_tool")
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    instance.agent_tool_allow = {"main": ["replacement_tool"]}
    _, changed = ensure_plugin_runtime_config(instance)

    assert changed is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["agents"]["entries"]["main"]["tools"]["alsoAllow"] == [
        "operator_tool",
        "replacement_tool",
    ]
