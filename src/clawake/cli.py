from __future__ import annotations

import json
import re
import subprocess
import warnings
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml

from clawake.config import (
    ImageSpec,
    InstanceSpec,
    Inventory,
    InventoryLayers,
    load_inventory_layers,
)
from clawake.inventory_validation import require_v1_runtime
from clawake.services.backup import backup_instance, prune_backups
from clawake.services.deployment import ArtifactChange, apply_artifacts, plan_deployment
from clawake.services.env_keys import EnvKeysError, env_file_keys
from clawake.services.image_check import ImageCheckError, check_image_availability
from clawake.services.member_ops import (
    plan_member_plugins,
    plan_setup_actions,
    plugin_sync_summary,
    run_plugin_sync,
    run_setup_actions,
    setup_summary,
)
from clawake.services.readiness import (
    SETTLED_STATES,
    MemberReadiness,
    default_probes,
    probe_member,
    wait_members_ready,
)
from clawake.services.rollout import (
    TIMESTAMP_PLACEHOLDER,
    RolloutUsageError,
    build_candidate,
    execute_rollout,
    failure_hint,
    plan_rollout,
    preview_lines,
    summary_lines,
)
from clawake.services.runtime_config import ADOPTIONS, pending_adoptions
from clawake.services.runtime_pins import (
    RuntimePinsWarning,
    count_fields,
    execute_init,
    pin_rows,
    plan_init,
)
from clawake.services.runtime_upgrade import (
    doctor_command,
    ensure_browser_cache,
    is_browser_image,
    run_doctor,
    verify_runtime,
)
from clawake.services.site_values import SiteValuesWarning
from clawake.services.systemd import CommandResult, SystemdService
from clawake.services.upgrade import (
    apply_upgrade,
    backup_config,
    build_upgrade_plan,
    plan_overlay_upgrade,
)

app = typer.Typer(
    help="Deploy and operate OpenClaw teams. Changes are previewed unless --execute is set.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

ConfigPath = Annotated[Path, typer.Option(..., "--config", "-c", exists=True, dir_okay=False)]
OptionalServiceName = Annotated[
    str | None, typer.Option("--service", "-s", help="Select a generic service.")
]

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
AdoptNames = Annotated[
    list[str] | None,
    typer.Option(
        "--adopt",
        help="Take over a named, not-yet-owned OpenClaw field once (discord-channel).",
    ),
]

_ENV_LINE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _load(path: Path, *, validation_only: bool = False) -> Inventory:
    return _load_layers(path, validation_only=validation_only).inventory


def _load_layers(
    path: Path, *, validation_only: bool = False, allow_missing_pins: bool = False
) -> InventoryLayers:
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimePinsWarning)
            warnings.simplefilter("always", SiteValuesWarning)
            layers = load_inventory_layers(path, allow_missing_pins=allow_missing_pins)
        for warning in caught:
            if issubclass(warning.category, (RuntimePinsWarning, SiteValuesWarning)):
                typer.echo(f"WARNING: {warning.message}", err=True)
            else:
                warnings.warn_explicit(
                    warning.message, warning.category, warning.filename, warning.lineno
                )
        if not validation_only:
            require_v1_runtime(layers.inventory)
        return layers
    except yaml.YAMLError as exc:
        # Parser excerpts can contain credentials mistakenly placed in an inventory.
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise typer.BadParameter(
            f"Cannot load {path}: invalid YAML{location}",
            param_hint="--config",
        ) from exc
    except (ValueError, OSError) as exc:
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
    inventory = _load(config, validation_only=True)
    selected = _select_instances(inventory, member)
    declared = [(instance, plugin) for instance in selected for plugin in instance.plugins]
    typer.echo(f"Plugin sync plan for {len(declared)} pinned plugin(s)")
    service = SystemdService()
    probes = default_probes(service)
    restarts: list[str] = []
    try:
        for instance in selected:
            # Read-only: one readiness probe, the install ledger and the config diff.
            plan = plan_member_plugins(instance, probes)
            if plan.readiness is not None:
                typer.echo(f" - {instance.name}: readiness={plan.readiness.state}")
            for plugin, decision in zip(instance.plugins, plan.decisions, strict=True):
                typer.echo(f" - {instance.name}: {plugin.id} ({decision.digest})")
                if plugin.source_type == "archive":
                    typer.echo(f"   mount: {plugin.artifact_path} -> {plugin.container_path}:ro")
                else:
                    typer.echo(f"   npm: {plugin.npm_spec}")
                typer.echo(f"   version: {decision.pinned_version or 'unreadable'}")
                reasons = f" ({', '.join(decision.reasons)})" if decision.reasons else ""
                typer.echo(f"   action: {decision.action}{reasons}")
            if plan.config_changed:
                typer.echo(f" - {instance.name}: managed plugin config would change")
            if plan.restart_expected:
                restarts.append(instance.name)
            elif plan.restart_possible:
                restarts.append(f"{instance.name} (if install)")
            if instance.global_tool_allow:
                typer.echo(
                    f" - {instance.name}: explicit global tools "
                    f"({', '.join(instance.global_tool_allow)})"
                )
            for agent_id, tools in sorted(instance.agent_tool_allow.items()):
                typer.echo(
                    f" - {instance.name}: agent {agent_id} optional tools ({', '.join(tools)})"
                )
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Members to restart: {', '.join(restarts) or 'none'}")

    if not execute:
        typer.echo(
            "DRY RUN sync-plugins complete. Run setup --execute first when an archive "
            "mount is new or changed, then re-run with --execute."
        )
        return

    rows, failed = run_plugin_sync(
        selected, service, probes, echo=typer.echo, on_result=_print_result
    )
    typer.echo("Plugin sync summary:")
    for line in plugin_sync_summary(rows):
        typer.echo(line)
    if failed:
        typer.echo("Plugin sync stopped at the first failing member.", err=True)
        raise typer.Exit(code=1)


