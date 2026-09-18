from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml

from clawake.config import ImageSpec, InstanceSpec, Inventory, load_inventory
from clawake.services.backup import backup_instance, prune_backups
from clawake.services.deployment import apply_artifacts, plan_deployment
from clawake.services.gateway_config import (
    ensure_control_ui_config,
    ensure_plugin_runtime_config,
    ensure_workspace_config,
)
from clawake.services.image_check import ImageCheckError, check_image_availability
from clawake.services.plugins import sync_plugin
from clawake.services.runtime_upgrade import (
    doctor_command,
    ensure_browser_cache,
    is_browser_image,
    run_doctor,
    verify_runtime,
    wait_for_health,
)
from clawake.services.systemd import CommandResult, SystemdService
from clawake.services.upgrade import apply_upgrade, backup_config, build_upgrade_plan

app = typer.Typer(
    help="Deploy and operate OpenClaw teams. Changes are previewed unless --execute is set.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

ConfigPath = Annotated[Path, typer.Option(..., "--config", "-c", exists=True, dir_okay=False)]
OptionalMemberName = Annotated[
    str | None, typer.Option("--member", "-m", help="Select one member; default: entire team.")
]
ExecuteFlag = Annotated[
    bool, typer.Option("--execute", help="Apply the previewed operation to the local host.")
]
StatusFormat = Annotated[Literal["text", "json"], typer.Option("--format")]
ShowTokenUrlFlag = Annotated[bool, typer.Option("--show-token-url")]
TargetTag = Annotated[str, typer.Option(..., "--to", help="Target OpenClaw image tag")]
TargetDigest = Annotated[str | None, typer.Option("--digest", help="Immutable target digest")]

_ENV_LINE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _load(path: Path) -> Inventory:
    try:
        return load_inventory(path)
    except (ValueError, yaml.YAMLError, OSError) as exc:
        raise typer.BadParameter(f"Cannot load {path}: {exc}", param_hint="--config") from exc


def _template_root() -> Path:
    return Path(__file__).resolve().parents[2] / "templates"


def _instance_by_name(inventory: Inventory, name: str) -> InstanceSpec:
    for instance in inventory.instances:
        if instance.name == name:
            return instance
    raise typer.BadParameter(
        f"Unknown member '{name}'. Available: "
        + ", ".join(instance.name for instance in inventory.instances),
        param_hint="--member",
    )


def _select_instances(inventory: Inventory, member: str | None) -> list[InstanceSpec]:
    if member is None:
        return list(inventory.instances)
    return [_instance_by_name(inventory, member)]


@app.command("sync-plugins")
def sync_plugins(
    config: ConfigPath,
    member: OptionalMemberName = None,
    execute: ExecuteFlag = False,
) -> None:
    """Verify and install integrity-pinned OpenClaw plugins from the inventory."""
    inventory = _load(config)
    selected = _select_instances(inventory, member)
    declared = [(instance, plugin) for instance in selected for plugin in instance.plugins]
    if not declared:
        typer.echo("No managed plugins declared for the selected member(s).")
        return

    typer.echo(f"Plugin sync plan for {len(declared)} pinned plugin(s)")
    plans = []
    try:
        for instance, plugin in declared:
            result = sync_plugin(instance, plugin, execute=False)
            plans.append((instance, plugin, result))
            typer.echo(f" - {instance.name}: {plugin.id} ({result.digest})")
            if plugin.source_type == "archive":
                typer.echo(f"   mount: {plugin.artifact_path} -> {plugin.container_path}:ro")
            else:
                typer.echo(f"   npm: {plugin.npm_spec}")
        for instance in selected:
            for agent_id, tools in sorted(instance.agent_tool_allow.items()):
                typer.echo(
                    f" - {instance.name}: agent {agent_id} optional tools ({', '.join(tools)})"
                )
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if not execute:
        typer.echo(
            "DRY RUN sync-plugins complete. Run setup --execute first when an archive "
            "mount is new or changed, then re-run with --execute."
        )
        return

    service = SystemdService()
    failed = False
    restart_instances: dict[str, InstanceSpec] = {}
    failed_instances: set[str] = set()
    for instance, plugin, _result in plans:
        try:
            result = sync_plugin(instance, plugin, execute=True)
            for command in result.commands:
                typer.echo(f"$ {' '.join(command)}")
            typer.echo(f"Installed verified plugin {plugin.id} on {instance.name}")
            restart_instances[instance.name] = instance
        except (OSError, RuntimeError, ValueError) as exc:
            typer.echo(str(exc), err=True)
            failed = True
            failed_instances.add(instance.name)
            continue
    for instance in selected:
        if instance.name in failed_instances:
            continue
        try:
            config_path, config_changed = ensure_plugin_runtime_config(instance)
            if config_changed:
                typer.echo(f"Updated managed plugin config and agent ACLs in {config_path}")
            restart_instances[instance.name] = instance
        except (OSError, RuntimeError, ValueError) as exc:
            typer.echo(str(exc), err=True)
            failed = True
            failed_instances.add(instance.name)
    for instance in restart_instances.values():
        restart_result = service.restart(instance.name, execute=True)
        _print_result(restart_result)
        failed = failed or restart_result.return_code != 0
    if failed:
        raise typer.Exit(code=1)


def _print_result(result: CommandResult) -> None:
    typer.echo(f"$ {' '.join(result.command)}")
    if result.stdout:
        typer.echo(result.stdout)
    if result.stderr:
        typer.echo(result.stderr, err=True)


def _status_state(result: CommandResult) -> str:
    if result.return_code == 0:
        return "healthy"
    return "failed"


def _status_needs_diagnostics(result: CommandResult) -> bool:
    details = "\n".join(part for part in (result.stdout, result.stderr) if part).lower()
    if result.return_code != 0:
        return True
    return any(
        token in details for token in ("inactive (dead)", "failed", "activating (auto-restart)")
    )


def _status_diagnostics(service: SystemdService, instance_name: str) -> CommandResult | None:
    logs_result = service.logs(instance_name, lines=50, execute=True)
    if not logs_result.stdout and not logs_result.stderr:
        return None
    return logs_result


def _diagnostic_summary(result: CommandResult | None) -> str | None:
    if result is None:
        return None
    for line in (result.stdout or result.stderr).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _is_tolerated_teardown_error(result: CommandResult) -> bool:
    details = "\n".join(part for part in (result.stdout, result.stderr) if part).lower()
    tolerated_markers = ("not loaded", "not found", "no such container", "does not exist")
    return any(marker in details for marker in tolerated_markers)


def _retire_legacy_services(
    service: SystemdService,
    instances: list[InstanceSpec],
    *,
    disable: bool,
    quadlet_roots: dict[str, Path] | None = None,
) -> bool:
    """Stop legacy units before a renamed instance claims their runtime resources."""
    failed = False
    for instance in instances:
        for legacy_name in instance.legacy_names:
            typer.echo(f"Retiring legacy service {legacy_name}.service for {instance.name}")
            stop_result = service.stop(legacy_name, execute=True)
            _print_result(stop_result)
            if stop_result.return_code != 0 and not _is_tolerated_teardown_error(stop_result):
                failed = True
                continue

            if disable:
                disable_result = service.disable(legacy_name, execute=True)
                _print_result(disable_result)
                if disable_result.return_code != 0 and not _is_tolerated_teardown_error(
                    disable_result
                ):
                    failed = True
                    continue

            if quadlet_roots is not None:
                root = quadlet_roots[instance.host]
                for artifact in (
                    f"{legacy_name}.container",
                    f"{legacy_name}.network",
                    f"{legacy_name}-state.volume",
                ):
                    remove_result = service.remove_quadlet(str(root / artifact), execute=True)
                    _print_result(remove_result)
                    if remove_result.return_code != 0 and not _is_tolerated_teardown_error(
                        remove_result
                    ):
                        failed = True
    return not failed


def _load_env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not _ENV_LINE_PATTERN.fullmatch(line):
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _instance_env_values(instance: InstanceSpec) -> dict[str, str]:
    merged: dict[str, str] = {}
    for raw_path in instance.env_files:
        merged.update(_load_env_file_values(Path(raw_path).expanduser()))
    return merged


def _dashboard_host_url(instance: InstanceSpec) -> str:
    gateway_port = instance.gateway_runtime.gateway_container_port
    published_gateway_port = next(
        (
            port
            for port in instance.ports
            if port.container_port == gateway_port and port.protocol == "tcp"
        ),
        None,
    )
    host_port = published_gateway_port.host_port if published_gateway_port else gateway_port
    return f"http://127.0.0.1:{host_port}/"


def _deploy_instance_assets(
    instance: InstanceSpec,
    inventory: Inventory,
) -> list[Path]:
    return apply_artifacts(plan_deployment(inventory, [instance], _template_root()))


@app.command("upgrade")
def upgrade_member(
    config: ConfigPath,
    member: Annotated[str, typer.Option(..., "--member", "-m")],
    target_tag: TargetTag,
    digest: TargetDigest = None,
    execute: ExecuteFlag = False,
) -> None:
    """Safely upgrade one member, including backup, migration, and health checks."""
    inventory = _load(config)
    instance = _instance_by_name(inventory, member)
    target_digest = digest
    if target_digest is None and target_tag == instance.image.tag:
        target_digest = instance.image.digest
    if not target_digest:
        raise typer.BadParameter(
            "An immutable --digest is required when changing tags; "
            "Clawake will not follow an unpinned image tag."
        )

    plan = build_upgrade_plan(config, member, target_tag, target_digest)
    target = ImageSpec(
        repository=instance.image.repository,
        tag=plan.next_tag,
        digest=plan.next_digest,
    )
    typer.echo(f"Upgrade plan for member '{member}':")
    typer.echo(f"  image: {plan.previous_tag} -> {plan.next_tag}")
    typer.echo(f"  digest: {plan.previous_digest or 'unpinned'} -> {plan.next_digest}")
    typer.echo(f"  backup: {'yes' if instance.backup_policy.pre_mutation else 'no'}")
    typer.echo(f"  migration: {' '.join(doctor_command(instance, target)[-5:])}")
    typer.echo(f"  health: {_dashboard_host_url(instance).rstrip('/')}{instance.health.path}")
    if not execute:
        typer.echo("DRY RUN upgrade complete. Re-run with --execute to mutate state.")
        return

    service = SystemdService()
    backup_dir = Path(".backups")
    stopped = False
    config_changed = False
    runtime_backup: Path | None = None
    config_backup: Path | None = None

    try:
        typer.echo("Verifying pinned image in registry...")
        check_image_availability(target)

        stop_result = service.stop(instance.name, execute=True)
        _print_result(stop_result)
        if stop_result.return_code != 0:
            raise RuntimeError(stop_result.stderr or "failed to stop service")
        stopped = True

        if instance.backup_policy.enabled and instance.backup_policy.pre_mutation:
            config_backup = backup_config(config, backup_dir)
            runtime_backup = backup_instance(instance, backup_dir, execute=True)
            typer.echo(f"Created config backup: {config_backup}")
            typer.echo(f"Created runtime backup: {runtime_backup}")

        if is_browser_image(target):
            cache_path = ensure_browser_cache(instance)
            typer.echo(f"Prepared private browser cache: {cache_path}")

        typer.echo("Running OpenClaw safe migrations in a one-shot container...")
        doctor_result = run_doctor(instance, target)
        if doctor_result.returncode != 0:
            details = doctor_result.stderr.strip() or doctor_result.stdout.strip()
            raise RuntimeError(f"OpenClaw migration failed: {details}")

        apply_upgrade(config, instance.name, target.tag, target.digest)
        config_changed = True
        updated_inventory = _load(config)
        updated_instance = _instance_by_name(updated_inventory, member)
        ensure_workspace_config(updated_instance)
        ensure_control_ui_config(updated_instance)
        for deployed in _deploy_instance_assets(updated_instance, updated_inventory):
            typer.echo(f"Deployed {deployed}")

        reload_result = service.daemon_reload(execute=True)
        _print_result(reload_result)
        if reload_result.return_code != 0:
            raise RuntimeError(reload_result.stderr or "systemd daemon-reload failed")

        restart_result = service.restart(instance.name, execute=True)
        _print_result(restart_result)
        if restart_result.return_code != 0:
            raise RuntimeError(restart_result.stderr or "service restart failed")

        healthy, health_details = wait_for_health(updated_instance)
        if not healthy:
            raise RuntimeError(f"health check failed: {health_details}")

        runtime = verify_runtime(updated_instance, target)
        if not runtime.healthy:
            raise RuntimeError(f"runtime verification failed: {runtime.error}")

        typer.echo(f"Upgrade complete: {runtime.version}")
        typer.echo(f"Running image: {runtime.image_name}")
        typer.echo(f"Health check: {health_details}")
        if runtime_backup:
            prune_backups(backup_dir, f"{instance.name}-", instance.backup_policy.retention)
        if config_backup:
            prune_backups(backup_dir, f"{config.stem}-", instance.backup_policy.retention)
    except (ImageCheckError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"Upgrade failed: {exc}", err=True)
        if stopped and not config_changed:
            typer.echo("The config was not changed; attempting to restart the previous image.")
            recovery = service.restart(instance.name, execute=True)
            _print_result(recovery)
        elif config_changed:
            typer.echo("Stopping the failed upgraded service to prevent a restart loop.", err=True)
            stopped_result = service.stop(instance.name, execute=True)
            _print_result(stopped_result)
        if runtime_backup:
            typer.echo(f"Runtime recovery archive: {runtime_backup}", err=True)
        if config_backup:
            typer.echo(f"Config recovery file: {config_backup}", err=True)
        typer.echo(
            f"Inspect logs with: journalctl --user-unit {service.unit_name(instance.name)} -n 100",
            err=True,
        )
        raise typer.Exit(code=1) from exc


@app.command("onboard-member")
def onboard_member(
    config: ConfigPath,
    member: Annotated[str, typer.Option(..., "--member", "-m")],
    execute: ExecuteFlag = False,
) -> None:
    """Run OpenClaw's interactive onboarding inside one managed container."""
    inventory = _load(config)
    instance = _instance_by_name(inventory, member)
    command = [
        "podman",
        "exec",
        "--interactive",
        "--tty",
        instance.container_name,
        "openclaw",
        "onboard",
        "--workspace",
        "/workspace",
        "--skip-bootstrap",
        "--no-install-daemon",
    ]

    typer.echo(f"Onboarding plan for member '{instance.name}':")
    typer.echo(f"$ {' '.join(command)}")
    if not execute:
        typer.echo("DRY RUN onboard-member complete. Re-run with --execute to open the TUI.")
        return

    onboard_result = subprocess.run(command, check=False)
    if onboard_result.returncode != 0:
        raise typer.Exit(code=onboard_result.returncode or 1)

    workspace_config, workspace_changed = ensure_workspace_config(instance)
    if workspace_changed:
        typer.echo(f"Configured managed agent workspace in {workspace_config}")

    restart_result = SystemdService().restart(instance.name, execute=True)
    _print_result(restart_result)
    if restart_result.return_code != 0:
        raise typer.Exit(code=1)


@app.command("diagnose-dashboard")
def diagnose_dashboard(
    config: ConfigPath,
    member: OptionalMemberName = None,
    format: StatusFormat = "text",
    show_token_url: ShowTokenUrlFlag = False,
) -> None:
    """Show dashboard URLs and token diagnostics for selected members."""
    inventory = _load(config)
    selected = _select_instances(inventory, member)
    if not selected:
        typer.echo("No matching members selected for dashboard diagnosis")
        return

    rows: list[dict[str, object]] = []
    for instance in selected:
        dashboard_url = _dashboard_host_url(instance)
        env_values = _instance_env_values(instance)
        token = env_values.get("OPENCLAW_GATEWAY_TOKEN", "")
        auth_url = f"{dashboard_url}#token={token}" if token else None
        row = {
            "instance": instance.name,
            "role": instance.role,
            "dashboard_url": dashboard_url,
            "token_present": bool(token),
            "token_env_key": "OPENCLAW_GATEWAY_TOKEN",
            "token_env_files": [str(Path(path).expanduser()) for path in instance.env_files],
            "auth_url": auth_url if show_token_url else None,
        }
        rows.append(row)

    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "cluster": inventory.cluster.name,
                    "mode": inventory.cluster.mode,
                    "instances": rows,
                },
                indent=2,
            )
        )
        return

    typer.echo(f"Dashboard diagnosis for cluster '{inventory.cluster.name}'")
    for row in rows:
        typer.echo(f"- {row['instance']} [{row['role']}]")
        typer.echo(f"  dashboard: {row['dashboard_url']}")
        typer.echo(f"  token_env_key: {row['token_env_key']}")
        typer.echo(f"  token_present: {'yes' if row['token_present'] else 'no'}")
        for env_file in row["token_env_files"]:
            typer.echo(f"  env_file: {env_file}")
        if show_token_url and row["auth_url"]:
            typer.echo(f"  auth_url: {row['auth_url']}")
        elif row["token_present"]:
            typer.echo("  hint: re-run with --show-token-url to print URL fragment auth")


