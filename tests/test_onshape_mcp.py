from pathlib import Path

import pytest
from pydantic import ValidationError

from clawake.services.onshape_mcp import (
    OnshapeMcpSpec,
    plan_onshape_mcp,
    render_onshape_mcp,
    validate_runtime_paths,
)


def spec(tmp_path: Path) -> OnshapeMcpSpec:
    return OnshapeMcpSpec(
        image={
            "repository": "localhost/onshape-mcp",
            "tag": "pinned",
            "digest": "sha256:" + "a" * 64,
        },
        credential_directory=str(tmp_path / "credential"),
        config_file=str(tmp_path / "config.toml"),
        quadlet_root=str(tmp_path / "quadlet"),
    )


def test_isolated_pinned_service_and_restart_health(tmp_path):
    value = spec(tmp_path)
    assets = render_onshape_mcp(value)
    text = assets["clawonshape-mcp.container"]
    assert "PublishPort=" not in text
    for item in [
        "ReadOnly=true",
        "NoNewPrivileges=true",
        "DropCapability=all",
        "User=1000:1000",
        ":/run/onshape-credentials:ro",
        "HealthOnFailure=kill",
        "Restart=on-failure",
        "MemoryMax=512M",
        "CPUQuota=100%",
    ]:
        assert item in text
    assert "refresh" not in text and "EnvironmentFile=" not in text
    assert "sha256:" + "a" * 64 in text
    assert len(plan_onshape_mcp(value)) == 2
    for name, content in assets.items():
        target = Path(value.quadlet_root) / name
        target.parent.mkdir(exist_ok=True)
        target.write_text(content)
    assert plan_onshape_mcp(value) == []


def test_no_floating_image_or_overlapping_storage(tmp_path):
    data = spec(tmp_path).model_dump()
    data["image"]["digest"] = None
    with pytest.raises(ValidationError):
        OnshapeMcpSpec.model_validate(data)
    data = spec(tmp_path).model_dump()
    data["credential_directory"] = str(tmp_path)
    with pytest.raises(ValidationError):
        OnshapeMcpSpec.model_validate(data)


def test_runtime_paths_must_be_private_without_token_reads(tmp_path):
    value = spec(tmp_path)
    with pytest.raises(FileNotFoundError):
        validate_runtime_paths(value)
    Path(value.credential_directory).mkdir(mode=0o700)
    Path(value.config_file).write_text("non-secret config")
    Path(value.config_file).chmod(0o600)
    validate_runtime_paths(value)
    Path(value.credential_directory).chmod(0o755)
    with pytest.raises(ValueError):
        validate_runtime_paths(value)


def test_native_registry_reconciliation_keeps_agent_mcp_disabled():
    from clawake.config import load_inventory
    from clawake.services.runtime_config import desired_fields

    inventory = load_inventory(Path(__file__).resolve().parents[1] / "examples/staff/team.yml")
    member = inventory.instances[0].model_copy(deep=True)
    member.onshape_mcp = True
    entry = desired_fields(inventory, member)["mcp/servers/onshape"]
    assert entry["url"] == "http://clawonshape-mcp:8080/mcp"
    assert entry["enabled"] is False and "auth" not in entry
    assert "toolFilter" not in entry


def test_explicit_native_read_enable_retains_exact_connector_and_finite_catalog():
    from clawake.config import InstanceSpec, load_inventory
    from clawake.services.runtime_config import desired_fields

    inventory = load_inventory(Path(__file__).resolve().parents[1] / "examples/staff/team.yml")
    member = inventory.instances[0].model_copy(deep=True)
    member.onshape_mcp = True
    member.onshape_mcp_enabled = True
    entry = desired_fields(inventory, member)["mcp/servers/onshape"]
    assert entry["enabled"] is True
    assert entry["url"] == "http://clawonshape-mcp:8080/mcp"
    assert entry["supportsParallelToolCalls"] is False
    assert set(entry["toolFilter"]["include"]) == {
        "onshape_auth_status",
        "onshape_api_explain",
        "onshape_api_schema",
        "onshape_api_call",
    }
    assert "auth" not in entry and "headers" not in entry
    invalid = member.model_dump()
    invalid["onshape_mcp"] = False
    with pytest.raises(ValidationError, match="requires the managed onshape_mcp connector"):
        InstanceSpec.model_validate(invalid)


def test_native_probe_uses_top_level_catalog_and_rejects_diagnostics(tmp_path, monkeypatch):
    import json
    import subprocess

    from clawake.services.onshape_mcp import probe_native

    value = spec(tmp_path).model_copy(update={"client_container": "engineer"})
    payload = {"servers": {"onshape": {"tools": 1}}, "tools": ["onshape__onshape_api_call"]}
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    result = probe_native(value)
    assert result["tools"] == payload["tools"] and result["toolCalls"] == 0
    assert all("onshape-local-readonly-20261012" in command for command in commands)
    assert "probe" in commands[-1] and "call" not in commands[-1]
    payload["diagnostics"] = ["provider-secret"]
    with pytest.raises(ValueError, match="contract invalid") as error:
        probe_native(value)
    assert "provider-secret" not in str(error.value)
