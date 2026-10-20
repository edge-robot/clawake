"""Focused rootless MCP lifecycle; never reads or brokers OAuth credentials."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from clawake.config import BaseModel, HostPath, NetworkSpec, ResourceName, ServiceImageSpec
from clawake.services.deployment import ArtifactChange
from clawake.services.readiness import run_command
from clawake.services.render import render_shared_network
from clawake.services.systemd import CommandResult, SystemdService

# The health check rendered before #39. Without a `health` block the quadlet stays byte-identical.
DEFAULT_HEALTH_COMMAND = ["curl", "--fail", "--silent", "http://127.0.0.1:8080/health"]
_ABSOLUTE_EXECUTABLE = re.compile(r"/[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*")
_BARE_EXECUTABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


class OnshapeMcpHealth(BaseModel):
    """Container health check; `command` runs without a shell (exec form)."""

    command: list[str] = Field(
        default_factory=lambda: list(DEFAULT_HEALTH_COMMAND), min_length=1, max_length=16
    )
    interval_s: int = Field(default=30, ge=1, le=3600)
    timeout_s: int = Field(default=5, ge=1, le=600)
    retries: int = Field(default=3, ge=1, le=100)
    start_period_s: int = Field(default=0, ge=0, le=3600)

    @field_validator("command")
    @classmethod
    def safe_command(cls, command: list[str]) -> list[str]:
        for argument in command:
            # `%` and `$` would be expanded by systemd in the generated ExecStart line.
            if not argument or not argument.isprintable() or "%" in argument or "$" in argument:
                raise ValueError(
                    "Health command arguments must be non-empty single-line text without '%' or '$'"
                )
        executable = command[0]
        if not (
            _ABSOLUTE_EXECUTABLE.fullmatch(executable) or _BARE_EXECUTABLE.fullmatch(executable)
        ):
            raise ValueError("Health command must start with an absolute path or executable name")
        return command

    @property
    def shell_form(self) -> bool:
        """Only the legacy default keeps Podman's shell form, for a byte-identical quadlet."""
        return self.command == DEFAULT_HEALTH_COMMAND

    def health_cmd(self) -> str:
        if self.shell_form:
            return " ".join(self.command)
        # A JSON array starting with "CMD" is exec form: Podman runs it without /bin/sh.
        return json.dumps(["CMD", *self.command], separators=(",", ":"))

    def preflight_executables(self) -> list[str]:
        """Executables the image must provide for this health check to run."""
        if self.shell_form:
            return ["/bin/sh", self.command[0]]
        return [self.command[0]]

    def quadlet_lines(self) -> list[str]:
        lines = [
            f"HealthCmd={self.health_cmd()}",
            f"HealthInterval={self.interval_s}s",
            f"HealthTimeout={self.timeout_s}s",
            f"HealthRetries={self.retries}",
        ]
        if self.start_period_s > 0:
            lines.append(f"HealthStartPeriod={self.start_period_s}s")
        lines.append("HealthOnFailure=kill")
        return lines


class OnshapeMcpSpec(BaseModel):
    name: ResourceName = "clawonshape-mcp"
    client_container: ResourceName | None = None
    network: ResourceName = "clawonshape-mcp-private"
    image: ServiceImageSpec
    credential_directory: HostPath
    config_file: HostPath
    quadlet_root: HostPath
    memory_mb: int = Field(default=512, ge=128, le=4096)
    cpu_quota_percent: int = Field(default=100, ge=10, le=400)
    health: OnshapeMcpHealth = Field(default_factory=OnshapeMcpHealth)

    @model_validator(mode="after")
    def pinned(self) -> OnshapeMcpSpec:
        if not self.image.digest:
            raise ValueError("Onshape MCP requires an immutable image digest")
        paths = [Path(self.credential_directory), Path(self.config_file), Path(self.quadlet_root)]
        if any(
            a == b or a.is_relative_to(b) or b.is_relative_to(a)
            for i, a in enumerate(paths)
            for b in paths[i + 1 :]
        ):
            raise ValueError("MCP storage and Quadlet paths must be disjoint")
        return self


def image_reference(spec: OnshapeMcpSpec) -> str:
    return f"{spec.image.repository}:{spec.image.tag}@{spec.image.digest}"