@app.command("setup-quadlets")
def setup_quadlets(
    config: ConfigPath,
    member: OptionalMemberName = None,
    execute: ExecuteFlag = False,
) -> None:
    """Render and apply desired Quadlet definitions for the current team."""
    inventory = _load(config)
    selected = _select_instances(inventory, member)
    if not selected:
        typer.echo("No matching members selected for setup")
        return

    changed_artifacts = plan_deployment(inventory, selected, _template_root())
    changed_instance_names = {change.member for change in changed_artifacts}

    typer.echo(
        f"Setup plan for cluster '{inventory.cluster.name}' ({inventory.cluster.mode}): "
        f"{len(changed_instance_names)}/{len(selected)} member(s) changed"
    )

    for change in changed_artifacts:
        typer.echo(f" - {change.member} [{change.role}] write {change.destination}")
    typer.echo(
        "  Apply: retire legacy services, prepare runtime config, reload systemd, "
        "restart selected members."
    )

    if not execute:
        typer.echo("DRY RUN setup-quadlets complete. Re-run with --execute to mutate state.")
        return

    service = SystemdService()
    failed = False

    quadlet_roots = {host.name: Path(host.quadlet_root).expanduser() for host in inventory.hosts}
    if not _retire_legacy_services(
        service,
        selected,
        disable=True,
        quadlet_roots=quadlet_roots,
    ):
        raise typer.Exit(code=1)

    # Podman requires bind-mount sources to exist before starting the unit.
    for instance in selected:
        runtime_state = Path(instance.workspace_path).expanduser() / ".openclaw"
        runtime_state.mkdir(parents=True, exist_ok=True)
        if is_browser_image(instance.image):
            ensure_browser_cache(instance)
        workspace_config, workspace_config_changed = ensure_workspace_config(instance)
        if workspace_config_changed:
            typer.echo(f"Configured managed agent workspace in {workspace_config}")
        gateway_config, gateway_config_changed = ensure_control_ui_config(instance)
        if gateway_config_changed:
            typer.echo(f"Updated local Control UI access in {gateway_config}")

    for destination in apply_artifacts(changed_artifacts):
        typer.echo(f"Deployed {destination}")

    reload_result = service.daemon_reload(execute=True)
    _print_result(reload_result)
    if reload_result.return_code != 0:
        raise typer.Exit(code=1)

    if not changed_artifacts:
        typer.echo("No rendered changes detected; ensuring selected services are running.")

    # Setup is an idempotent reconciliation operation: even when the rendered
    # Quadlets already match, the selected service may be stopped or may never
    # have been started. Restart every selected member after daemon-reload.
    for instance in selected:
        restart_result = service.restart(instance.name, execute=True)
        _print_result(restart_result)
        failed = failed or restart_result.return_code != 0

    if failed:
        raise typer.Exit(code=1)


