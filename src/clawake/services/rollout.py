"""One-step rollout for v1 members (ADR-003 item 5, ADR-004).

`clawake rollout` combines the accepted building blocks into one reviewed operation:

1. Build the candidate state in memory: inventory + runtime pin overlay + the archive
   plugin pins given with ``--plugin``, validated through the same path as
   ``load_inventory``. Nothing is written.
2. Plan read-only: rendered artifact diff, setup action per member, readiness now, plugin
   decisions, grant changes and the backups that would be taken.
3. With ``--execute``: take all backups first; then one member at a time: deploy from the
   candidate state, reload systemd, act (restart / start / none) and wait for readiness,
   sync plugins with P2 semantics (install or skip, ledger, config/grants, restart only on
   change, verify), and only then commit that member's new pins to the overlay. Stop at the
   first failure; later members are not touched and keep their committed pins.

Every host call goes through the injected seams (`SupportsLifecycle`, `ReadinessProbes`)
and the module-level functions that tests replace (`backup_instance`, `apply_artifacts`,
`set_plugin_pins`). Secrets are never read or printed.
"""

from __future__ import annotations

import os
import tarfile
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from clawake.config import InstanceSpec, Inventory, InventoryLayers, PluginSpec
from clawake.services.backup import backup_instance
from clawake.services.deployment import ArtifactChange, apply_artifacts, plan_deployment
from clawake.services.member_ops import (
    DIGEST_PREFIX_LENGTH,
    Echo,
    MemberOutcome,
    MemberPluginPlan,
    PluginSyncRow,
    SetupAction,
    SupportsLifecycle,
    plan_member_plugins,
    plan_setup_actions,
    render_table,
    run_plugin_sync,
    run_setup_actions,
)
from clawake.services.plugins import artifact_digest
from clawake.services.readiness import (
    MemberReadiness,
    ReadinessProbes,
    pinned_plugin_version,
    probe_member,
)
from clawake.services.runtime_pins import (
    PluginPin,
    RuntimePinsError,
    RuntimePinsWarning,
    copy_overlay,
    plan_plugin_pins,
    set_plugin_pins,
)
from clawake.services.runtime_upgrade import ensure_browser_cache, is_browser_image
from clawake.services.systemd import CommandResult

ROLLOUT_V1_ONLY = "rollout supports inventory version 1 only"
TIMESTAMP_PLACEHOLDER = "<UTC timestamp>"
RunResult = Literal["ok", "failed", "not_run"]


class RolloutUsageError(ValueError):
    """The request is invalid; raised before any host read or write (exit code 2)."""


def _silent(*_args: object, **_kwargs: object) -> None:
    return None


def _prefix(digest: str | None) -> str | None:
    return digest[:DIGEST_PREFIX_LENGTH] if digest else None


# --- candidate state (in memory) ------------------------------------------------------------


@dataclass(frozen=True)
class PluginRequest:
    """One ``--plugin <id>=<path>`` request after inspecting the archive."""

    plugin_id: str
    artifact_path: str
    sha256: str
    version: str


@dataclass(frozen=True)
class PinChange:
    member: str
    plugin_id: str
    old_artifact_path: str | None
    old_sha256: str | None
    old_version: str | None
    new: PluginRequest

    @property
    def changed(self) -> bool:
        return (self.old_artifact_path, self.old_sha256) != (
            self.new.artifact_path,
            self.new.sha256,
        )


@dataclass(frozen=True)
class RolloutCandidate:
    config_path: Path
    pins_path: Path | None
    current: Inventory
    inventory: Inventory
    selected: tuple[InstanceSpec, ...]
    pin_changes: tuple[PinChange, ...]

    def changes_for(self, member: str) -> tuple[PinChange, ...]:
        return tuple(item for item in self.pin_changes if item.member == member and item.changed)

    @property
    def changed_pins(self) -> tuple[PinChange, ...]:
        return tuple(item for item in self.pin_changes if item.changed)


def parse_plugin_option(value: str) -> tuple[str, str]:
    plugin_id, separator, path = value.partition("=")
    if not separator or not plugin_id.strip() or not path.strip():
        raise RolloutUsageError(f"--plugin expects <id>=<path-to-tgz>, got {value!r}")
    return plugin_id.strip(), path.strip()