def render_onshape_mcp(spec: OnshapeMcpSpec) -> dict[str, str]:
    image = image_reference(spec)
    health = "\n".join(spec.health.quadlet_lines())
    network = render_shared_network(
        NetworkSpec(name=spec.network, host="local", quadlet_default_dependencies=False)
    )
    # Network egress is required for cad.onshape.com; no published inbound port.
    container = f"""[Quadlet]
DefaultDependencies=false

[Unit]
Description=Clawake external-token read-only Onshape MCP
After=network-online.target
Wants=network-online.target

[Container]
ContainerName={spec.name}
Image={image}
Network={spec.network}.network
NetworkAlias={spec.name}
UserNS=keep-id:uid=1000,gid=1000
User=1000:1000
ReadOnly=true
NoNewPrivileges=true
DropCapability=all
Volume={spec.credential_directory}:/run/onshape-credentials:ro
Volume={spec.config_file}:/run/onshape-config/config.toml:ro
Exec=--config /run/onshape-config/config.toml http
{health}
PodmanArgs=--pids-limit=128 --tmpfs=/tmp:rw,noexec,nosuid,size=16m

[Service]
Restart=on-failure
RestartSec=5
TimeoutStartSec=120
MemoryMax={spec.memory_mb}M
CPUQuota={spec.cpu_quota_percent}%

[Install]
WantedBy=default.target
"""
    return {f"{spec.network}.network": network, f"{spec.name}.container": container}


class McpHealthError(ValueError):
    """A fail-closed health preflight or health wait result; the message is operator-ready."""


@dataclass(frozen=True)
class McpProbes:
    """Injectable seams for every podman/systemctl call of the MCP health lifecycle."""

    run: Callable[[list[str], float], CommandResult]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def default_mcp_probes() -> McpProbes:
    return McpProbes(run=run_command)


PREFLIGHT_TIMEOUT_SECONDS = 60.0
# podman run: 125 is a Podman error, 126/127 mean the contained command cannot be invoked or
# found. Every other exit code comes from the executable itself, so it exists.
_PODMAN_ERROR = 125
_COMMAND_NOT_RUNNABLE = frozenset({126, 127})


def preflight_command(spec: OnshapeMcpSpec, executable: str) -> list[str]:
    """Read-only check that the image can execute `executable`: no volumes, no network."""
    return [
        "podman",
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--entrypoint",
        executable,
        image_reference(spec),
        "--help",
    ]


def preflight_health_command(spec: OnshapeMcpSpec, probes: McpProbes) -> None:
    """Fail closed unless the pinned image can run every executable of the health check."""
    image = image_reference(spec)
    for executable in spec.health.preflight_executables():
        result = probes.run(preflight_command(spec, executable), PREFLIGHT_TIMEOUT_SECONDS)
        # run_command reports a missing podman binary as 127 with the exception class name.
        if result.return_code == 127 and result.stderr == "FileNotFoundError":
            raise McpHealthError("Onshape MCP preflight failed: podman is not available")
        if result.return_code == 124 and result.stderr == "timeout":
            raise McpHealthError(
                f"Onshape MCP preflight for image {image} and health command {executable} "
                f"timed out after {PREFLIGHT_TIMEOUT_SECONDS:g}s"
            )
        if result.return_code in _COMMAND_NOT_RUNNABLE:
            raise McpHealthError(
                f"Onshape MCP image {image} cannot run health command {executable}: "
                f"executable missing or not runnable (podman exit {result.return_code})"
            )
        if result.return_code == _PODMAN_ERROR:
            raise McpHealthError(
                f"Onshape MCP preflight could not run image {image} for health command "
                f"{executable}: podman exit {_PODMAN_ERROR} (the image must be in local storage)"
            )


HEALTH_POLL_SECONDS = 2.0
HEALTH_PROBE_TIMEOUT_SECONDS = 10.0
HEALTH_WAIT_MARGIN_SECONDS = 30
_HEALTH_STATUS = re.compile(r"[a-z]{1,32}")


def health_wait_window(spec: OnshapeMcpSpec) -> int:
    """Bounded post-apply wait: start period, every allowed failed probe plus one, 30 s margin."""
    health = spec.health
    return (
        health.start_period_s
        + health.interval_s * (health.retries + 1)
        + (HEALTH_WAIT_MARGIN_SECONDS)
    )


def health_status_command(spec: OnshapeMcpSpec) -> list[str]:
    return ["podman", "inspect", "--format", "{{.State.Health.Status}}", spec.name]


def restart_count_command(spec: OnshapeMcpSpec) -> list[str]:
    unit = SystemdService.unit_name(spec.name)
    return ["systemctl", "--user", "show", "--property=NRestarts", "--value", unit]


def read_health_status(spec: OnshapeMcpSpec, probes: McpProbes, timeout: float) -> str:
    """The Podman health state, or ``unknown``; never echoes free text."""
    result = probes.run(health_status_command(spec), timeout)
    value = result.stdout.strip()
    if result.return_code or not _HEALTH_STATUS.fullmatch(value):
        return "unknown"
    return value


def read_restart_count(spec: OnshapeMcpSpec, probes: McpProbes, timeout: float) -> int | None:
    """systemd's automatic restart counter of the unit, or ``None`` when unreadable."""
    result = probes.run(restart_count_command(spec), timeout)
    value = result.stdout.strip()
    if result.return_code or not value.isdigit():
        return None
    return int(value)