BACKUP_DIR = Path(".backups")
MemberNames = Annotated[
    list[str] | None,
    typer.Option("--member", "-m", help="Select a member; repeatable. Default: entire team."),
]
PluginArchives = Annotated[
    list[str] | None,
    typer.Option(
        "--plugin",
        help="Roll out an archive plugin: <id>=<path-to-tgz>; repeatable. Requires runtime_pins.",
    ),
]


@app.command("rollout")
def rollout(
    config: ConfigPath,
    member: MemberNames = None,
    plugin: PluginArchives = None,
    execute: ExecuteFlag = False,
) -> None:
    """Roll out members in one step: backup, apply, restart, readiness, plugins, verify, pins."""
    layers = _load_layers(config, validation_only=True)
    try:
        candidate = build_candidate(layers, member or [], plugin or [])
    except RolloutUsageError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    service = SystemdService()
    probes = default_probes(service)
    pins = candidate.changed_pins
    typer.echo(
        f"Rollout plan for cluster '{candidate.inventory.cluster.name}': "
        f"{len(candidate.selected)} member(s), {len(pins)} plugin pin change(s)"
    )
    if candidate.pins_path is not None:
        typer.echo(
            f"Runtime pins: {candidate.pins_path} (a member's new pins are committed only "
            "after it verified)"
        )
    try:
        # Read-only: rendered diff, unit state, readiness, ledger and config diff.
        plan = plan_rollout(candidate, probes, _template_root(), BACKUP_DIR)
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _print_plan(list(plan.artifacts))
    for line in preview_lines(plan):
        typer.echo(line)
    if not execute:
        typer.echo("DRY RUN rollout complete. Re-run with --execute.")
        return

    # No A2A environment check here: it reads token values, and rollout never reads secrets.
    result = execute_rollout(plan, service, probes, echo=typer.echo, on_result=_print_result)
    typer.echo("Rollout summary:")
    for line in summary_lines(result):
        typer.echo(line)
    if result.failed:
        for line in failure_hint(plan, result):
            typer.echo(line, err=True)
        raise typer.Exit(code=1)
    typer.echo("Rollout complete.")


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


def _print_plan(changes: list[ArtifactChange]) -> None:
    for change in changes:
        typer.echo(f" - {change.member} [{change.role}] {change.kind}: write {change.destination}")
        if change.changed_keys:
            typer.echo(f"   changed keys: {', '.join(change.changed_keys)}")