def inspect_archive(plugin_id: str, path_text: str) -> PluginRequest:
    """Check that the archive is a readable file; compute its digest and read its version."""
    path = Path(os.path.abspath(Path(path_text).expanduser()))
    if not path.is_file():
        raise RolloutUsageError(
            f"plugin archive for '{plugin_id}' does not exist or is not a file: {path}"
        )
    if not os.access(path, os.R_OK):
        raise RolloutUsageError(f"plugin archive for '{plugin_id}' is not readable: {path}")
    try:
        digest = artifact_digest(path)
    except OSError as exc:
        raise RolloutUsageError(
            f"plugin archive for '{plugin_id}' is not readable: {path} ({exc.strerror})"
        ) from exc
    try:
        spec = PluginSpec(id=plugin_id, artifact_path=str(path), sha256=digest)
    except ValidationError as exc:
        raise RolloutUsageError(f"invalid plugin id {plugin_id!r}") from exc
    try:
        version = pinned_plugin_version(spec)
    except ValueError as exc:
        raise RolloutUsageError(
            f"cannot read the version (package/package.json) of plugin archive {path}: {exc}"
        ) from exc
    return PluginRequest(plugin_id, str(path), digest, version)


def _select(inventory: Inventory, members: Sequence[str]) -> list[InstanceSpec]:
    names = [instance.name for instance in inventory.instances]
    for name in members:
        if name not in names:
            raise RolloutUsageError(f"Unknown member '{name}'. Available: {', '.join(names)}")
    if not members:
        return list(inventory.instances)
    wanted = set(members)
    return [instance for instance in inventory.instances if instance.name in wanted]


def _old_version(plugin: PluginSpec) -> str | None:
    try:
        return pinned_plugin_version(plugin)
    except ValueError:
        return None


def build_candidate(
    layers: InventoryLayers, members: Sequence[str], plugin_options: Sequence[str]
) -> RolloutCandidate:
    """Validate the request and build the candidate effective inventory; nothing is written.

    Raises `RolloutUsageError` for every invalid request, before any host read.
    """
    current = layers.inventory
    if current.version != 1:
        raise RolloutUsageError(ROLLOUT_V1_ONLY)
    selected = _select(current, members)
    parsed = [parse_plugin_option(option) for option in plugin_options]
    seen: set[str] = set()
    for plugin_id, _path in parsed:
        if plugin_id in seen:
            raise RolloutUsageError(f"--plugin {plugin_id} is given more than once")
        seen.add(plugin_id)
    if parsed and layers.pins_path is None:
        raise RolloutUsageError(
            f"inventory {layers.inventory_path} does not declare runtime_pins; --plugin pins "
            "are committed to the runtime pin overlay. Run 'clawake pins init' first."
        )

    targets: dict[str, list[InstanceSpec]] = {}
    for plugin_id, _path in parsed:
        declared = []
        for instance in selected:
            plugin = next((item for item in instance.plugins if item.id == plugin_id), None)
            if plugin is None:
                continue
            if plugin.source_type != "archive":
                raise RolloutUsageError(
                    f"plugin '{plugin_id}' is declared as an npm plugin on member "
                    f"{instance.name}; --plugin accepts archive plugins only"
                )
            declared.append(instance)
        if not declared:
            raise RolloutUsageError(
                f"plugin '{plugin_id}' is not declared on any selected member "
                f"({', '.join(instance.name for instance in selected)})"
            )
        targets[plugin_id] = declared

    requests = [inspect_archive(plugin_id, path) for plugin_id, path in parsed]

    pins: dict[str, dict[str, PluginPin]] = {}
    changes: list[PinChange] = []
    for request in requests:
        for instance in targets[request.plugin_id]:
            old = next(item for item in instance.plugins if item.id == request.plugin_id)
            pins.setdefault(instance.name, {})[request.plugin_id] = PluginPin(
                request.artifact_path, request.sha256
            )
            changes.append(
                PinChange(
                    instance.name,
                    request.plugin_id,
                    old.artifact_path,
                    old.sha256,
                    _old_version(old),
                    request,
                )
            )

    inventory = current
    if pins:
        try:
            _overlay, inventory = plan_plugin_pins(layers, pins)
        except (RuntimePinsError, ValidationError, ValueError) as exc:
            raise RolloutUsageError(f"candidate pins are invalid: {exc}") from exc
    by_name = {instance.name: instance for instance in inventory.instances}
    return RolloutCandidate(
        config_path=layers.inventory_path,
        pins_path=layers.pins_path,
        current=current,
        inventory=inventory,
        selected=tuple(by_name[instance.name] for instance in selected),
        pin_changes=tuple(changes),
    )


# --- read-only plan -------------------------------------------------------------------------


