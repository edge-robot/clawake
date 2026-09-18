"""Side-effect-free deployment planning and explicit artifact application."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from clawake.config import InstanceSpec, Inventory
from clawake.services.render import render_instance_assets, render_shared_network
from clawake.services.runtime_config import plan_runtime_files


@dataclass(frozen=True)
class ArtifactChange:
    member: str
    role: str
    destination: Path
    content: str = field(repr=False)
    kind: str = "quadlet"
    previous: str | None = field(default=None, repr=False)
    changed_keys: tuple[str, ...] = ()


def plan_deployment(
    inventory: Inventory, instances: list[InstanceSpec], template_root: Path
) -> list[ArtifactChange]:
    """Render desired state in memory and compare it with installed artifacts."""
    hosts = {host.name: host for host in inventory.hosts}
    changes = []
    required_networks = {name for instance in instances for name in instance.networks}
    for network in inventory.networks:
        if network.name not in required_networks:
            continue
        destination = Path(hosts[network.host].quadlet_root).expanduser() / network.quadlet_path
        content = render_shared_network(network)
        current = destination.read_text(encoding="utf-8") if destination.exists() else None
        if current != content:
            changes.append(ArtifactChange(
                network.name, "shared_network", destination, content, previous=current,
            ))
    for instance in instances:
        root = Path(hosts[instance.host].quadlet_root).expanduser()
        for relative, content in render_instance_assets(instance, template_root).items():
            destination = root / relative
            current = destination.read_text(encoding="utf-8") if destination.exists() else None
            if current != content:
                changes.append(ArtifactChange(
                    instance.name, instance.role, destination, content, previous=current,
                ))
        for destination, content, current, keys in plan_runtime_files(inventory, instance):
            changes.append(ArtifactChange(
                instance.name, instance.role, destination, content, "runtime", current, keys,
            ))
    return changes


def apply_artifacts(changes: list[ArtifactChange]) -> list[Path]:
    """Write a reviewed plan. The caller owns runtime preparation and reloads."""
    # Detect drift before applying any part of the plan; never print file contents.
    for change in changes:
        current = (change.destination.read_text(encoding="utf-8")
                   if change.destination.exists() else None)
        if current != change.previous:
            raise ValueError(f"Configuration changed since planning: {change.destination}")
    deployed = []
    for change in changes:
        change.destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".clawake-", dir=change.destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(change.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600 if change.kind == "runtime" else 0o644)
            os.replace(temporary, change.destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        deployed.append(change.destination)
    return deployed
