import json
import tarfile
from pathlib import Path

import pytest

from clawake.config import load_inventory
from clawake.services.backup import backup_instance, backup_sources
from clawake.services.gateway_config import ensure_plugin_runtime_config, ensure_workspace_config
from clawake.services.render import render_instance_assets
from clawake.services.runtime_config import plan_runtime_files
from clawake.services.runtime_upgrade import browser_cache_path, doctor_command


def test_separate_state_is_used_across_lifecycle(tmp_path: Path) -> None:
    inventory = load_inventory(Path("examples/staff/team.yml"))
    instance = inventory.instances[0]
    workspace = tmp_path / "workspace"
    state = tmp_path / "private-state"
    role = tmp_path / "definition"
    workspace.mkdir()
    state.mkdir()
    role.mkdir()
    instance.workspace_path = str(workspace)
    instance.state_path = str(state)
    instance.team_definition_path = str(role)
    instance.backup_policy.paths = []
    (state / "openclaw.json").write_text(json.dumps({"existing": "preserved"}))
    (state / "session.json").write_text("private history")

    rendered = render_instance_assets(instance, Path("templates"))
    assert f"Volume={state}:/home/node/.openclaw" in rendered[instance.quadlet_path]
    assert f"Options=device={state}" in rendered[instance.runtime_volume_quadlet_path]
    config, changed = ensure_workspace_config(instance)
    assert changed and config == state / "openclaw.json"
    assert json.loads(config.read_text())["existing"] == "preserved"
    assert all(path.parent == state for path, *_ in plan_runtime_files(inventory, instance))
    assert f"{state}:/home/node/.openclaw" in doctor_command(instance, instance.image)
    assert browser_cache_path(instance) == state / "cache" / "openclaw-1000"
    assert state in backup_sources(instance)
    archive = backup_instance(instance, tmp_path / "backups", execute=True)
    with tarfile.open(archive) as reader:
        assert any(member.name.endswith("/session.json") for member in reader.getmembers())
    plugin_config, _ = ensure_plugin_runtime_config(instance)
    assert plugin_config == state / "openclaw.json"
    assert not (workspace / ".openclaw").exists()


def test_default_state_remains_nested_without_duplicate_backup() -> None:
    instance = load_inventory(Path("examples/staff/team.yml")).instances[0]
    assert instance.runtime_state_path == Path(instance.workspace_path) / ".openclaw"
    assert instance.runtime_state_path not in backup_sources(instance)


@pytest.mark.parametrize("value", ["relative/state", "", "/tmp/state\nVolume=unsafe"])
def test_inventory_rejects_unsafe_state_path(tmp_path: Path, value: str) -> None:
    payload = load_inventory(Path("examples/staff/team.yml")).model_dump(mode="json")
    payload["instances"][0]["state_path"] = value
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_inventory(path)


def test_inventory_rejects_shared_runtime_state(tmp_path: Path) -> None:
    payload = load_inventory(Path("examples/staff/team.yml")).model_dump(mode="json")
    for instance in payload["instances"]:
        instance["state_path"] = "/tmp/shared-private-state"
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Path collision"):
        load_inventory(path)