@dataclass(frozen=True)
class MemberPlan:
    instance: InstanceSpec
    setup: SetupAction
    readiness: MemberReadiness
    plugins: MemberPluginPlan
    pin_changes: tuple[PinChange, ...]
    backup_enabled: bool

    @property
    def acts(self) -> bool:
        """Whether --execute may change anything for this member."""
        return bool(
            self.setup.action != "none"
            or self.plugins.installs
            or self.plugins.config_changed
            or self.pin_changes
        )

    @property
    def takes_backup(self) -> bool:
        return self.acts and self.backup_enabled


@dataclass(frozen=True)
class RolloutPlan:
    candidate: RolloutCandidate
    artifacts: tuple[ArtifactChange, ...]
    members: tuple[MemberPlan, ...]
    backup_dir: Path

    def restarts(self) -> list[str]:
        result: list[str] = []
        for member in self.members:
            name = member.instance.name
            if member.setup.action == "restart" or member.plugins.restart_expected:
                result.append(name)
            elif member.setup.action == "start":
                result.append(f"{name} (start)")
            elif member.plugins.restart_possible:
                result.append(f"{name} (if install)")
        return result


def plan_rollout(
    candidate: RolloutCandidate,
    probes: ReadinessProbes,
    template_root: Path,
    backup_dir: Path,
) -> RolloutPlan:
    """Read-only: rendered diff, unit state, readiness, ledger and plugin decisions."""
    selected = list(candidate.selected)
    artifacts = plan_deployment(candidate.inventory, selected, template_root)
    actions = plan_setup_actions(selected, artifacts, probes.unit_status)
    current = {instance.name: instance for instance in candidate.current.instances}
    members = []
    for instance, action in zip(selected, actions, strict=True):
        pin_changes = candidate.changes_for(instance.name)
        # "Readiness now" is measured against the committed pins.
        readiness = probe_member(current[instance.name], probes)
        # Plugin decisions compare the runtime with the candidate pins; that needs a second
        # read-only probe only when this member's pins change.
        plugins = plan_member_plugins(
            instance, probes, readiness=None if pin_changes else readiness
        )
        policy = instance.backup_policy
        members.append(
            MemberPlan(
                instance=instance,
                setup=action,
                readiness=readiness,
                plugins=plugins,
                pin_changes=pin_changes,
                backup_enabled=policy.enabled and policy.pre_mutation,
            )
        )
    return RolloutPlan(candidate, tuple(artifacts), tuple(members), backup_dir)


def _readiness_text(readiness: MemberReadiness) -> str:
    codes = ", ".join(reason.code for reason in readiness.reasons)
    return f"{readiness.state} ({codes})" if codes else readiness.state


def _member_backup_preview(plan: RolloutPlan, member: MemberPlan) -> str:
    if not member.acts:
        return "none (no change planned)"
    if not member.backup_enabled:
        return "none (backup_policy disabled or pre_mutation off)"
    return f"{plan.backup_dir}/{member.instance.name}-{TIMESTAMP_PLACEHOLDER}.tar.gz"


def overlay_backup_preview(plan: RolloutPlan) -> str | None:
    pins_path = plan.candidate.pins_path
    if pins_path is None or not plan.candidate.changed_pins:
        return None
    return f"{plan.backup_dir}/{pins_path.stem}-{TIMESTAMP_PLACEHOLDER}{pins_path.suffix}"


def preview_lines(plan: RolloutPlan) -> list[str]:
    """Deterministic per-member preview; the caller prints the rendered artifact diff."""
    lines: list[str] = []
    all_changes = {(item.member, item.plugin_id): item for item in plan.candidate.pin_changes}
    for member in plan.members:
        instance = member.instance
        lines.append(f"Member {instance.name}:")
        lines.append(f"  readiness now: {_readiness_text(member.readiness)}")
        lines.append(f"  setup: {member.setup.action} ({', '.join(member.setup.reasons)})")
        decisions = {item.plugin_id: item for item in member.plugins.decisions}
        for plugin in instance.plugins:
            decision = decisions[plugin.id]
            change = all_changes.get((instance.name, plugin.id))
            if change is not None and change.changed:
                old = f"{change.old_version or 'unreadable'} ({_prefix(change.old_sha256)})"
                new = f"{change.new.version} ({_prefix(change.new.sha256)})"
                lines.append(f"  plugin {plugin.id}: {old} -> {new}")
            else:
                version = decision.pinned_version or "unreadable"
                lines.append(
                    f"  plugin {plugin.id}: {version} ({_prefix(decision.digest)}) unchanged pin"
                )
            reasons = f" ({', '.join(decision.reasons)})" if decision.reasons else ""
            lines.append(f"    action: {decision.action}{reasons}")
        grants = "would change" if member.plugins.config_changed else "unchanged"
        lines.append(f"  grants: managed plugin config and agent grants {grants}")
        if instance.global_tool_allow:
            lines.append(f"    global tools: {', '.join(instance.global_tool_allow)}")
        for agent_id, tools in sorted(instance.agent_tool_allow.items()):
            lines.append(f"    agent {agent_id} optional tools: {', '.join(tools)}")
        lines.append(f"  backup: {_member_backup_preview(plan, member)}")
    overlay_backup = overlay_backup_preview(plan)
    if overlay_backup is not None:
        lines.append(f"Overlay backup: {overlay_backup}")
    lines.append(
        "Order with --execute: all backups first; then one member at a time: deploy, reload "
        "systemd, act and wait for readiness, sync plugins and verify, commit that member's "
        "pins; stop at the first failure."
    )
    lines.append(f"Members to restart: {', '.join(plan.restarts()) or 'none'}")
    return lines


