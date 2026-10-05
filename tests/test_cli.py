import json
import re
from pathlib import Path
from types import SimpleNamespace

import yaml
from typer.testing import CliRunner

from clawake.cli import app
from clawake.services.systemd import CommandResult

runner = CliRunner()


def _plain_output(text: str) -> str:
    """Keep error assertions independent of CI-forced ANSI styling."""
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def _write_inventory(
    tmp_path: Path,
    *,
    quadlet_root: Path | None = None,
    env_content: str = "OPENCLAW_GATEWAY_TOKEN=sample-token\n",
    legacy_names: list[str] | None = None,
) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    team_definition = tmp_path / "team.yml"
    env_file = tmp_path / "instance.env"
    inventory_file = tmp_path / "inventory.yml"

    workspace.mkdir(parents=True, exist_ok=True)
    team_definition.write_text("team: sample\n", encoding="utf-8")
    env_file.write_text(env_content, encoding="utf-8")

    data = {
        "version": 1,
        "cluster": {
            "name": "c",
            "mode": "single_host",
            "primary_host": "h",
        },
        "hosts": [
            {
                "name": "h",
                "quadlet_root": str(quadlet_root or (tmp_path / "quadlet-target")),
            }
        ],
        "instances": [
            {
                "name": "one",
                "host": "h",
                "role": "developer",
                "workspace_path": str(workspace),
                "team_definition_path": str(team_definition),
                "quadlet_path": "one.container",
                "container_name": "one",
                "image": {
                    "repository": "ghcr.io/openclaw/openclaw",
                    "tag": "2026.6.5",
                },
                "ports": [
                    {
                        "bind_address": "127.0.0.1",
                        "host_port": 18789,
                        "container_port": 18789,
                        "protocol": "tcp",
                    },
                    {
                        "bind_address": "127.0.0.1",
                        "host_port": 18790,
                        "container_port": 18790,
                        "protocol": "tcp",
                    },
                ],
                "env_files": [str(env_file)],
                "gateway_runtime": {
                    "enabled": True,
                    "bind": "loopback",
                    "gateway_container_port": 18789,
                    "bridge_container_port": 18790,
                },
                "dashboard": {"friendly_name": "One"},
            }
        ],
    }
    if legacy_names:
        data["instances"][0]["legacy_names"] = legacy_names
    inventory_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return inventory_file, env_file, workspace


