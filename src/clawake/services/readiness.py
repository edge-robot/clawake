"""Shared member readiness model for v1 members (ADR-003, decision items 1, 2 and 6).

States, evaluated in this order:

- ``stopped``: the unit is inactive, dead or unknown to systemd.
- ``failed``: the unit failed, or a bounded wait ran out before ``ready``/``degraded``.
- ``starting``: the unit is active (or activating) and ``/readyz`` is not 200 yet.
- ``degraded``: ``/readyz`` is 200, but a pinned plugin or an opted-in channel check does not
  match, or could not be parsed. Unknown never counts as healthy.
- ``ready``: none of the above.

Every probe is read-only and secret-free: one HTTP GET to the published gateway port, plus
``podman exec <container> openclaw plugins list --json`` and, opt-in per member,
``openclaw channels status --json`` (never ``--probe``). Raw command output is parsed but
never stored or printed; reasons only carry sanitized identifiers and versions.
"""

from __future__ import annotations

import json
import re
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from clawake.config import InstanceSpec, PluginSpec
from clawake.services.runtime_upgrade import _gateway_host_url
from clawake.services.systemd import CommandResult, SystemdService

ReadinessState = Literal["stopped", "starting", "ready", "degraded", "failed"]
SETTLED_STATES: frozenset[str] = frozenset({"ready", "degraded"})

PROBE_TIMEOUT_SECONDS = 10.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_PACKAGE_JSON_LIMIT_BYTES = 1024 * 1024
# OpenClaw documents "blocked" as a non-running lifecycle. A healthy 2026.9.9 member reports
# "ready" (live sample, docs/evidence/2026-10-19-member-readiness-status.md); "running" is kept.
_UP_LIFECYCLES = frozenset({"ready", "running"})
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,47}")
_ACTIVE_LINE = re.compile(r"^\s*Active:\s*(?P<state>[a-z-]+)(?:\s+\((?P<sub>[^)]*)\))?", re.M)


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class PluginReadiness:
    id: str
    pinned_version: str | None
    installed_version: str | None = None
    status: str | None = None
    enabled: bool | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pinned_version": self.pinned_version,
            "installed_version": self.installed_version,
            "status": self.status,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class MemberReadiness:
    member: str
    state: ReadinessState
    reasons: tuple[Reason, ...] = ()
    plugins: tuple[PluginReadiness, ...] = ()
    # The systemctl status result the state was derived from; callers may show it.
    unit_result: CommandResult | None = field(default=None, compare=False, repr=False)

    def to_json(self) -> dict[str, object]:
        return {
            "member": self.member,
            "readiness": self.state,
            "reasons": [reason.to_json() for reason in self.reasons],
            "plugins": [plugin.to_json() for plugin in self.plugins],
        }


@dataclass(frozen=True)
class HttpOutcome:
    """Result of one readiness GET: an HTTP status, or a coarse transport error class."""

    status: int | None = None
    error: Literal["refused", "timeout", "error"] | None = None


@dataclass(frozen=True)
class ReadinessProbes:
    """Injectable seams for every host interaction of the readiness probe."""

    unit_status: Callable[[str], CommandResult]
    run: Callable[[list[str], float], CommandResult]
    http_get: Callable[[str, float], HttpOutcome]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def run_command(argv: list[str], timeout: float) -> CommandResult:
    """Run one read-only probe command with a hard timeout; never raises on failure."""
    try:
        process = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CommandResult(command=argv, return_code=124, stdout="", stderr="timeout")
    except OSError as exc:
        return CommandResult(command=argv, return_code=127, stdout="", stderr=type(exc).__name__)
    return CommandResult(
        command=argv,
        return_code=process.returncode,
        stdout=process.stdout,
        stderr=process.stderr,
    )