# --- execute --------------------------------------------------------------------------------


@dataclass
class MemberRun:
    plan: MemberPlan
    status: RunResult = "not_run"
    setup: MemberOutcome | None = None
    rows: list[PluginSyncRow] = field(default_factory=list)
    committed: bool = False
    backups: list[Path] = field(default_factory=list)


@dataclass
class RolloutResult:
    runs: list[MemberRun]
    overlay_backup: Path | None = None
    failed_member: str | None = None
    failure: str | None = None

    @property
    def failed(self) -> bool:
        return self.failure is not None


def _take_backups(plan: RolloutPlan, result: RolloutResult, echo: Echo) -> bool:
    for run in result.runs:
        member = run.plan
        if not member.takes_backup:
            continue
        name = member.instance.name
        try:
            archive = backup_instance(member.instance, plan.backup_dir, execute=True)
        except (OSError, tarfile.TarError) as exc:
            # A failed backup aborts the rollout before any deploy (explicit failure result).
            result.failed_member = name
            result.failure = f"Backup failed for {name}: {exc}"
            return False
        run.backups.append(archive)
        echo(f"Created runtime backup for {name}: {archive}")
    pins_path = plan.candidate.pins_path
    if pins_path is not None and plan.candidate.changed_pins:
        try:
            result.overlay_backup = copy_overlay(pins_path, plan.backup_dir)
        except (OSError, RuntimePinsError) as exc:
            result.failure = f"Backup failed for the runtime pins overlay {pins_path}: {exc}"
            return False
        echo(f"Created runtime pins backup: {result.overlay_backup}")
    return True


def _deploy(
    run: MemberRun,
    changes: list[ArtifactChange],
    service: SupportsLifecycle,
    echo: Echo,
    on_result: Callable[[CommandResult], None],
) -> str | None:
    """Deploy this member's rendered changes and reload systemd; return a failure or None."""
    instance = run.plan.instance
    try:
        # Podman requires bind-mount sources to exist before the unit starts.
        instance.runtime_state_path.mkdir(parents=True, exist_ok=True)
        if is_browser_image(instance.image):
            ensure_browser_cache(instance)
        deployed = apply_artifacts(changes) if changes else []
    except (OSError, RuntimeError, ValueError) as exc:
        return f"deploy failed: {exc}"
    for destination in deployed:
        echo(f"Deployed {destination}")
    if not deployed:
        return None
    reload_result = service.daemon_reload(execute=True)
    on_result(reload_result)
    if reload_result.return_code != 0:
        return "systemd daemon-reload failed"
    return None


def _commit_pins(config_path: Path, run: MemberRun, echo: Echo) -> str | None:
    changes = run.plan.pin_changes
    if not changes:
        return None
    pins = {
        change.plugin_id: PluginPin(change.new.artifact_path, change.new.sha256)
        for change in changes
    }
    try:
        with warnings.catch_warnings():
            # Overlay permission warnings were already reported when the inventory was loaded.
            warnings.simplefilter("ignore", RuntimePinsWarning)
            backup = set_plugin_pins(config_path, run.plan.instance.name, pins)
    except (OSError, ValueError) as exc:
        return f"pin commit failed: {exc}"
    run.committed = True
    echo(f"Committed runtime pins for {run.plan.instance.name}; previous pins: {backup}")
    return None