def test_diagnose_dashboard_json_reports_expected_fields(tmp_path: Path) -> None:
    cfg, _env_file, _workspace = _write_inventory(tmp_path)

    result = runner.invoke(app, ["diagnose-dashboard", "--config", str(cfg), "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["cluster"] == "c"
    instance = payload["instances"][0]
    assert instance["instance"] == "one"
    assert instance["dashboard_url"] == "http://127.0.0.1:18789/"
    assert instance["token_present"] is True


def test_diagnose_dashboard_show_token_url_includes_auth_fragment(tmp_path: Path) -> None:
    cfg, _env_file, _workspace = _write_inventory(tmp_path)

    result = runner.invoke(
        app,
        [
            "diagnose-dashboard",
            "--config",
            str(cfg),
            "--show-token-url",
        ],
    )

    assert result.exit_code == 0
    assert "auth_url:" in result.output
    assert "#token=sample-token" in result.output


def test_onboard_member_dry_run_uses_managed_workspace(tmp_path: Path) -> None:
    cfg, _env_file, _workspace = _write_inventory(tmp_path)

    result = runner.invoke(
        app,
        ["onboard-member", "--config", str(cfg), "--member", "one"],
    )

    assert result.exit_code == 0
    assert "podman exec --interactive --tty one openclaw onboard" in result.output
    assert "--workspace /workspace" in result.output
    assert "--skip-bootstrap" in result.output
    assert "--no-install-daemon" in result.output
    assert "DRY RUN onboard-member complete" in result.output


def test_upgrade_dry_run_requires_no_runtime_calls(tmp_path: Path) -> None:
    cfg, _env_file, _workspace = _write_inventory(tmp_path)

    result = runner.invoke(
        app,
        [
            "upgrade",
            "--config",
            str(cfg),
            "--member",
            "one",
            "--to",
            "2026.8.2",
            "--digest",
            "sha256:new",
        ],
    )

    assert result.exit_code == 0
    assert "2026.6.5 -> 2026.8.2" in result.output
    assert "DRY RUN upgrade complete" in result.output


def test_upgrade_rejects_unpinned_new_tag(tmp_path: Path) -> None:
    cfg, _env_file, _workspace = _write_inventory(tmp_path)

    result = runner.invoke(
        app,
        ["upgrade", "--config", str(cfg), "--member", "one", "--to", "2026.8.2"],
    )

    assert result.exit_code == 2
    assert "immutable --digest is required" in _plain_output(result.output)


def test_upgrade_execute_runs_backup_migration_and_health_checks(
    monkeypatch: object, tmp_path: Path
) -> None:
    from clawake import cli

    class RecordingSystemdService:
        calls: list[str] = []

        def stop(self, instance_name: str, execute: bool = False) -> CommandResult:
            self.calls.append("stop")
            return CommandResult(["systemctl", "stop"], 0, "", "")

        def daemon_reload(self, execute: bool = False) -> CommandResult:
            self.calls.append("reload")
            return CommandResult(["systemctl", "daemon-reload"], 0, "", "")

        def restart(self, instance_name: str, execute: bool = False) -> CommandResult:
            self.calls.append("restart")
            return CommandResult(["systemctl", "restart"], 0, "", "")

        def unit_name(self, instance_name: str) -> str:
            return f"{instance_name}.service"

    cfg, _env_file, _workspace = _write_inventory(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "SystemdService", RecordingSystemdService)
    monkeypatch.setattr(cli, "check_image_availability", lambda image: None)
    monkeypatch.setattr(
        cli,
        "run_doctor",
        lambda instance, image: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(cli, "wait_for_health", lambda instance: (True, "health-url"))
    monkeypatch.setattr(
        cli,
        "verify_runtime",
        lambda instance, image: SimpleNamespace(
            healthy=True,
            version="OpenClaw 2026.8.2",
            image_name="ghcr.io/openclaw/openclaw:2026.8.2@sha256:new",
            error="",
        ),
    )

    result = runner.invoke(
        app,
        [
            "upgrade",
            "--config",
            str(cfg),
            "--member",
            "one",
            "--to",
            "2026.8.2",
            "--digest",
            "sha256:new",
            "--execute",
        ],
    )

    assert result.exit_code == 0, result.output
    assert RecordingSystemdService.calls == ["stop", "reload", "restart"]
    assert "Upgrade complete: OpenClaw 2026.8.2" in result.output
    assert list((tmp_path / ".backups").glob("one-*.tar.gz"))
    image = yaml.safe_load(cfg.read_text(encoding="utf-8"))["instances"][0]["image"]
    assert image["tag"] == "2026.8.2"
    assert image["digest"] == "sha256:new"
    assert image["known_good_tag"] == "2026.6.5"


def test_setup_quadlets_dry_run_succeeds(tmp_path: Path) -> None:
    cfg, _env_file, _workspace = _write_inventory(tmp_path)

    result = runner.invoke(app, ["setup-quadlets", "--config", str(cfg)])

    assert result.exit_code == 0
    assert "DRY RUN setup-quadlets complete" in result.output


def test_setup_quadlets_execute_deploys_files(monkeypatch: object, tmp_path: Path) -> None:
    from clawake import cli

    class RecordingSystemdService:
        daemon_reload_calls = 0
        restart_calls = 0

        def daemon_reload(self, execute: bool = False) -> CommandResult:
            RecordingSystemdService.daemon_reload_calls += 1
            return CommandResult(["systemctl", "--user", "daemon-reload"], 0, "ok", "")

        def restart(self, instance_name: str, execute: bool = False) -> CommandResult:
            RecordingSystemdService.restart_calls += 1
            return CommandResult(
                ["systemctl", "--user", "restart", f"{instance_name}.service"],
                0,
                "ok",
                "",
            )

    quadlet_root = tmp_path / "quadlets"
    cfg, _env_file, _workspace = _write_inventory(tmp_path, quadlet_root=quadlet_root)
    monkeypatch.setattr(cli, "SystemdService", RecordingSystemdService)

    result = runner.invoke(app, ["setup-quadlets", "--config", str(cfg), "--execute"])

    assert result.exit_code == 0
    assert (quadlet_root / "one.container").is_file()
    assert (quadlet_root / "one.network").is_file()
    assert (quadlet_root / "one-state.volume").is_file()
    assert (_workspace / ".openclaw").is_dir()
    openclaw_config = json.loads(
        (_workspace / ".openclaw" / "openclaw.json").read_text(encoding="utf-8")
    )
    assert openclaw_config["agents"]["defaults"]["workspace"] == "/workspace"
    assert openclaw_config["gateway"]["controlUi"] == {
        "allowedOrigins": [
            "http://127.0.0.1:18789",
            "http://localhost:18789",
        ],
        "allowInsecureAuth": True,
    }
    assert RecordingSystemdService.daemon_reload_calls == 1
    assert RecordingSystemdService.restart_calls == 1


def test_setup_quadlets_execute_starts_unchanged_service(
    monkeypatch: object, tmp_path: Path
) -> None:
    from clawake import cli

    class RecordingSystemdService:
        restart_calls = 0

        def daemon_reload(self, execute: bool = False) -> CommandResult:
            return CommandResult(["systemctl", "--user", "daemon-reload"], 0, "ok", "")

        def restart(self, instance_name: str, execute: bool = False) -> CommandResult:
            RecordingSystemdService.restart_calls += 1
            return CommandResult(
                ["systemctl", "--user", "restart", f"{instance_name}.service"],
                0,
                "ok",
                "",
            )

    quadlet_root = tmp_path / "quadlets"
    cfg, _env_file, _workspace = _write_inventory(tmp_path, quadlet_root=quadlet_root)
    monkeypatch.setattr(cli, "SystemdService", RecordingSystemdService)

    first = runner.invoke(app, ["setup-quadlets", "--config", str(cfg), "--execute"])
    second = runner.invoke(app, ["setup-quadlets", "--config", str(cfg), "--execute"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "ensuring selected services are running" in second.output
    assert RecordingSystemdService.restart_calls == 2


def test_setup_quadlets_retires_legacy_service_before_restart(
    monkeypatch: object, tmp_path: Path
) -> None:
    from clawake import cli

    class RecordingSystemdService:
        calls: list[tuple[str, str]] = []

        def stop(self, instance_name: str, execute: bool = False) -> CommandResult:
            self.calls.append(("stop", instance_name))
            return CommandResult(["systemctl", "stop"], 0, "", "")

        def disable(self, instance_name: str, execute: bool = False) -> CommandResult:
            self.calls.append(("disable", instance_name))
            return CommandResult(["systemctl", "disable"], 0, "", "")

        def remove_quadlet(self, quadlet_path: str, execute: bool = False) -> CommandResult:
            self.calls.append(("remove", Path(quadlet_path).name))
            return CommandResult(["rm", quadlet_path], 0, "", "")

        def daemon_reload(self, execute: bool = False) -> CommandResult:
            self.calls.append(("reload", ""))
            return CommandResult(["systemctl", "daemon-reload"], 0, "", "")

        def restart(self, instance_name: str, execute: bool = False) -> CommandResult:
            self.calls.append(("restart", instance_name))
            return CommandResult(["systemctl", "restart"], 0, "", "")

    cfg, _env_file, _workspace = _write_inventory(
        tmp_path,
        quadlet_root=tmp_path / "quadlets",
        legacy_names=["old-one"],
    )
    monkeypatch.setattr(cli, "SystemdService", RecordingSystemdService)

    result = runner.invoke(app, ["setup", "--config", str(cfg), "--execute"])

    assert result.exit_code == 0, result.output
    assert RecordingSystemdService.calls == [
        ("stop", "old-one"),
        ("disable", "old-one"),
        ("remove", "old-one.container"),
        ("remove", "old-one.network"),
        ("remove", "old-one-state.volume"),
        ("reload", ""),
        ("restart", "one"),
    ]


def test_setup_quadlets_preserves_legacy_artifacts_when_stop_fails(
    monkeypatch: object, tmp_path: Path
) -> None:
    from clawake import cli

    class FailedLegacyStop:
        calls: list[tuple[str, str]] = []

        def stop(self, instance_name: str, execute: bool = False) -> CommandResult:
            self.calls.append(("stop", instance_name))
            return CommandResult(["systemctl", "stop"], 1, "", "permission denied")

        def disable(self, instance_name: str, execute: bool = False) -> CommandResult:
            raise AssertionError("Must not disable after a failed stop")

        def remove_quadlet(self, quadlet_path: str, execute: bool = False) -> CommandResult:
            raise AssertionError("Must not remove artifacts after a failed stop")

        def daemon_reload(self, execute: bool = False) -> CommandResult:
            raise AssertionError("Must not reload after a failed legacy migration")

        def restart(self, instance_name: str, execute: bool = False) -> CommandResult:
            raise AssertionError("Must not restart after a failed legacy migration")

    cfg, _env_file, _workspace = _write_inventory(
        tmp_path,
        quadlet_root=tmp_path / "quadlets",
        legacy_names=["old-one"],
    )
    monkeypatch.setattr(cli, "SystemdService", FailedLegacyStop)

    result = runner.invoke(app, ["setup", "--config", str(cfg), "--execute"])

    assert result.exit_code == 1
    assert FailedLegacyStop.calls == [("stop", "old-one")]


def test_restart_quadlets_execute_propagates_failures(monkeypatch: object, tmp_path: Path) -> None:
    from clawake import cli

    class FailingSystemdService:
        def restart(self, instance_name: str, execute: bool = False) -> CommandResult:
            return CommandResult(
                ["systemctl", "--user", "restart", f"{instance_name}.service"],
                1,
                "",
                "failed",
            )

    cfg, _env_file, _workspace = _write_inventory(tmp_path)
    monkeypatch.setattr(cli, "SystemdService", FailingSystemdService)

    result = runner.invoke(app, ["restart-quadlets", "--config", str(cfg), "--execute"])

    assert result.exit_code == 1


def test_status_quadlets_json_healthy(monkeypatch: object, tmp_path: Path) -> None:
    from clawake import cli

    class HealthySystemdService:
        def status(self, instance_name: str, execute: bool = False) -> CommandResult:
            return CommandResult(
                ["systemctl", "--user", "status", f"{instance_name}.service"],
                0,
                "active",
                "",
            )

        def logs(
            self, instance_name: str, lines: int = 100, execute: bool = False
        ) -> CommandResult:
            return CommandResult(["journalctl"], 0, "", "")

        def unit_name(self, instance_name: str) -> str:
            return f"{instance_name}.service"

    cfg, _env_file, _workspace = _write_inventory(tmp_path)
    monkeypatch.setattr(cli, "SystemdService", HealthySystemdService)

    result = runner.invoke(app, ["status-quadlets", "--config", str(cfg), "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["instances"][0]["state"] == "healthy"
    assert payload["instances"][0]["unit"] == "one.service"


def test_status_quadlets_text_prints_diagnostic_cause(monkeypatch: object, tmp_path: Path) -> None:
    from clawake import cli

    class FailedSystemdService:
        def status(self, instance_name: str, execute: bool = False) -> CommandResult:
            return CommandResult(
                ["systemctl", "--user", "status", f"{instance_name}.service"],
                3,
                "Active: failed (Result: exit-code)",
                "",
            )

        def logs(
            self, instance_name: str, lines: int = 100, execute: bool = False
        ) -> CommandResult:
            return CommandResult(["journalctl"], 0, "EnvironmentFile missing", "")

        def unit_name(self, instance_name: str) -> str:
            return f"{instance_name}.service"

    cfg, _env_file, _workspace = _write_inventory(tmp_path)
    monkeypatch.setattr(cli, "SystemdService", FailedSystemdService)

    result = runner.invoke(app, ["status-quadlets", "--config", str(cfg), "--format", "text"])

    assert result.exit_code == 1
    assert "state=failed rc=3" in result.output
    assert "cause: EnvironmentFile missing" in result.output


def test_teardown_quadlets_dry_run_succeeds(tmp_path: Path) -> None:
    cfg, _env_file, _workspace = _write_inventory(tmp_path)

    result = runner.invoke(app, ["teardown-quadlets", "--config", str(cfg)])

    assert result.exit_code == 0
    assert "DRY RUN teardown-quadlets complete" in result.output


def test_setup_preview_and_validate_do_not_write(monkeypatch: object, tmp_path: Path) -> None:
    cfg, _, _ = _write_inventory(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    for command in ("setup", "validate"):
        result = runner.invoke(app, [command, "-c", str(cfg)])
        assert result.exit_code == 0, result.output
    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
    assert not (tmp_path / ".rendered").exists()
    assert not (tmp_path / "quadlet-target").exists()


def test_invalid_yaml_reports_config_error(tmp_path: Path) -> None:
    config = tmp_path / "broken.yml"
    config.write_text("instances: [", encoding="utf-8")
    result = runner.invoke(app, ["validate", "-c", str(config)])
    assert result.exit_code == 2
    assert "Cannot load" in result.output
    assert "--config" in _plain_output(result.output)


def test_unknown_member_lists_available_members(tmp_path: Path) -> None:
    cfg, _, _ = _write_inventory(tmp_path)
    result = runner.invoke(app, ["setup", "-c", str(cfg), "-m", "missing"])
    assert result.exit_code == 2
    assert "Available: one" in result.output


def test_reload_failure_prevents_restart(monkeypatch: object, tmp_path: Path) -> None:
    from clawake import cli

    class FailedReload:
        def daemon_reload(self, execute: bool = False) -> CommandResult:
            return CommandResult(["systemctl"], 1, "", "reload failed")

        def restart(self, *args: object, **kwargs: object) -> CommandResult:
            raise AssertionError("Must not restart after reload failure")

    cfg, _, _ = _write_inventory(tmp_path)
    monkeypatch.setattr(cli, "SystemdService", FailedReload)
    result = runner.invoke(app, ["setup", "-c", str(cfg), "--execute"])
    assert result.exit_code == 1
    assert "reload failed" in result.output


def test_logs_scopes_member_and_line_count(monkeypatch: object, tmp_path: Path) -> None:
    from clawake import cli

    class Journal:
        def logs(self, name: str, lines: int, execute: bool) -> CommandResult:
            assert (name, lines, execute) == ("one", 25, True)
            return CommandResult(["journalctl"], 0, "gateway ready", "")

    cfg, _, _ = _write_inventory(tmp_path)
    monkeypatch.setattr(cli, "SystemdService", Journal)
    result = runner.invoke(app, ["logs", "-c", str(cfg), "-m", "one", "-n", "25"])
    assert result.exit_code == 0
    assert result.output.strip() == "gateway ready"