def http_get(url: str, timeout: float) -> HttpOutcome:
    """GET a loopback URL without proxies and report only the status or an error class."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return HttpOutcome(status=response.status)
    except urllib.error.HTTPError as exc:
        return HttpOutcome(status=exc.code)
    except urllib.error.URLError as exc:
        return HttpOutcome(error=_transport_error(exc.reason))
    except (TimeoutError, OSError) as exc:
        return HttpOutcome(error=_transport_error(exc))


def _transport_error(exc: object) -> Literal["refused", "timeout", "error"]:
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "error"


def default_probes(service: SystemdService | None = None) -> ReadinessProbes:
    systemd = service or SystemdService()
    return ReadinessProbes(
        unit_status=lambda name: systemd.status(name, execute=True),
        run=run_command,
        http_get=http_get,
    )


def _safe(value: object) -> str:
    """Return a printable identifier or a redaction marker; never echo free text."""
    if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
        return value
    return "<redacted>"


def pinned_plugin_version(plugin: PluginSpec) -> str:
    """Version of the pinned package: npm spec suffix or package/package.json in the archive."""
    if plugin.source_type == "npm":
        assert plugin.npm_spec is not None
        return plugin.npm_spec.rsplit("@", 1)[1]
    assert plugin.artifact_path is not None
    try:
        with tarfile.open(Path(plugin.artifact_path), "r:gz") as archive:
            member = archive.getmember("package/package.json")
            if not member.isfile() or member.size > _PACKAGE_JSON_LIMIT_BYTES:
                raise ValueError("package/package.json is not a regular small file")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("package/package.json cannot be read")
            payload = json.loads(handle.read())
    except (OSError, KeyError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the pinned archive of plugin '{plugin.id}'") from exc
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError(f"pinned archive of plugin '{plugin.id}' has no version")
    return version


def _pinned_or_none(plugin: PluginSpec) -> str | None:
    try:
        return pinned_plugin_version(plugin)
    except ValueError:
        return None


def classify_unit(result: CommandResult) -> tuple[ReadinessState | None, Reason | None]:
    """Public view of the unit mapping: None means the unit is active (or reloading)."""
    return _unit_state(result)


def _unit_state(result: CommandResult) -> tuple[ReadinessState | None, Reason | None]:
    """Map `systemctl --user status` to stopped/failed/starting, or None for active units."""
    match = _ACTIVE_LINE.search(result.stdout or "")
    if match:
        state = match.group("state")
        sub = match.group("sub") or ""
        if state in {"active", "reloading"}:
            return None, None
        if state == "activating" and sub.startswith("auto-restart"):
            return "failed", Reason("unit_auto_restart", "unit exited; systemd restart pending")
        if state == "activating":
            return "starting", Reason("unit_activating", "unit is activating")
        if state in {"inactive", "deactivating"}:
            return "stopped", Reason("unit_inactive", f"unit is {state}")
        if state == "failed":
            return "failed", Reason("unit_failed", "unit is in failed state")
        return "failed", Reason("unit_state_unknown", f"unit state '{_safe(state)}'")
    # LSB exit codes of systemctl status: 0 running, 3 not running, 4 unknown unit.
    if result.return_code == 0:
        return None, None
    if result.return_code == 3:
        return "stopped", Reason("unit_inactive", "unit is not running")
    if result.return_code == 4:
        return "stopped", Reason("unit_not_found", "unit is not loaded")
    return "failed", Reason("unit_state_unknown", f"systemctl status exit {result.return_code}")


def _readyz_reason(outcome: HttpOutcome, path: str) -> Reason | None:
    if outcome.status == 200:
        return None
    if outcome.status is not None:
        return Reason("readyz_not_ready", f"GET {path} returned HTTP {outcome.status}")
    if outcome.error == "timeout":
        return Reason("readyz_timeout", f"GET {path} timed out")
    if outcome.error == "refused":
        return Reason("readyz_unreachable", f"GET {path}: connection refused")
    return Reason("readyz_unreachable", f"GET {path}: transport error")


def _parse_json(stdout: str) -> object:
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def evaluate_plugins(
    pinned: Sequence[PluginSpec], result: CommandResult
) -> tuple[list[PluginReadiness], list[Reason]]:
    """Compare `openclaw plugins list --json` with the inventory pins."""
    reasons: list[Reason] = []
    pinned_versions: dict[str, str | None] = {}
    for plugin in pinned:
        try:
            pinned_versions[plugin.id] = pinned_plugin_version(plugin)
        except ValueError:
            pinned_versions[plugin.id] = None
            reasons.append(
                Reason("plugin_pin_unreadable", f"{plugin.id}: pinned version unreadable")
            )

    def unknown() -> list[PluginReadiness]:
        return [PluginReadiness(plugin.id, pinned_versions[plugin.id]) for plugin in pinned]

    if result.return_code != 0:
        reasons.append(
            Reason("plugins_list_failed", f"plugins list exited with {result.return_code}")
        )
        return unknown(), reasons
    payload = _parse_json(result.stdout)
    entries = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        reasons.append(Reason("plugins_unparseable", "plugins list JSON has no plugins array"))
        return unknown(), reasons
    installed: dict[str, dict[str, object]] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            installed.setdefault(entry["id"], entry)

    rows: list[PluginReadiness] = []
    for plugin in pinned:
        pinned_version = pinned_versions[plugin.id]
        entry = installed.get(plugin.id)
        if entry is None:
            rows.append(PluginReadiness(plugin.id, pinned_version))
            reasons.append(Reason("plugin_missing", f"{plugin.id}: not installed"))
            continue
        version, status, enabled = entry.get("version"), entry.get("status"), entry.get("enabled")
        if not (isinstance(version, str) and isinstance(status, str) and isinstance(enabled, bool)):
            rows.append(PluginReadiness(plugin.id, pinned_version))
            reasons.append(Reason("plugins_unparseable", f"{plugin.id}: unexpected field types"))
            continue
        rows.append(
            PluginReadiness(plugin.id, pinned_version, _safe(version), _safe(status), enabled)
        )
        if plugin.enabled and not enabled:
            reasons.append(Reason("plugin_disabled", f"{plugin.id}: installed but disabled"))
        if plugin.enabled and status != "loaded":
            reasons.append(Reason("plugin_not_loaded", f"{plugin.id}: status {_safe(status)}"))
        if pinned_version is not None and version != pinned_version:
            reasons.append(
                Reason(
                    "plugin_version_mismatch",
                    f"{plugin.id}: installed {_safe(version)}, pinned {_safe(pinned_version)}",
                )
            )
    return rows, reasons


def _channel_accounts(payload: object) -> list[tuple[str, object]] | None:
    """Accept `channelAccounts: {channel: [account]}` or `channels: {channel: {accounts}}`."""
    if not isinstance(payload, dict):
        return None
    container = payload.get("channelAccounts")
    if isinstance(container, dict):
        accounts: list[tuple[str, object]] = []
        for channel, items in container.items():
            if not isinstance(items, list):
                return None
            accounts.extend((str(channel), item) for item in items)
        return accounts
    container = payload.get("channels")
    if isinstance(container, dict) and all(
        isinstance(value, dict) and isinstance(value.get("accounts"), list)
        for value in container.values()
    ):
        return [
            (str(channel), item)
            for channel, value in container.items()
            for item in value["accounts"]
        ]
    return None


def evaluate_channels(result: CommandResult) -> list[Reason]:
    """Check recorded channel lifecycle from `openclaw channels status --json` (no --probe)."""
    if result.return_code != 0:
        return [Reason("channels_status_failed", f"channels status exited {result.return_code}")]
    payload = _parse_json(result.stdout)
    if isinstance(payload, dict) and payload.get("status") == "starting":
        phase = _safe(payload.get("startupPhase"))
        return [Reason("channels_starting", f"gateway startup phase {phase}")]
    accounts = _channel_accounts(payload)
    if accounts is None:
        return [Reason("channels_unparseable", "channels status JSON has no account list")]
    if not accounts:
        return [Reason("channels_none_reported", "no channel accounts reported")]
    reasons: list[Reason] = []
    for channel, account in accounts:
        if not isinstance(account, dict):
            reasons.append(
                Reason("channels_unparseable", f"{_safe(channel)}: account not an object")
            )
            continue
        label = f"{_safe(channel)}/{_safe(account.get('accountId', 'default'))}"
        running = account.get("running")
        lifecycle = account.get("lifecycle")
        if not isinstance(running, bool) and not isinstance(lifecycle, str):
            reasons.append(Reason("channels_unparseable", f"{label}: no running or lifecycle"))
            continue
        # Up means: running is true, connected is not false, lifecycle is ready/running.
        if running is not True:
            state = "false" if running is False else "missing"
            reasons.append(Reason("channel_not_running", f"{label}: running={state}"))
        if account.get("connected") is False:
            reasons.append(Reason("channel_not_connected", f"{label}: connected=false"))
        if not isinstance(lifecycle, str):
            reasons.append(Reason("channel_lifecycle", f"{label}: lifecycle missing"))
        elif lifecycle not in _UP_LIFECYCLES:
            reasons.append(Reason("channel_lifecycle", f"{label}: lifecycle {_safe(lifecycle)}"))
    return reasons


def probe_member(
    instance: InstanceSpec,
    probes: ReadinessProbes | None = None,
    *,
    deadline: float | None = None,
    unit_result: CommandResult | None = None,
) -> MemberReadiness:
    """Probe one member once. `deadline` (probes.clock time) caps every probe timeout."""
    seams = probes or default_probes()
    pinned_rows = tuple(
        PluginReadiness(plugin.id, _pinned_or_none(plugin)) for plugin in instance.plugins
    )

    def budget(limit: float) -> float:
        cap = min(limit, PROBE_TIMEOUT_SECONDS)
        if deadline is None:
            return cap
        return min(cap, deadline - seams.clock())

    unit = unit_result if unit_result is not None else seams.unit_status(instance.name)
    unit_state, unit_reason = _unit_state(unit)
    if unit_state is not None:
        reasons = (unit_reason,) if unit_reason else ()
        return MemberReadiness(instance.name, unit_state, reasons, pinned_rows, unit)

    path = "/" + instance.health.path.lstrip("/")
    url = _gateway_host_url(instance).rstrip("/") + path
    timeout = budget(float(instance.health.timeout_seconds))
    if timeout <= 0:
        reason = Reason("readyz_timeout", f"GET {path} skipped: start window exhausted")
        return MemberReadiness(instance.name, "starting", (reason,), pinned_rows, unit)
    readyz = _readyz_reason(seams.http_get(url, timeout), path)
    if readyz is not None:
        return MemberReadiness(instance.name, "starting", (readyz,), pinned_rows, unit)

    reasons: list[Reason] = []
    plugin_rows: tuple[PluginReadiness, ...] = ()
    exec_prefix = ["podman", "exec", instance.container_name, "openclaw"]
    if instance.plugins:
        timeout = budget(PROBE_TIMEOUT_SECONDS)
        if timeout <= 0:
            reasons.append(Reason("plugins_list_failed", "skipped: start window exhausted"))
            plugin_rows = pinned_rows
        else:
            listed = seams.run([*exec_prefix, "plugins", "list", "--json"], timeout)
            rows, plugin_reasons = evaluate_plugins(instance.plugins, listed)
            plugin_rows = tuple(rows)
            reasons.extend(plugin_reasons)
    if instance.readiness.channels:
        timeout = budget(PROBE_TIMEOUT_SECONDS)
        if timeout <= 0:
            reasons.append(Reason("channels_status_failed", "skipped: start window exhausted"))
        else:
            channels = seams.run([*exec_prefix, "channels", "status", "--json"], timeout)
            reasons.extend(evaluate_channels(channels))
    state: ReadinessState = "degraded" if reasons else "ready"
    return MemberReadiness(instance.name, state, tuple(reasons), plugin_rows, unit)


def _expire(result: MemberReadiness, window: float) -> MemberReadiness:
    reason = Reason("start_window_exceeded", f"not ready within {window:g}s")
    state: ReadinessState = "stopped" if result.state == "stopped" else "failed"
    return replace(result, state=state, reasons=(*result.reasons, reason))


def wait_members_ready(
    instances: Iterable[InstanceSpec],
    timeout: float | None = None,
    probes: ReadinessProbes | None = None,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> list[MemberReadiness]:
    """Poll until every member is ready/degraded or its start window has passed.

    The window is `timeout` if given, else each member's `readiness.start_window_seconds`.
    Members still not settled at their deadline become `failed` (or stay `stopped`) with
    reason `start_window_exceeded`. No probe runs past a deadline; the loop is bounded.
    """
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    seams = probes or default_probes()
    members = list(instances)
    start = seams.clock()
    windows = {
        member.name: float(
            timeout if timeout is not None else member.readiness.start_window_seconds
        )
        for member in members
    }
    deadlines = {name: start + window for name, window in windows.items()}
    latest: dict[str, MemberReadiness] = {}
    pending = list(members)
    # Defensive bound in case the clock does not advance; the deadlines bound real runs.
    max_rounds = int(max(windows.values(), default=0) / poll_interval) + 2
    rounds = 0
    while pending:
        rounds += 1
        still_pending: list[InstanceSpec] = []
        for member in pending:
            if member.name in latest and (
                seams.clock() >= deadlines[member.name] or rounds > max_rounds
            ):
                latest[member.name] = _expire(latest[member.name], windows[member.name])
                continue
            result = probe_member(member, seams, deadline=deadlines[member.name])
            latest[member.name] = result
            if result.state not in SETTLED_STATES:
                still_pending.append(member)
        pending = still_pending
        if not pending:
            break
        remaining = min(deadlines[member.name] for member in pending) - seams.clock()
        if remaining > 0:
            seams.sleep(min(poll_interval, remaining))
    return [latest[member.name] for member in members]