def execute_rollout(
    plan: RolloutPlan,
    service: SupportsLifecycle,
    probes: ReadinessProbes,
    *,
    echo: Echo = _silent,
    on_result: Callable[[CommandResult], None] = _silent,
) -> RolloutResult:
    """Backups first, then member by member: deploy → act → plugin sync → verify → commit."""
    result = RolloutResult([MemberRun(member) for member in plan.members])
    if not _take_backups(plan, result, echo):
        return result
    shared = [change for change in plan.artifacts if change.role == "shared_network"]
    for run in result.runs:
        name = run.plan.instance.name
        changes = shared + [
            change
            for change in plan.artifacts
            if change.member == name and change.role != "shared_network"
        ]
        shared = []
        failure = _run_member(plan, run, changes, service, probes, echo, on_result)
        if failure is not None:
            run.status = "failed"
            result.failed_member = name
            result.failure = failure
            break
        run.status = "ok"
    return result


def _run_member(
    plan: RolloutPlan,
    run: MemberRun,
    changes: list[ArtifactChange],
    service: SupportsLifecycle,
    probes: ReadinessProbes,
    echo: Echo,
    on_result: Callable[[CommandResult], None],
) -> str | None:
    instance = run.plan.instance
    failure = _deploy(run, changes, service, echo, on_result)
    if failure is not None:
        return failure
    outcome = run_setup_actions(
        [instance], [run.plan.setup], service, probes, echo=echo, on_result=on_result
    )[0]
    run.setup = outcome
    if outcome.result != "ok":
        return f"{outcome.action} failed ({', '.join(outcome.reasons) or 'no detail'})"
    # Architecture-Ref: clawake/docs/architecture.md#lifecycle-contracts
    # The doc says plugin installation remains an explicit `sync-plugins` action; ADR-003
    # item 5 adds `rollout`, which runs the same sync after the setup action.
    rows, sync_failed = run_plugin_sync([instance], service, probes, echo=echo, on_result=on_result)
    run.rows = rows
    if sync_failed:
        return "plugin sync or verification failed"
    # ADR-004: pins are committed only after this member verified successfully.
    return _commit_pins(plan.candidate.config_path, run, echo)


# --- output ---------------------------------------------------------------------------------

SUMMARY_HEADERS = (
    "member",
    "setup",
    "plugin",
    "action",
    "version",
    "digest",
    "result",
    "readiness",
    "ready_s",
    "pins",
)


def _summary_row(run: MemberRun, plugin: PluginSpec | None) -> tuple[object, ...]:
    member = run.plan
    plugin_id = plugin.id if plugin else "-"
    sync = next((row for row in run.rows if row.plugin_id == plugin_id), None)
    change = next((item for item in member.pin_changes if item.plugin_id == plugin_id), None)
    decision = next(
        (item for item in member.plugins.decisions if item.plugin_id == plugin_id), None
    )
    if sync is not None:
        action: str | None = sync.action
    elif plugin is None:
        action = None
    else:
        action = "not_run"
    if change is not None:
        version: str | None = f"{change.old_version or '?'}->{change.new.version}"
        digest = _prefix(change.new.sha256)
    else:
        version = sync.version if sync else (decision.pinned_version if decision else None)
        digest = sync.digest if sync else _prefix(decision.digest if decision else None)
    readiness = sync.readiness if sync and sync.readiness else None
    seconds = sync.seconds if sync and sync.seconds is not None else None
    if run.setup is not None:
        readiness = readiness or run.setup.readiness
        seconds = seconds if seconds is not None else run.setup.seconds
    if change is None:
        pins = None
    else:
        pins = "yes" if run.committed else "no"
    return (
        member.instance.name,
        member.setup.action,
        plugin_id,
        action,
        version,
        digest,
        run.status,
        readiness,
        seconds,
        pins,
    )


def summary_lines(result: RolloutResult) -> list[str]:
    rows = []
    for run in result.runs:
        plugins: list[PluginSpec | None] = list(run.plan.instance.plugins) or [None]
        rows.extend(_summary_row(run, plugin) for plugin in plugins)
    return render_table(SUMMARY_HEADERS, rows)


def failure_hint(plan: RolloutPlan, result: RolloutResult) -> list[str]:
    """Explain where the rollout stopped and how to return to the committed pins."""
    member = result.failed_member
    if member is None:
        return [f"Rollout stopped: {result.failure}", "Nothing was deployed."]
    run = next(item for item in result.runs if item.plan.instance.name == member)
    if run.status == "not_run":
        return [f"Rollout stopped: {result.failure}", "Nothing was deployed."]
    backups = [str(path) for path in run.backups]
    if result.overlay_backup is not None:
        backups.append(str(result.overlay_backup))
    return [
        f"Rollout stopped at {member}: {result.failure}. Later members were not touched.",
        f"The deployed state of {member} now differs from the committed runtime pins.",
        f"Backups: {', '.join(backups) or 'none taken'}",
        "To return to the committed pins, run: "
        f"clawake rollout -c {plan.candidate.config_path} -m {member} --execute",
    ]
