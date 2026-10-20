import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from typer.testing import CliRunner

from clawake.cli import app
from clawake.config import PluginSpec, load_inventory
from clawake.services.deployment import apply_artifacts, plan_deployment
from clawake.services.gateway_config import ensure_plugin_runtime_config
from clawake.services.readiness import HttpOutcome, ReadinessProbes
from clawake.services.systemd import CommandResult


def _ready_probes(monkeypatch) -> None:
    """Members answer /readyz 200 with no plugins installed; npm inspect finds nothing."""

    def run(argv: list[str], timeout: float) -> CommandResult:
        if argv[-3:-1] == ["plugins", "list"]:
            return CommandResult(argv, 0, '{"plugins": []}', "")
        return CommandResult(argv, 1, "", "not installed")

    monkeypatch.setattr(
        "clawake.cli.default_probes",
        lambda service: ReadinessProbes(
            unit_status=lambda name: CommandResult([], 0, "Active: active (running)", ""),
            run=run,
            http_get=lambda url, timeout: HttpOutcome(status=200),
        ),
    )


@pytest.fixture
def configured(tmp_path):
    inventory = load_inventory(Path("tests/fixtures/services-v2.yml"))
    instance = load_inventory(Path("examples/staff/team.yml")).instances[0]
    instance.name = "engineer-runtime"
    instance.host = "local"
    instance.container_name = "engineer-runtime"
    instance.quadlet_path = "engineer-runtime.container"
    instance.networks = ["team"]
    instance.legacy_names = []
    instance.openclaw = None
    inventory.instances = [instance]
    instance.workspace_path = str(tmp_path / "workspace")
    inventory.hosts[0].quadlet_root = str(tmp_path / "quadlets")
    instance.agent_tool_allow = {"engineer": ["cad_tool"]}
    config_path, _ = ensure_plugin_runtime_config(instance)
    payload = json.loads(config_path.read_text())
    payload["agents"]["entries"]["engineer"]["tools"]["alsoAllow"].append("operator_tool")
    config_path.write_text(json.dumps(payload))
    instance.agent_tool_allow = {}
    config = tmp_path / "team.yml"
    config.write_text(yaml.safe_dump(inventory.model_dump()))
    return config, config_path, inventory


def test_sync_without_plugins_revokes_only_managed_grants_and_preview_is_inert(
    configured, monkeypatch
):
    config, config_path, _ = configured
    before = config_path.read_bytes()
    runtime = Mock()
    runtime.restart.return_value = SimpleNamespace(return_code=0, stdout="", stderr="", command=[])
    monkeypatch.setattr("clawake.cli.SystemdService", lambda: runtime)
    _ready_probes(monkeypatch)
    preview = CliRunner().invoke(app, ["sync-plugins", "-c", str(config)])
    assert preview.exit_code == 0, preview.output
    assert config_path.read_bytes() == before
    runtime.restart.assert_not_called()
    applied = CliRunner().invoke(app, ["sync-plugins", "-c", str(config), "--execute"])
    assert applied.exit_code == 0, applied.output
    assert json.loads(config_path.read_text())["agents"]["entries"]["engineer"]["tools"][
        "alsoAllow"
    ] == ["operator_tool"]
    assert runtime.restart.call_count == len(load_inventory(config).instances)
    runtime.reset_mock()
    repeated = CliRunner().invoke(app, ["sync-plugins", "-c", str(config), "--execute"])
    assert repeated.exit_code == 0, repeated.output
    runtime.restart.assert_not_called()


def test_failed_plugin_install_does_not_restart_or_reconcile_member(configured, monkeypatch):
    config, config_path, inventory = configured
    instance = inventory.instances[0]
    instance.plugins = [
        PluginSpec(id=name, npm_spec=f"@example/{name}@1.0.0", integrity="sha512-dGVzdA==")
        for name in ("first", "second")
    ]
    config.write_text(yaml.safe_dump(inventory.model_dump()))
    before = config_path.read_bytes()

    def sync(instance, plugin, *, execute):
        if execute and plugin.id == "second":
            raise RuntimeError("installation_failed")
        return SimpleNamespace(digest="sha512-dGVzdA==", commands=[])

    monkeypatch.setattr("clawake.services.member_ops.sync_plugin", sync)
    runtime = Mock()
    runtime.restart.return_value = SimpleNamespace(return_code=0, stdout="", stderr="", command=[])
    monkeypatch.setattr("clawake.cli.SystemdService", lambda: runtime)
    _ready_probes(monkeypatch)
    applied = CliRunner().invoke(
        app, ["sync-plugins", "-c", str(config), "-m", instance.name, "--execute"]
    )
    assert applied.exit_code == 1, applied.output
    runtime.restart.assert_not_called()
    assert config_path.read_bytes() == before


def test_v2_setup_plan_preserves_openclaw_tool_grants(configured):
    _, config_path, inventory = configured
    before = json.loads(config_path.read_text())["agents"]["entries"]["engineer"]
    changes = plan_deployment(inventory, inventory.instances, Path("templates"))
    apply_artifacts(changes)
    assert json.loads(config_path.read_text())["agents"]["entries"]["engineer"] == before
    assert plan_deployment(inventory, inventory.instances, Path("templates")) == []