@app.command("restart-quadlets")
def restart_quadlets(
    config: ConfigPath,
    member: OptionalMemberName = None,
    execute: ExecuteFlag = False,
) -> None:
    """Restart selected or all managed services after config/image changes."""
    inventory = _load(config)
    selected = _select_instances(inventory, member)
    if not selected:
        typer.echo("No matching members selected for restart")
        return

    service = SystemdService()
    failed = False
    if execute and not _retire_legacy_services(service, selected, disable=False):
        raise typer.Exit(code=1)
    for instance in selected:
        result = service.restart(instance.name, execute=execute)
        _print_result(result)
        if result.return_code != 0:
            failed = True

    if not execute:
        typer.echo("DRY RUN restart-quadlets complete. Re-run with --execute to mutate state.")
        return

    if failed:
        raise typer.Exit(code=1)


@app.command("status-quadlets")
def status_quadlets(
    config: ConfigPath,
    member: OptionalMemberName = None,
    format: StatusFormat = "text",
) -> None:
    """Aggregate per-member runtime status with optional machine-readable output."""
    inventory = _load(config)
    selected = _select_instances(inventory, member)
    service = SystemdService()
    rows: list[dict[str, object]] = []

    for instance in selected:
        result = service.status(instance.name, execute=True)
        diagnostics = None
        if _status_needs_diagnostics(result):
            diagnostics = _status_diagnostics(service, instance.name)
        row = {
            "instance": instance.name,
            "role": instance.role,
            "state": _status_state(result),
            "return_code": result.return_code,
            "unit": service.unit_name(instance.name),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "diagnostics": (
                diagnostics.stdout
                if diagnostics and diagnostics.stdout
                else diagnostics.stderr
                if diagnostics
                else ""
            ),
        }
        rows.append(row)

    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "cluster": inventory.cluster.name,
                    "mode": inventory.cluster.mode,
                    "instances": rows,
                },
                indent=2,
            )
        )
    else:
        typer.echo(f"Cluster: {inventory.cluster.name} ({inventory.cluster.mode})")
        for row in rows:
            typer.echo(
                f"- {row['instance']} [{row['role']}] state={row['state']} rc={row['return_code']}"
            )
            diagnostics_summary = _diagnostic_summary(
                CommandResult(command=[], return_code=0, stdout=str(row["diagnostics"]), stderr="")
                if row["diagnostics"]
                else None
            )
            if diagnostics_summary:
                typer.echo(f"  cause: {diagnostics_summary}")

    if any(int(row["return_code"]) != 0 for row in rows):
        raise typer.Exit(code=1)