def _check_discord_token_env(instances: list[InstanceSpec]) -> None:
    """Fail closed unless token_env is a key in a member env file (ADR-005 item 4).

    Uses key names only; token values are never read into memory beyond the line parser.
    """
    for instance in instances:
        spec = instance.openclaw.discord if instance.openclaw else None
        if spec is None or not spec.enabled or spec.token_env is None:
            continue
        missing: list[str] = []
        declared = False
        for raw_path in instance.env_files:
            path = Path(raw_path).expanduser()
            if not path.is_file():
                missing.append(str(path))
                continue
            try:
                keys = env_file_keys(path)
            except EnvKeysError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=2) from None
            if spec.token_env in keys:
                declared = True
                break
        if not declared:
            files = ", ".join(instance.env_files) or "none"
            detail = f"; missing: {', '.join(missing)}" if missing else ""
            typer.echo(
                f"Error: discord.token_env {spec.token_env} of member {instance.name} is not "
                f"declared as a key in its env_files ({files}{detail}); add "
                f"{spec.token_env}=<bot token> to one of them",
                err=True,
            )
            raise typer.Exit(code=2)


def _plan_or_exit(
    inventory: Inventory,
    selected: list[InstanceSpec],
    adopt: tuple[str, ...] | None,
) -> tuple[dict[str, tuple[str, ...]], list[ArtifactChange]]:
    """Plan read-only; a planning refusal becomes `Error: <message>` with exit 1.

    `adopt=None` means: plan with every pending adoption (validate never writes). The
    adoption preconditions still run. Returns the pending adoptions per member that are
    covered by the plan, and the planned changes.
    """
    try:
        pending = {
            instance.name: names
            for instance in selected
            if (adopt is None or adopt) and (names := pending_adoptions(inventory, instance))
        }
        if adopt is None:
            adopt = tuple(dict.fromkeys(name for names in pending.values() for name in names))
        changes = plan_deployment(inventory, selected, _template_root(), adopt=adopt)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    return pending, changes


def _requested_adoptions(adopt: list[str] | None) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(adopt or ()))
    for name in names:
        if name not in ADOPTIONS:
            typer.echo(
                f"Error: Unknown adoption '{name}'. Available: {', '.join(sorted(ADOPTIONS))}",
                err=True,
            )
            raise typer.Exit(code=2)
    return names


