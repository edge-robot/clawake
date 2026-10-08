"""Offline generic service rendering and lifecycle regression checks."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from typer.testing import CliRunner

from clawake.cli import app
from clawake.config import Inventory, load_inventory
from clawake.services.deployment import apply_artifacts, plan_deployment
from clawake.services.render import render_service_assets
from clawake.services.resources import lifecycle, secret_preflight, select_resources, status_rows


@pytest.fixture
def inventory(tmp_path):
    data = load_inventory(Path("tests/fixtures/services-v2.yml")).model_dump()
    data["hosts"][0]["quadlet_root"] = str(tmp_path / "quadlets")
    api = data["services"][0]
    api.update(mounts=[], env_files=[], secrets=[], health=None)
    db = deepcopy(api)
    db.update(
        name="db",
        command=["serve"],
        volumes=[{"name": "data", "target": "/data"}],
        interfaces={},
        ports=[],
    )
    api["depends_on"] = ["db"]
    data["services"] = [api, db]
    return Inventory.model_validate(data)


def test_service_rendering_preserves_arguments_and_private_secrets(inventory):
    service = inventory.services[0]
    service.command = ["serve", "space argument", 'literal"quote', "$literal", "%literal"]
    text = render_service_assets(service)["api.container"]
    assert 'Exec="serve" "space argument" "literal\\"quote" "$literal" "%%literal"' in text
    assert "Requires=db.service" in text and "Network=team.network" in text
    assert "UserNS=keep-id" in text
    fixture = load_inventory(Path("tests/fixtures/services-v2.yml")).services[0]
    rendered = render_service_assets(fixture)["api.container"]
    assert "Secret=api-key,type=mount,target=provider-key,mode=0400" in rendered
    assert "HealthCmd=curl --fail" in rendered and "--insecure" not in rendered


def test_plan_is_deterministic_shared_network_once_and_offline(inventory, tmp_path):
    before = inventory.model_dump()
    changes = plan_deployment(inventory, select_resources(inventory, None, None), Path("templates"))
    assert len([c for c in changes if c.role == "shared_network"]) == 1
    assert inventory.model_dump() == before
    assert not list(tmp_path.iterdir())
    apply_artifacts(changes)
    assert not plan_deployment(
        inventory, select_resources(inventory, None, None), Path("templates")
    )


def test_plan_detects_drift_before_any_write(inventory):
    changes = plan_deployment(inventory, inventory.services, Path("templates"))
    changes[-1].destination.parent.mkdir(parents=True)
    changes[-1].destination.write_text("owner edit")
    with pytest.raises(ValueError, match="Configuration changed"):
        apply_artifacts(changes)
    assert not changes[0].destination.exists()


def test_selectors_and_dependency_order(inventory):
    assert [r.name for r in select_resources(inventory, None, "api", dependencies=True)] == [
        "db",
        "api",
    ]
    assert [r.name for r in select_resources(inventory, None, "api")] == ["api"]
    with pytest.raises(ValueError, match="either"):
        select_resources(inventory, "x", "api")
    with pytest.raises(ValueError, match="Unknown"):
        select_resources(inventory, None, "missing")


def test_preview_has_no_runtime_or_filesystem_mutations(inventory, tmp_path, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("runtime in preview"))
    monkeypatch.setattr("clawake.services.resources.SystemdService", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    plan = lifecycle(inventory, "setup", None, "api", Path("templates"))
    assert plan[-2:] == ["setup db", "setup api"]
    assert not list(tmp_path.iterdir())


def test_setup_reloads_once_and_blocks_dependents_on_failure(inventory, monkeypatch):
    runtime = Mock()
    runtime.daemon_reload.return_value.return_code = 0
    runtime.restart.return_value.return_code = 0
    monkeypatch.setattr("clawake.services.resources.SystemdService", lambda: runtime)
    monkeypatch.setattr("clawake.services.resources.wait_ready", lambda r: r.name != "db")
    with pytest.raises(ValueError, match="dependents were not started"):
        lifecycle(inventory, "setup", None, "api", Path("templates"), execute=True)
    runtime.daemon_reload.assert_called_once()
    runtime.restart.assert_called_once_with("db", execute=True)


def test_teardown_reverse_order_preserves_data_and_shared_network(inventory, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.write_text("persistent evidence")
    runtime = Mock()
    for method in ("stop", "remove_container", "remove_quadlet", "daemon_reload"):
        getattr(runtime, method).return_value.return_code = 0
    monkeypatch.setattr("clawake.services.resources.SystemdService", lambda: runtime)
    lifecycle(inventory, "teardown", None, "api", Path("templates"), execute=True)
    assert [call.args[0] for call in runtime.stop.call_args_list] == ["api"]
    assert data.read_text() == "persistent evidence"
    with pytest.raises(ValueError, match="Unselected resources"):
        lifecycle(inventory, "teardown", None, "db", Path("templates"))
    runtime.reset_mock()
    lifecycle(inventory, "teardown", None, None, Path("templates"), execute=True)
    assert [call.args[0] for call in runtime.stop.call_args_list] == ["api", "db", "team-network"]


def test_secret_preflight_checks_names_only(monkeypatch):
    inventory = load_inventory(Path("tests/fixtures/services-v2.yml"))
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="api-key\n"))
    monkeypatch.setattr("subprocess.run", run)
    secret_preflight(inventory, inventory.services)
    assert run.call_args.args[0] == ["podman", "secret", "ls", "--format", "{{.Name}}"]
    run.return_value.stdout = "other\n"
    with pytest.raises(ValueError, match="Missing Podman secret"):
        secret_preflight(inventory, inventory.services)


def test_status_does_not_expose_environment_or_health_output(inventory, monkeypatch):
    runtime = Mock()
    runtime.status.return_value.return_code = 0
    runtime.status.return_value.stdout = "DO-NOT-PRINT"
    monkeypatch.setattr("clawake.services.resources.SystemdService", lambda: runtime)
    monkeypatch.setattr("clawake.services.resources.readiness", lambda r: "running")
    rows = status_rows(inventory, None, "api")
    assert len(rows) == 1 and rows[0]["active"] is True
    assert "DO-NOT-PRINT" not in str(rows)


def test_cli_service_preview_is_supported(inventory, tmp_path, monkeypatch):
    config = tmp_path / "team.yml"
    config.write_text(yaml.safe_dump(inventory.model_dump()))
    monkeypatch.setattr("subprocess.run", Mock(side_effect=AssertionError("runtime in preview")))
    result = CliRunner().invoke(app, ["setup", "-c", str(config), "-s", "api"])
    assert result.exit_code == 0, result.output
    assert (
        "setup db" in result.output and "setup api" in result.output and "DRY RUN" in result.output
    )