@app.command("teardown-quadlets")
def teardown_quadlets(
    config: ConfigPath,
    member: OptionalMemberName = None,
    execute: ExecuteFlag = False,
) -> None:
    """Remove managed runtime services for selected members or whole teams."""
    inventory = _load(config)
    selected = _select_instances(inventory, member)
    if not selected:
        typer.echo("No matching members selected for teardown")
        return

    host_map = {host.name: host for host in inventory.hosts}
    service = SystemdService()
    failed = False

    typer.echo(
        f"Teardown plan for cluster '{inventory.cluster.name}': {len(selected)} member(s) "
        f"(execute={execute})"
    )

    for chosen in selected:
        host = host_map[chosen.host]
        quadlet_targets = [
            Path(host.quadlet_root).expanduser() / artifact_path
            for artifact_path in chosen.quadlet_artifact_paths
        ]

        typer.echo(f"- Teardown {chosen.name} [{chosen.role}]")
        results = [
            service.stop(chosen.name, execute=execute),
            service.disable(chosen.name, execute=execute),
            service.remove_container(chosen.container_name, execute=execute),
        ]
        for quadlet_target in quadlet_targets:
            results.append(service.remove_quadlet(str(quadlet_target), execute=execute))
        for result in results:
            _print_result(result)
            if result.return_code != 0 and not _is_tolerated_teardown_error(result):
                failed = True

    reload_result = service.daemon_reload(execute=execute)
    _print_result(reload_result)
    if reload_result.return_code != 0:
        failed = True

    if not execute:
        typer.echo("DRY RUN teardown-quadlets complete. Re-run with --execute to mutate state.")
        return

    if failed:
        raise typer.Exit(code=1)


