"""Per-member lifecycle orchestration for v1 members (ADR-003, decision items 3, 4 and 6).

Shared by `setup`, `sync-plugins` and, later, `rollout`:

- `plan_setup_actions` / `run_setup_actions`: restart changed members, start unchanged
  inactive members, leave unchanged active members alone; act on one member at a time,
  wait for readiness, and stop at the first failure.
- `plan_member_plugins` / `run_plugin_sync`: wait for readiness, skip packages that are
  already installed at the pinned digest and version, install the rest, record the
  installed archive digest, restart only on change, verify, and stop at the first failure.

Planning is read-only: it reads rendered artifacts, the unit state, the plugin ledger and
read-only readiness probes. Every host call goes through injected seams
(`SupportsLifecycle`, `ReadinessProbes`, `install`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from clawake.config import InstanceSpec, PluginSpec
from clawake.services.deployment import ArtifactChange
from clawake.services.gateway_config import (
    InstalledPlugin,
    ensure_plugin_runtime_config,
    plan_plugin_runtime_config,
    read_installed_plugins,
    record_installed_plugin,
)
from clawake.services.plugins import (
    PluginSyncResult,
    npm_inspect_command,
    npm_install_mismatch,
    sync_plugin,
    verify_plugin,
)
from clawake.services.readiness import (
    PROBE_TIMEOUT_SECONDS,
    SETTLED_STATES,
    MemberReadiness,
    ReadinessProbes,
    classify_unit,
    pinned_plugin_version,
    probe_member,
    wait_members_ready,
)
from clawake.services.systemd import CommandResult, SystemdService

Echo = Callable[..., None]
SetupActionName = Literal["restart", "start", "none"]
PluginActionName = Literal["skip", "install", "install (unverified)"]
DIGEST_PREFIX_LENGTH = 19  # "sha256:" plus 12 hex digits


class SupportsLifecycle(Protocol):
    def restart(self, instance_name: str, execute: bool = False) -> CommandResult: ...

    def start(self, instance_name: str, execute: bool = False) -> CommandResult: ...


def _silent(*_args: object, **_kwargs: object) -> None:
    return None


def _read_unit(unit_status: Callable[[str], CommandResult], name: str) -> CommandResult:
    """Read the unit state; a missing systemctl becomes an explicit unknown-state result."""
    try:
        return unit_status(name)
    except OSError:
        command = ["systemctl", "--user", "status", SystemdService.unit_name(name)]
        return CommandResult(command=command, return_code=127, stdout="", stderr="")


# --- setup ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SetupAction:
    member: str
    action: SetupActionName
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MemberOutcome:
    member: str
    action: str
    result: Literal["ok", "failed", "not_run"]
    readiness: str | None = None
    seconds: float | None = None
    reasons: tuple[str, ...] = ()


def change_reasons(member: str, changes: Sequence[ArtifactChange]) -> tuple[str, ...]:
    """Artifact file names, plus `runtime_config` for managed OpenClaw config or ledger."""
    reasons: list[str] = []
    for change in changes:
        if change.member != member or change.role == "shared_network":
            continue
        reason = "runtime_config" if change.kind == "runtime" else change.destination.name
        if reason not in reasons:
            reasons.append(reason)
    return tuple(reasons)


def plan_setup_actions(
    instances: Sequence[InstanceSpec],
    changes: Sequence[ArtifactChange],
    unit_status: Callable[[str], CommandResult],
) -> list[SetupAction]:
    """Changed → restart; unchanged and not active → start; unchanged and active → none.

    Only unchanged members have their unit state read (`systemctl --user status`, read-only).
    """
    actions: list[SetupAction] = []
    for instance in instances:
        reasons = change_reasons(instance.name, changes)
        if reasons:
            actions.append(SetupAction(instance.name, "restart", reasons))
            continue
        state, reason = classify_unit(_read_unit(unit_status, instance.name))
        if state is None:
            actions.append(SetupAction(instance.name, "none", ("unchanged", "unit_active")))
        else:
            code = reason.code if reason else "unit_not_active"
            actions.append(SetupAction(instance.name, "start", ("unchanged", code)))
    return actions


def _settled_or_failure(result: MemberReadiness) -> tuple[str, ...]:
    if result.state in SETTLED_STATES:
        return ()
    return tuple(reason.code for reason in result.reasons) or (result.state,)


def run_setup_actions(
    instances: Sequence[InstanceSpec],
    actions: Sequence[SetupAction],
    service: SupportsLifecycle,
    probes: ReadinessProbes,
    *,
    echo: Echo = _silent,
    on_result: Callable[[CommandResult], None] = _silent,
) -> list[MemberOutcome]:
    """Act on one member at a time and wait for it; stop at the first failure."""
    by_name = {instance.name: instance for instance in instances}
    outcomes: list[MemberOutcome] = []
    failed = False
    for action in actions:
        if failed:
            outcomes.append(MemberOutcome(action.member, action.action, "not_run"))
            continue
        if action.action == "none":
            outcomes.append(MemberOutcome(action.member, "none", "ok"))
            continue
        instance = by_name[action.member]
        started = probes.clock()
        operation = service.restart if action.action == "restart" else service.start
        result = operation(instance.name, execute=True)
        on_result(result)
        if result.return_code != 0:
            outcomes.append(
                MemberOutcome(
                    instance.name, action.action, "failed", reasons=(f"{action.action}_failed",)
                )
            )
            failed = True
            continue
        echo(
            f"Waiting for {instance.name} to become ready "
            f"(start window {instance.readiness.start_window_seconds}s)..."
        )
        readiness = wait_members_ready([instance], probes=probes)[0]
        seconds = probes.clock() - started
        problems = _settled_or_failure(readiness)
        if problems:
            outcomes.append(
                MemberOutcome(
                    instance.name, action.action, "failed", readiness.state, seconds, problems
                )
            )
            failed = True
            continue
        if readiness.state == "degraded":
            codes = ", ".join(reason.code for reason in readiness.reasons)
            echo(f"WARNING: {instance.name} is degraded: {codes}", err=True)
        outcomes.append(
            MemberOutcome(
                instance.name,
                action.action,
                "ok",
                readiness.state,
                seconds,
                tuple(reason.code for reason in readiness.reasons),
            )
        )
    return outcomes


# --- sync-plugins -----------------------------------------------------------------------


@dataclass(frozen=True)
class PluginDecision:
    plugin_id: str
    action: PluginActionName
    reasons: tuple[str, ...]
    pinned_version: str | None
    digest: str


@dataclass(frozen=True)
class MemberPluginPlan:
    member: str
    readiness: MemberReadiness | None
    decisions: tuple[PluginDecision, ...]
    config_changed: bool

    @property
    def installs(self) -> tuple[PluginDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.action != "skip")

    @property
    def restart_expected(self) -> bool:
        return self.config_changed or any(
            decision.action == "install" for decision in self.decisions
        )

    @property
    def restart_possible(self) -> bool:
        return self.restart_expected or bool(self.installs)


@dataclass(frozen=True)
class PluginSyncRow:
    member: str
    action: str
    plugin_id: str
    version: str | None
    digest: str | None
    readiness: str | None
    seconds: float | None


def _readiness_problems(plugin: PluginSpec, readiness: MemberReadiness) -> list[str]:
    row = next((item for item in readiness.plugins if item.id == plugin.id), None)
    if row is None or row.installed_version is None:
        return ["plugin_missing"]
    problems: list[str] = []
    if row.pinned_version is None or row.installed_version != row.pinned_version:
        problems.append("version_mismatch")
    if plugin.enabled and row.enabled is not True:
        problems.append("plugin_disabled")
    if plugin.enabled and row.status != "loaded":
        problems.append("plugin_not_loaded")
    return problems


def decide_plugin(
    instance: InstanceSpec,
    plugin: PluginSpec,
    readiness: MemberReadiness | None,
    ledger: dict[str, InstalledPlugin],
    inspect: Callable[[list[str], float], CommandResult],
) -> PluginDecision:
    """Skip only if the ledger digest equals the pin and the probe shows the pin loaded.

    npm pins use `plugins inspect --runtime --json` (resolved spec + integrity) instead of
    the ledger. A member that is not ready yields `install (unverified)` unless an install
    is already certain from the ledger.
    """
    digest = verify_plugin(plugin)
    try:
        pinned_version: str | None = pinned_plugin_version(plugin)
    except ValueError:
        pinned_version = None
    reasons: list[str] = []
    if plugin.source_type == "archive":
        entry = ledger.get(plugin.id)
        if entry is None:
            reasons.append("ledger_missing")
        elif entry.sha256 != plugin.sha256:
            reasons.append("digest_changed")
    if readiness is None or readiness.state not in SETTLED_STATES:
        # Without a settled probe the skip rule cannot be verified; only the ledger can
        # already prove that an install is needed.
        certain = bool(reasons)
        reasons.append("member_not_ready")
        action: PluginActionName = "install" if certain else "install (unverified)"
        return PluginDecision(plugin.id, action, tuple(reasons), pinned_version, digest)
    if plugin.source_type == "npm":
        result = inspect(npm_inspect_command(instance, plugin), PROBE_TIMEOUT_SECONDS)
        mismatch = npm_install_mismatch(plugin, result.return_code, result.stdout)
        if mismatch:
            reasons.append(mismatch)
    for problem in _readiness_problems(plugin, readiness):
        if problem not in reasons:
            reasons.append(problem)
    action = "install" if reasons else "skip"
    return PluginDecision(plugin.id, action, tuple(reasons), pinned_version, digest)


def plan_member_plugins(
    instance: InstanceSpec,
    probes: ReadinessProbes,
    *,
    readiness: MemberReadiness | None = None,
) -> MemberPluginPlan:
    """Read-only plan for one member: probe (once, if needed), ledger, config diff."""
    if instance.plugins and readiness is None:
        readiness = probe_member(instance, probes)
    ledger = read_installed_plugins(instance)
    decisions = tuple(
        decide_plugin(instance, plugin, readiness, ledger, probes.run)
        for plugin in instance.plugins
    )
    config_changed = plan_plugin_runtime_config(instance).config_changed
    return MemberPluginPlan(instance.name, readiness, decisions, config_changed)


def verify_pinned_plugins(instance: InstanceSpec, readiness: MemberReadiness) -> list[str]:
    """Post-restart check: every pinned plugin is listed at its pinned version and loaded."""
    problems: list[str] = []
    for plugin in instance.plugins:
        problems.extend(
            f"{plugin.id}:{problem}" for problem in _readiness_problems(plugin, readiness)
        )
    return problems


def _digest_prefix(digest: str | None) -> str | None:
    return digest[:DIGEST_PREFIX_LENGTH] if digest else None


def _default_install(instance: InstanceSpec, plugin: PluginSpec) -> PluginSyncResult:
    return sync_plugin(instance, plugin, execute=True)


@dataclass
class _MemberRun:
    instance: InstanceSpec
    plan: MemberPluginPlan | None = None
    action_by_plugin: dict[str, str] = field(default_factory=dict)
    readiness: str | None = None
    seconds: float | None = None
    member_action: str = "none"

    def rows(self) -> list[PluginSyncRow]:
        decisions = {item.plugin_id: item for item in self.plan.decisions} if self.plan else {}
        if not self.instance.plugins:
            return [
                PluginSyncRow(
                    self.instance.name,
                    self.member_action,
                    "-",
                    None,
                    None,
                    self.readiness,
                    self.seconds,
                )
            ]
        rows = []
        for plugin in self.instance.plugins:
            decision = decisions.get(plugin.id)
            rows.append(
                PluginSyncRow(
                    self.instance.name,
                    self.action_by_plugin.get(plugin.id, self.member_action),
                    plugin.id,
                    decision.pinned_version if decision else None,
                    _digest_prefix(decision.digest if decision else None),
                    self.readiness,
                    self.seconds,
                )
            )
        return rows


def run_plugin_sync(
    instances: Sequence[InstanceSpec],
    service: SupportsLifecycle,
    probes: ReadinessProbes,
    *,
    install: Callable[[InstanceSpec, PluginSpec], PluginSyncResult] = _default_install,
    echo: Echo = _silent,
    on_result: Callable[[CommandResult], None] = _silent,
) -> tuple[list[PluginSyncRow], bool]:
    """Sync members one at a time; return the summary rows and whether a member failed.

    Per member: wait for ready/degraded → install non-skipped plugins → record the archive
    digest → apply managed config/grants → restart only on change → wait → verify pins.
    Members after the first failure are reported as `not_run` and never touched.
    """
    rows: list[PluginSyncRow] = []
    failed = False
    for instance in instances:
        run = _MemberRun(instance)
        if failed:
            run.member_action = "not_run"
            rows.extend(run.rows())
            continue
        failed = not _sync_member(run, service, probes, install, echo, on_result)
        rows.extend(run.rows())
    return rows, failed


def _fail(run: _MemberRun, message: str, echo: Echo, plugin_id: str | None = None) -> bool:
    echo(f"{run.instance.name}: {message}", err=True)
    run.member_action = "failed"
    if plugin_id is not None:
        run.action_by_plugin[plugin_id] = "failed"
    else:
        run.action_by_plugin.clear()
    return False


def _sync_member(
    run: _MemberRun,
    service: SupportsLifecycle,
    probes: ReadinessProbes,
    install: Callable[[InstanceSpec, PluginSpec], PluginSyncResult],
    echo: Echo,
    on_result: Callable[[CommandResult], None],
) -> bool:
    instance = run.instance
    readiness: MemberReadiness | None = None
    if instance.plugins:
        readiness = probe_member(instance, probes)
        if readiness.state == "stopped":
            run.readiness = readiness.state
            return _fail(run, "member is stopped; run setup --execute first", echo)
        if readiness.state not in SETTLED_STATES:
            echo(
                f"Waiting for {instance.name} to become ready before installing "
                f"(start window {instance.readiness.start_window_seconds}s)..."
            )
            readiness = wait_members_ready([instance], probes=probes)[0]
        run.readiness = readiness.state
        if readiness.state not in SETTLED_STATES:
            codes = ", ".join(_settled_or_failure(readiness))
            return _fail(run, f"not ready, nothing installed ({codes})", echo)
    try:
        plan = plan_member_plugins(instance, probes, readiness=readiness)
    except (OSError, ValueError) as exc:
        return _fail(run, str(exc), echo)
    run.plan = plan
    for decision in plan.decisions:
        run.action_by_plugin[decision.plugin_id] = decision.action
    plugins = {plugin.id: plugin for plugin in instance.plugins}
    installed_any = False
    for decision in plan.installs:
        plugin = plugins[decision.plugin_id]
        try:
            result = install(instance, plugin)
        except (OSError, RuntimeError, ValueError) as exc:
            return _fail(run, str(exc), echo, plugin.id)
        for command in result.commands:
            echo(f"$ {' '.join(command)}")
        echo(f"Installed verified plugin {plugin.id} on {instance.name}")
        installed_any = True
        run.action_by_plugin[plugin.id] = "install"
        if plugin.source_type == "archive":
            assert plugin.sha256 is not None
            try:
                record_installed_plugin(instance, plugin.id, plugin.sha256, decision.pinned_version)
            except (OSError, ValueError) as exc:
                return _fail(run, f"cannot record installed plugin: {exc}", echo, plugin.id)
    try:
        config_path, config_changed = ensure_plugin_runtime_config(instance)
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(run, str(exc), echo)
    if config_changed:
        echo(f"Updated managed plugin config and agent ACLs in {config_path}")
    if not (installed_any or config_changed):
        run.member_action = "skip" if instance.plugins else "none"
        return True
    run.member_action = "config"
    started = probes.clock()
    restart = service.restart(instance.name, execute=True)
    on_result(restart)
    if restart.return_code != 0:
        return _fail(run, "restart failed", echo)
    echo(
        f"Waiting for {instance.name} to become ready "
        f"(start window {instance.readiness.start_window_seconds}s)..."
    )
    after = wait_members_ready([instance], probes=probes)[0]
    run.readiness = after.state
    run.seconds = probes.clock() - started
    if after.state not in SETTLED_STATES:
        codes = ", ".join(_settled_or_failure(after))
        return _fail(run, f"not ready after restart ({codes})", echo)
    problems = verify_pinned_plugins(instance, after)
    if problems:
        return _fail(run, f"plugin verification failed: {', '.join(problems)}", echo)
    if after.state == "degraded":
        codes = ", ".join(reason.code for reason in after.reasons)
        echo(f"WARNING: {instance.name} is degraded: {codes}", err=True)
    return True


# --- summaries --------------------------------------------------------------------------


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.0f}"
    return str(value)


def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    """Deterministic fixed-width table; `-` marks an empty cell."""
    cells = [list(headers), *[[_cell(value) for value in row] for row in rows]]
    widths = [max(len(row[index]) for row in cells) for index in range(len(headers))]
    return [
        (
            "  " + "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True))
        ).rstrip()
        for row in cells
    ]


def setup_summary(outcomes: Sequence[MemberOutcome]) -> list[str]:
    return render_table(
        ("member", "action", "result", "readiness", "ready_s", "reasons"),
        [
            (
                item.member,
                item.action,
                item.result,
                item.readiness,
                item.seconds,
                ",".join(item.reasons) or None,
            )
            for item in outcomes
        ],
    )


def plugin_sync_summary(rows: Sequence[PluginSyncRow]) -> list[str]:
    return render_table(
        ("member", "action", "plugin", "version", "digest", "readiness", "ready_s"),
        [
            (
                row.member,
                row.action,
                row.plugin_id,
                row.version,
                row.digest,
                row.readiness,
                row.seconds,
            )
            for row in rows
        ],
    )