@dataclass(frozen=True)
class McpHealthOutcome:
    healthy: bool
    state: str
    restarts: int | None
    elapsed: float

    def message(self) -> str:
        if self.healthy:
            return (
                f"Onshape MCP service healthy after {round(self.elapsed)}s; "
                "credential readiness unverified"
            )
        restarts = "+?" if self.restarts is None else f"{self.restarts:+d}"
        return f"Onshape MCP service not healthy: {self.state}, restarts {restarts}"


def wait_healthy(spec: OnshapeMcpSpec, probes: McpProbes) -> McpHealthOutcome:
    """Poll until ``healthy`` without new restarts, ``unhealthy``, a restart, or the window ends.

    Read-only and bounded by `health_wait_window`; every probe has its own timeout. Nothing is
    stopped or rolled back here: the operator decides what to do with a failure.
    """
    window = health_wait_window(spec)
    start = probes.clock()
    deadline = start + window

    def probe_timeout() -> float:
        return max(1.0, min(HEALTH_PROBE_TIMEOUT_SECONDS, deadline - probes.clock()))

    baseline = read_restart_count(spec, probes, probe_timeout())
    if baseline is None:
        return McpHealthOutcome(False, "restart count unavailable", None, 0.0)
    while True:
        state = read_health_status(spec, probes, probe_timeout())
        count = read_restart_count(spec, probes, probe_timeout())
        elapsed = probes.clock() - start
        if count is None:
            return McpHealthOutcome(False, "restart count unavailable", None, elapsed)
        restarts = count - baseline
        # A changed counter means systemd restarted the service during the wait (#39).
        if restarts != 0:
            return McpHealthOutcome(False, state, restarts, elapsed)
        if state == "healthy":
            return McpHealthOutcome(True, state, restarts, elapsed)
        if state == "unhealthy":
            return McpHealthOutcome(False, state, restarts, elapsed)
        remaining = deadline - probes.clock()
        if remaining <= 0:
            return McpHealthOutcome(
                False, f"timeout after {window}s (last state {state})", restarts, elapsed
            )
        probes.sleep(min(HEALTH_POLL_SECONDS, remaining))


def plan_onshape_mcp(spec: OnshapeMcpSpec) -> list[ArtifactChange]:
    changes = []
    for relative, content in render_onshape_mcp(spec).items():
        destination = Path(spec.quadlet_root) / relative
        previous = destination.read_text() if destination.exists() else None
        if content != previous:
            changes.append(
                ArtifactChange(spec.name, "onshape_mcp", destination, content, previous=previous)
            )
    return changes


def validate_runtime_paths(spec: OnshapeMcpSpec) -> None:
    # Inspect only ownership/permissions. Token file contents remain owner-only.
    import os
    import stat

    for value, directory in [(spec.credential_directory, True), (spec.config_file, False)]:
        item = Path(value)
        metadata = item.lstat()
        if (
            item.resolve() != item
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or (directory and not stat.S_ISDIR(metadata.st_mode))
            or (not directory and not stat.S_ISREG(metadata.st_mode))
        ):
            raise ValueError("MCP runtime path requires private owner permissions")


def probe_native(spec: OnshapeMcpSpec) -> dict:
    """Real native CLI discovery in an operator-only profile; no agent turn/tool call."""
    import json
    import subprocess

    if not spec.client_container:
        raise ValueError("Native probe requires an explicit client container")
    prefix = [
        "podman",
        "exec",
        spec.client_container,
        "openclaw",
        "--profile",
        "onshape-local-readonly-20261012",
        "mcp",
    ]
    entry = {
        "transport": "streamable-http",
        "url": "http://clawonshape-mcp:8080/mcp",
        "enabled": True,
        "requestTimeoutMs": 30000,
        "connectionTimeoutMs": 5000,
    }
    saved = subprocess.run(
        [*prefix, "set", "onshape", json.dumps(entry)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if saved.returncode:
        raise ValueError("Native operator profile configuration failed")
    result = subprocess.run(
        [*prefix, "probe", "onshape", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ValueError("Native local MCP discovery failed")
    payload = json.loads(result.stdout)
    if not isinstance(payload.get("tools"), list) or payload.get("diagnostics"):
        raise ValueError("Native local MCP discovery contract invalid")
    names = payload["tools"]
    if len(names) > 256 or any(
        not isinstance(n, str) or not n.replace("_", "").isalnum() for n in names
    ):
        raise ValueError("Native local MCP discovery contract invalid")
    return {
        "profile": "onshape-local-readonly-20261012",
        "nativeDiscovery": "PASS",
        "tools": names,
        "fullSchemas": "UNAVAILABLE_FROM_NATIVE_CLI",
        "toolCalls": 0,
        "agentGrantsChanged": False,
    }