@app.command("validate")
def validate_inventory(config: ConfigPath, member: OptionalMemberName = None) -> None:
    """Validate inventory and render selected members without writes or runtime calls."""
    inventory = _load(config)
    selected = _select_instances(inventory, member)
    changes = plan_deployment(inventory, selected, _template_root())
    typer.echo(
        f"Valid inventory: {inventory.cluster.name}; {len(selected)} member(s), "
        f"{len(changes)} artifact change(s)."
    )


@app.command("logs")
def member_logs(
    config: ConfigPath,
    member: Annotated[str, typer.Option(..., "--member", "-m")],
    lines: Annotated[int, typer.Option("--lines", "-n", min=1, max=10000)] = 100,
) -> None:
    """Read the latest journal entries for one member."""
    instance = _instance_by_name(_load(config), member)
    result = SystemdService().logs(instance.name, lines=lines, execute=True)
    if result.stdout:
        typer.echo(result.stdout)
    if result.stderr:
        typer.echo(result.stderr, err=True)
    if result.return_code:
        raise typer.Exit(code=1)


# Intent-oriented names; established commands remain compatible with scripts and Make.
app.command("setup")(setup_quadlets)
app.command("restart")(restart_quadlets)
app.command("status")(status_quadlets)
app.command("teardown")(teardown_quadlets)
app.command("onboard")(onboard_member)
app.command("dashboard")(diagnose_dashboard)


if __name__ == "__main__":
    app()
