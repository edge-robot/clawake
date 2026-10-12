"""Focused rootless MCP lifecycle; never reads or brokers OAuth credentials."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator

from clawake.config import BaseModel, HostPath, NetworkSpec, ResourceName, ServiceImageSpec
from clawake.services.deployment import ArtifactChange
from clawake.services.render import render_shared_network


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


def render_onshape_mcp(spec: OnshapeMcpSpec) -> dict[str, str]:
    image = f"{spec.image.repository}:{spec.image.tag}@{spec.image.digest}"
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
HealthCmd=curl --fail --silent http://127.0.0.1:8080/health
HealthInterval=30s
HealthTimeout=5s
HealthRetries=3
HealthOnFailure=kill
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