def _check_a2a_environment(instances: list[InstanceSpec], inventory: Inventory) -> None:
    """Check configured environment files before writes; never disclose values."""
    environments = {item.name: _instance_env_values(item) for item in inventory.instances}
    for instance in instances:
        if not instance.openclaw or not instance.openclaw.a2a.enabled:
            continue
        values = environments[instance.name]
        required = {"OPENCLAW_GATEWAY_TOKEN"}
        for peer in instance.openclaw.a2a.peers.values():
            required.update([peer.inbound_token_env, peer.outbound_token_env])
        missing = sorted(key for key in required if not values.get(key))
        if missing:
            raise typer.BadParameter(
                f"Missing environment values for {instance.name}: {', '.join(missing)}"
            )
        for peer in instance.openclaw.a2a.peers.values():
            remote_values = environments[peer.instance]
            for key in (peer.inbound_token_env, peer.outbound_token_env):
                if remote_values.get(key) and values[key] != remote_values[key]:
                    raise typer.BadParameter(
                        f"A2A token mismatch for {key}: {instance.name}, {peer.instance}"
                    )


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

    try:
        plan = build_upgrade_plan(config, member, target_tag, target_digest)
        # ADR-004: with runtime_pins the image fields go to the overlay; validate that write now.
        pins_path = plan_overlay_upgrade(config, member, target_tag, target_digest)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(f"Cannot plan upgrade: {exc}", param_hint="--config") from exc
    target = ImageSpec(
        repository=instance.image.repository,
        tag=plan.next_tag,
        digest=plan.next_digest,
    )
    target_inventory = inventory.model_copy(deep=True)
    target_instance = _instance_by_name(target_inventory, member)
    target_instance.image = target
    changes = plan_deployment(target_inventory, [target_instance], _template_root())
    typer.echo(f"Upgrade plan for member '{member}':")
    typer.echo(f"  image: {plan.previous_tag} -> {plan.next_tag}")
    typer.echo(f"  digest: {plan.previous_digest or 'unpinned'} -> {plan.next_digest}")
    typer.echo(f"  backup: {'yes' if instance.backup_policy.pre_mutation else 'no'}")
    if pins_path is not None:
        typer.echo(f"  pins: {pins_path} (inventory file is not modified)")
    typer.echo(f"  migration: {' '.join(doctor_command(instance, target)[-5:])}")
    typer.echo(f"  health: {_dashboard_host_url(instance).rstrip('/')}{instance.health.path}")
    typer.echo(f"  readiness wait: up to {instance.readiness.start_window_seconds}s")
    _print_plan(changes)
    if not execute:
        typer.echo("DRY RUN upgrade complete. Re-run with --execute to mutate state.")
        return

    _check_a2a_environment([instance], inventory)

    service = SystemdService()
    backup_dir = Path(".backups")
    stopped = False
    config_changed = False
    runtime_backup: Path | None = None
    config_backup: Path | None = None
    pins_backup: Path | None = None

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

        with warnings.catch_warnings():
            # Overlay permission warnings were already reported when the inventory was loaded.
            warnings.simplefilter("ignore", RuntimePinsWarning)
            applied = apply_upgrade(config, instance.name, target.tag, target.digest)
        config_changed = True
        pins_backup = applied.pins_backup
        if pins_backup is not None:
            typer.echo(f"Updated runtime pins; previous pins: {pins_backup}")
        updated_inventory = _load(config)
        updated_instance = _instance_by_name(updated_inventory, member)
        # Doctor may have migrated the configuration. Recompute the same plan against it.
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

        # ADR-003: wait on readiness for the whole start window, not a fixed retry count.
        probes = default_probes(service)
        wait_started = probes.clock()
        readiness = wait_members_ready([updated_instance], probes=probes)[0]
        ready_seconds = probes.clock() - wait_started
        reason_codes = ", ".join(reason.code for reason in readiness.reasons)
        if readiness.state not in SETTLED_STATES:
            raise RuntimeError(f"readiness {readiness.state}: {reason_codes or 'no detail'}")
        if readiness.state == "degraded":
            typer.echo(f"WARNING: {instance.name} is degraded: {reason_codes}", err=True)
        health_details = f"readiness={readiness.state} after {ready_seconds:.0f}s"

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
        if pins_backup:
            typer.echo(f"Runtime pins recovery file: {pins_backup}", err=True)
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

    _check_a2a_environment([instance], inventory)

    onboard_result = subprocess.run(command, check=False)
    if onboard_result.returncode != 0:
        raise typer.Exit(code=onboard_result.returncode or 1)

    changes = plan_deployment(inventory, [instance], _template_root())
    _print_plan(changes)
    apply_artifacts(changes)
    reload_result = SystemdService().daemon_reload(execute=True)
    _print_result(reload_result)
    if reload_result.return_code != 0:
        raise typer.Exit(code=1)

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
    service_name: OptionalServiceName = None,
    execute: ExecuteFlag = False,
    adopt: AdoptNames = None,
) -> None:
    """Render and apply desired Quadlet definitions for the current team."""
    inventory = _load(config, validation_only=True)
    if inventory.version == 2:
        if adopt:
            raise typer.BadParameter("--adopt requires inventory v1", param_hint="--adopt")
        _v2_operation(inventory, "setup", member, service_name, execute)
        return
    if service_name:
        raise typer.BadParameter("Services require inventory v2")
    requested = _requested_adoptions(adopt)
    selected = _select_instances(inventory, member)
    if not selected:
        typer.echo("No matching members selected for setup")
        return
    _check_discord_token_env(selected)

    # Read-only: which members need a requested named adoption (and a backup first).
    adoptions, changed_artifacts = _plan_or_exit(inventory, selected, requested)
    changed_instance_names = {
        change.member for change in changed_artifacts if change.role != "shared_network"
    }

    typer.echo(
        f"Setup plan for cluster '{inventory.cluster.name}' ({inventory.cluster.mode}): "
        f"{len(changed_instance_names)}/{len(selected)} member(s) changed"
    )

    _print_plan(changed_artifacts)
    if adoptions:
        typer.echo("Adoptions:")
        for name, names in adoptions.items():
            backup = BACKUP_DIR / f"{name}-{TIMESTAMP_PLACEHOLDER}.tar.gz"
            typer.echo(f" - {name}: adopt {', '.join(names)} (backup first: {backup})")
    service = SystemdService()
    probes = default_probes(service)
    # Read-only: the unit state of unchanged members decides between start and none.
    actions = plan_setup_actions(selected, changed_artifacts, probes.unit_status)
    typer.echo("Member actions:")
    for action in actions:
        typer.echo(f" - {action.member}: {action.action} ({', '.join(action.reasons)})")
    typer.echo(
        "  Apply: retire legacy services, prepare runtime config, reload systemd, then one "
        "member at a time: act, wait for ready/degraded within its start window; stop at "
        "the first failure."
    )

    if not execute:
        typer.echo("DRY RUN setup-quadlets complete. Re-run with --execute to mutate state.")
        return

    _check_a2a_environment(selected, inventory)

    # ADR-005 item 5: the member backup precedes replacing an adopted field.
    for instance in selected:
        if instance.name not in adoptions:
            continue
        try:
            archive = backup_instance(instance, BACKUP_DIR, execute=True)
        except OSError as exc:
            typer.echo(
                f"Backup before adopting {', '.join(adoptions[instance.name])} for "
                f"{instance.name} failed: {exc}. Nothing was changed.",
                err=True,
            )
            raise typer.Exit(code=1) from exc
        typer.echo(f"Created runtime backup for {instance.name}: {archive}")

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
        runtime_state = instance.runtime_state_path
        runtime_state.mkdir(parents=True, exist_ok=True)
        if is_browser_image(instance.image):
            ensure_browser_cache(instance)

    for destination in apply_artifacts(changed_artifacts):
        typer.echo(f"Deployed {destination}")

    reload_result = service.daemon_reload(execute=True)
    _print_result(reload_result)
    if reload_result.return_code != 0:
        raise typer.Exit(code=1)

    if not changed_artifacts:
        typer.echo("No rendered changes detected; starting only inactive members.")

    # Restart only changed members; start unchanged inactive ones (#5 problem 1).
    # Architecture-Ref: clawake/docs/architecture.md#lifecycle-contracts
    # The doc still says setup restarts unchanged members; ADR-003 item 3 supersedes it.
    outcomes = run_setup_actions(
        selected, actions, service, probes, echo=typer.echo, on_result=_print_result
    )
    typer.echo("Setup summary:")
    for line in setup_summary(outcomes):
        typer.echo(line)
    if any(outcome.result != "ok" for outcome in outcomes):
        failed = next(outcome for outcome in outcomes if outcome.result == "failed")
        typer.echo(
            f"Setup stopped at {failed.member}: {', '.join(failed.reasons)}. "
            "Later members were not touched.",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command("stop")
def stop_members(
    config: ConfigPath,
    member: OptionalMemberName = None,
    execute: ExecuteFlag = False,
) -> None:
    """Stop managed services without removing runtime data or Quadlets."""
    selected = _select_instances(_load(config), member)
    service = SystemdService()
    failed = False
    for instance in selected:
        result = service.stop(instance.name, execute=execute)
        _print_result(result)
        failed = failed or result.return_code != 0
    if failed:
        raise typer.Exit(code=1)
    if not execute:
        typer.echo("DRY RUN stop complete. Re-run with --execute to mutate state.")


@app.command("restart-quadlets")
def restart_quadlets(
    config: ConfigPath,
    member: OptionalMemberName = None,
    service_name: OptionalServiceName = None,
    execute: ExecuteFlag = False,
) -> None:
    """Restart selected or all managed services after config/image changes."""
    inventory = _load(config, validation_only=True)
    if inventory.version == 2:
        _v2_operation(inventory, "restart", member, service_name, execute)
        return
    if service_name:
        raise typer.BadParameter("Services require inventory v2")
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


WaitFlag = Annotated[
    bool,
    typer.Option(
        "--wait",
        help="Poll until every selected member is ready or degraded, bounded by the "
        "member's readiness.start_window_seconds.",
    ),
]
WaitTimeout = Annotated[
    int | None,
    typer.Option(
        "--timeout",
        min=1,
        max=3600,
        help="Override the start window in seconds for --wait.",
    ),
]


@app.command("status-quadlets")
def status_quadlets(
    config: ConfigPath,
    member: OptionalMemberName = None,
    service_name: OptionalServiceName = None,
    format: StatusFormat = "text",
    wait: WaitFlag = False,
    timeout: WaitTimeout = None,
) -> None:
    """Aggregate per-member runtime status and readiness with optional JSON output."""
    # Architecture-Ref: clawake/docs/architecture.md#ownership-and-isolation
    # The doc still says status represents systemd state only; ADR-003 adds readiness.
    inventory = _load(config, validation_only=True)
    if inventory.version == 2:
        if wait or timeout is not None:
            raise typer.BadParameter("--wait and --timeout are only supported for v1 members")
        _v2_operation(inventory, "status", member, service_name, False, format)
        return
    if service_name:
        raise typer.BadParameter("Services require inventory v2")
    if timeout is not None and not wait:
        raise typer.BadParameter("--timeout requires --wait", param_hint="--timeout")
    selected = _select_instances(inventory, member)
    service = SystemdService()
    probes = default_probes(service)
    if wait:
        readiness = wait_members_ready(selected, timeout=timeout, probes=probes)
    else:
        readiness = [probe_member(instance, probes) for instance in selected]
    rows: list[dict[str, object]] = []

    for instance, member_readiness in zip(selected, readiness, strict=True):
        result = member_readiness.unit_result
        assert result is not None
        diagnostics = None
        if _status_needs_diagnostics(result):
            diagnostics = _status_diagnostics(service, instance.name)
        row = {
            "instance": instance.name,
            "role": instance.role,
            # Deprecated: systemctl-only view; use "readiness" (ADR-003).
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
            "readiness": member_readiness.state,
            "reasons": [reason.to_json() for reason in member_readiness.reasons],
            "plugins": [plugin.to_json() for plugin in member_readiness.plugins],
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
        for row, member_readiness in zip(rows, readiness, strict=True):
            typer.echo(
                f"- {row['instance']} [{row['role']}] state={row['state']} "
                f"rc={row['return_code']} readiness={member_readiness.state}"
            )
            if member_readiness.state == "degraded":
                typer.echo("  WARNING: degraded; the gateway is ready but checks below failed")
            for reason in member_readiness.reasons:
                typer.echo(f"  reason: {reason.code}: {reason.detail}")
            for plugin in member_readiness.plugins:
                typer.echo(
                    f"  plugin: {plugin.id} pinned={plugin.pinned_version or '-'} "
                    f"installed={plugin.installed_version or '-'} "
                    f"status={plugin.status or '-'} enabled={_tri_state(plugin.enabled)}"
                )
            diagnostics_summary = _diagnostic_summary(
                CommandResult(command=[], return_code=0, stdout=str(row["diagnostics"]), stderr="")
                if row["diagnostics"]
                else None
            )
            if diagnostics_summary:
                typer.echo(f"  cause: {diagnostics_summary}")

    if _status_exit_code(readiness, waited=wait):
        raise typer.Exit(code=1)


def _tri_state(value: bool | None) -> str:
    if value is None:
        return "-"
    return "yes" if value else "no"


def _status_exit_code(readiness: list[MemberReadiness], *, waited: bool) -> int:
    """Non-zero for stopped/failed; after --wait also for anything not ready/degraded."""
    if any(item.state in {"stopped", "failed"} for item in readiness):
        return 1
    if waited and any(item.state not in SETTLED_STATES for item in readiness):
        return 1
    return 0


@app.command("teardown-quadlets")
def teardown_quadlets(
    config: ConfigPath,
    member: OptionalMemberName = None,
    service_name: OptionalServiceName = None,
    execute: ExecuteFlag = False,
) -> None:
    """Remove managed runtime services for selected members or whole teams."""
    inventory = _load(config, validation_only=True)
    if inventory.version == 2:
        _v2_operation(inventory, "teardown", member, service_name, execute)
        return
    if service_name:
        raise typer.BadParameter("Services require inventory v2")
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

    # Shared resources belong to the inventory, never to a single member.
    selected_names = {chosen.name for chosen in selected}
    for network in inventory.networks:
        owners = {item.name for item in inventory.instances if network.name in item.networks}
        if failed or not owners or not owners.issubset(selected_names):
            continue
        result = service.stop(f"{network.name}-network", execute=execute)
        _print_result(result)
        if result.return_code != 0 and not _is_tolerated_teardown_error(result):
            failed = True
            continue
        target = Path(host_map[network.host].quadlet_root).expanduser() / network.quadlet_path
        result = service.remove_quadlet(str(target), execute=execute)
        _print_result(result)
        failed = failed or result.return_code != 0

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
    """Validate desired state offline; v2 is model-only until service rendering exists."""
    layers = _load_layers(config, validation_only=True)
    inventory = layers.inventory
    selected = _select_instances(inventory, member)
    if inventory.version == 2:
        typer.echo(
            f"Valid inventory v2: {inventory.cluster.name}; {len(inventory.instances)} member(s), "
            f"{len(inventory.services)} service(s), {len(inventory.networks)} network(s). "
            "Model validation only; no rendering or runtime/secret existence checks."
        )
        return
    # Plan with every pending adoption: validate never writes, never adopts (ADR-005 item 5).
    pending, changes = _plan_or_exit(inventory, selected, None)
    typer.echo(
        f"Valid inventory: {inventory.cluster.name}; {len(selected)} member(s), "
        f"{len(changes)} artifact change(s)."
    )
    if layers.overlay is not None:
        members, fields = count_fields(layers.overlay)
        typer.echo(f"runtime pins: {layers.pins_path} ({members} members, {fields} fields)")
    if layers.site is not None:
        site = layers.site
        typer.echo(
            f"site values: {site.path} ({site.value_count} values, "
            f"{len(site.references)} references)"
        )
        if site.unused:
            # Names only, never values (ADR-005 item 2).
            typer.echo(
                f"WARNING: site values file {site.path}: unused values: {', '.join(site.unused)}",
                err=True,
            )
    for name, names in pending.items():
        for adoption in names:
            typer.echo(
                f"pending adoption: {name} {adoption} "
                f"(review with: clawake setup -c {config} --adopt {adoption})"
            )


@app.command("upgrade-service")
def service_upgrade(
    config: ConfigPath,
    service_name: Annotated[str, typer.Option(..., "--service", "-s")],
    tag: TargetTag,
    digest: Annotated[str, typer.Option(..., "--digest")],
    execute: ExecuteFlag = False,
    with_dependents: Annotated[bool, typer.Option("--with-dependents")] = False,
) -> None:
    """Preview a pinned generic service upgrade with consistent private backups."""
    from clawake.services.service_upgrade import upgrade_service

    try:
        for entry in upgrade_service(
            config,
            service_name,
            tag,
            digest,
            _template_root(),
            execute=execute,
            include_dependents=with_dependents,
        ):
            typer.echo(entry)
        if not execute:
            typer.echo("DRY RUN; re-run with --execute to apply")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("logs")
def member_logs(
    config: ConfigPath,
    member: OptionalMemberName = None,
    service_name: OptionalServiceName = None,
    lines: Annotated[int, typer.Option("--lines", "-n", min=1, max=10000)] = 100,
) -> None:
    """Read the latest journal entries for one resource."""
    from clawake.services.resources import select_resources

    inventory = _load(config, validation_only=True)
    if not member and not service_name:
        raise typer.BadParameter("Select --member or --service for logs")
    try:
        selected = select_resources(inventory, member, service_name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    result = SystemdService().logs(selected[0].name, lines=lines, execute=True)
    if result.stdout:
        typer.echo(result.stdout)
    if result.stderr:
        typer.echo(result.stderr, err=True)
    if result.return_code:
        raise typer.Exit(code=1)


def _v2_operation(inventory, action, member, service_name, execute=False, format="text"):
    from clawake.services.resources import lifecycle, status_rows

    try:
        if action == "status":
            rows = status_rows(inventory, member, service_name)
            if format == "json":
                typer.echo(
                    json.dumps({"cluster": inventory.cluster.name, "resources": rows}, indent=2)
                )
            else:
                for row in rows:
                    typer.echo(
                        f"{row['name']}: active={row['active']} readiness={row['readiness']}"
                    )
            if any(
                not row["active"]
                or row["readiness"] not in {"healthy", "running"}
                or row["failed_dependencies"]
                for row in rows
            ):
                raise typer.Exit(1)
        else:

            def prepare_instances(instances):
                _check_a2a_environment(instances, inventory)
                roots = {h.name: Path(h.quadlet_root) for h in inventory.hosts}
                if not _retire_legacy_services(
                    SystemdService(), instances, disable=action == "setup", quadlet_roots=roots
                ):
                    raise ValueError("Cannot retire legacy members")

            for entry in lifecycle(
                inventory,
                action,
                member,
                service_name,
                _template_root(),
                execute=execute,
                prepare_instances=prepare_instances,
            ):
                typer.echo(entry)
            if not execute:
                typer.echo("DRY RUN; re-run with --execute to apply")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise typer.BadParameter(str(exc)) from exc


# Intent-oriented names; established commands remain compatible with scripts and Make.
app.command("setup")(setup_quadlets)
app.command("restart")(restart_quadlets)
app.command("status")(status_quadlets)
app.command("teardown")(teardown_quadlets)
app.command("onboard")(onboard_member)
app.command("dashboard")(diagnose_dashboard)


@app.command("onshape-mcp")
def onshape_mcp(
    config: ConfigPath,
    action: Annotated[
        Literal["plan", "apply", "status", "restart", "stop", "probe"], typer.Option("--action")
    ] = "plan",
    execute: ExecuteFlag = False,
) -> None:
    """Operate the isolated external-token MCP service; never handle OAuth tokens."""
    from clawake.services.onshape_mcp import (
        OnshapeMcpSpec,
        plan_onshape_mcp,
        probe_native,
        validate_runtime_paths,
    )

    try:
        spec = OnshapeMcpSpec.model_validate(yaml.safe_load(config.read_text()))
    except (ValueError, OSError, yaml.YAMLError) as exc:
        raise typer.BadParameter("Invalid Onshape MCP service specification") from exc
    if action == "probe":
        if execute:
            try:
                typer.echo(json.dumps(probe_native(spec)))
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                raise typer.BadParameter("Native local MCP discovery failed") from exc
        else:
            typer.echo("Native operator-only local MCP discovery; no agent grants or tool calls")
        return
    service = SystemdService()
    if action in {"status", "restart", "stop"}:
        operation = {"status": service.status, "restart": service.restart, "stop": service.stop}
        result = operation[action](spec.name, execute=execute)
        typer.echo(" ".join(result.command))
        if execute:
            typer.echo(f"exit={result.return_code}")
        if result.return_code:
            raise typer.Exit(1)
        return
    changes = plan_onshape_mcp(spec)
    for change in changes:
        typer.echo(f"{change.member}: {change.destination}")
    if action == "apply" and execute:
        validate_runtime_paths(spec)
        apply_artifacts(changes)
        for operation in [
            lambda: service.daemon_reload(execute=True),
            lambda: service.restart(spec.name, execute=True),
        ]:
            result = operation()
            if result.return_code:
                raise typer.BadParameter("Onshape MCP service start failed; inspect systemd")
        typer.echo("Onshape MCP service configured and started; credential readiness unverified")


pins_app = typer.Typer(
    help="Inspect and migrate the private runtime pin overlay (ADR-004).",
    no_args_is_help=True,
)
app.add_typer(pins_app, name="pins")


@pins_app.command("show")
def pins_show(config: ConfigPath, format: StatusFormat = "text") -> None:
    """Show effective image and plugin pins per member and where each value comes from."""
    layers = _load_layers(config)
    rows = pin_rows(layers.raw, layers.overlay)
    members: dict[str, dict[str, dict[str, object]]] = {
        instance.name: {} for instance in layers.inventory.instances
    }
    for row in rows:
        members[row.member][row.field] = {
            "value": row.value,
            "source": row.source,
            "also_in_inventory": row.also_in_inventory,
        }
    if format == "json":
        payload = {
            "cluster": layers.inventory.cluster.name,
            "runtime_pins": str(layers.pins_path) if layers.pins_path else None,
            "members": members,
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"runtime pins: {layers.pins_path or 'none declared'}")
    for name, fields in members.items():
        typer.echo(name)
        for field_name, entry in fields.items():
            source = str(entry["source"])
            if entry["also_in_inventory"]:
                source += ", also in inventory"
            typer.echo(f"  {field_name} = {entry['value']} [{source}]")


@pins_app.command("init")
def pins_init(
    config: ConfigPath,
    output: Annotated[
        Path, typer.Option(..., "--output", help="New overlay file; never overwritten.")
    ],
    execute: ExecuteFlag = False,
) -> None:
    """Extract the current pins into a new overlay. Never edits the inventory."""
    _load_layers(config, allow_missing_pins=True)
    try:
        with warnings.catch_warnings():
            # Overlay permission warnings were already reported by _load_layers.
            warnings.simplefilter("ignore", RuntimePinsWarning)
            plan = plan_init(config, output)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--output") from exc
    typer.echo(f"Runtime pins overlay to write: {plan.output} (mode 0600)")
    typer.echo(plan.content, nl=False)
    typer.echo(f"Then remove these {len(plan.removals)} field(s) from {config}:")
    for removal in plan.removals:
        typer.echo(f"  - {removal}")
    if plan.already_declared is not None:
        typer.echo(f"runtime_pins already declared: {plan.already_declared}")
        if plan.already_declared != plan.output:
            typer.echo(f"Then change it to: {plan.reference_line}")
    else:
        typer.echo("And add this top-level line:")
        typer.echo(f"  {plan.reference_line}")
    typer.echo(f"Verify with: clawake validate -c {config} and clawake pins show -c {config}")
    if not execute:
        typer.echo("DRY RUN; the inventory is never edited. Re-run with --execute to write.")
        return
    try:
        execute_init(plan)
    except (ValueError, OSError) as exc:
        typer.echo(f"Writing runtime pins failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Wrote {plan.output}. The inventory {config} was not modified.")


if __name__ == "__main__":
    app()
