"""Offline v2 validation; no filesystem writes, provider or runtime calls."""

from __future__ import annotations

import os
import re
from graphlib import CycleError, TopologicalSorter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from clawake.config import Inventory

NAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"


def resource_name(value: str) -> str:
    if not re.fullmatch(NAME_PATTERN, value):
        raise ValueError("Resource name must be a lowercase DNS label of at most 63 characters")
    return value


def single_line(value: str) -> str:
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Expected non-empty text without control characters")
    return value


def safe_path(value: str, *, host: bool) -> str:
    """Normalize lexically, without resolving symlinks or checking path existence."""
    if host:
        value = os.path.expanduser(os.path.expandvars(value))
    single_line(value)
    if any(c.isspace() or c in ":\\%$\"'" for c in value):
        raise ValueError("Path contains unsupported whitespace, variable or delimiter characters")
    if not value.startswith("/") or value.startswith("//"):
        raise ValueError("Path must be an absolute path")
    if not host and any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("Container target must not contain dot segments")
    normalized = os.path.normpath(value)
    if normalized == "/":
        raise ValueError("Root paths are not allowed")
    return normalized


def host_path(value: str) -> str:
    return safe_path(value, host=True)


def target_path(value: str) -> str:
    return safe_path(value, host=False)


def interface_url(value: str) -> str:
    single_line(value)
    if any(c.isspace() for c in value) or any(c in value for c in ("\\", '"', "'", "$", "%")):
        raise ValueError("Interface URL contains unsupported characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = parsed.scheme in {"http", "https"} and parsed.hostname
    except ValueError as exc:
        raise ValueError("Invalid interface URL") from exc
    if not valid or port == 0:
        raise ValueError("Interface URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or "?" in value or "#" in value:
        raise ValueError("Interface URL must not contain credentials, a query or a fragment")
    return value


def dependency_order(inventory: Inventory) -> list[str]:
    resources = [*inventory.instances, *inventory.services]
    by_name = {item.name: item for item in resources}
    graph = {}
    for item in resources:
        if len(item.depends_on) != len(set(item.depends_on)):
            raise ValueError(f"Duplicate dependencies for {item.name}")
        for name in item.depends_on:
            if name == item.name:
                raise ValueError(f"Self dependency for {item.name}")
            if name not in by_name or by_name[name].host != item.host:
                raise ValueError(f"Unknown or cross-host dependency for {item.name}")
        graph[item.name] = set(item.depends_on)
    sorter = TopologicalSorter(graph)
    try:
        sorter.prepare()
    except CycleError as exc:
        raise ValueError("Cyclic service/instance dependencies") from exc
    ordered = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        ordered.extend(ready)
        sorter.done(*ready)
    return ordered


def require_v1_runtime(inventory: Inventory) -> None:
    if inventory.version != 1:
        raise ValueError(
            "Inventory v2 is validation-only; service rendering and lifecycle are not implemented"
        )


def validate_v2_inventory(inventory: Inventory) -> None:
    # Imported lazily to keep helpers usable by the model's field validators.
    from clawake.config import bind_addresses_overlap

    resources = [*inventory.instances, *inventory.services]
    hosts = {h.name for h in inventory.hosts}
    networks = {n.name: n for n in inventory.networks}
    resource_name(inventory.cluster.name)
    declared: set[str] = set()
    for item in [*resources, *inventory.networks]:
        resource_name(item.name)
        if item.name in declared:
            raise ValueError(f"Duplicate resource name: {item.name}")
        declared.add(item.name)
        if item.host not in hosts:
            raise ValueError(f"Unknown host for {item.name}")
    for host in inventory.hosts:
        resource_name(host.name)
        host.quadlet_root = host_path(host.quadlet_root)

    # Single-host inventory: unit namespaces are shared across all Quadlet types.
    units: set[str] = set()
    artifacts: set[str] = set()
    containers: set[str] = set()
    volumes: set[str] = set()
    podman_networks = set(networks)

    def artifact(path: str) -> None:
        single_line(path)
        parts = path.split("/")
        if path.startswith("/") or any(p in {"", ".", ".."} for p in parts):
            raise ValueError("Quadlet path must be a safe relative path")
        if any(c.isspace() or c in ":\\%$\"'" for c in path):
            raise ValueError("Unsafe Quadlet path")
        stem, suffix = PurePosixPath(path).stem, PurePosixPath(path).suffix
        unit_suffix = {".container": "", ".network": "-network", ".volume": "-volume"}[suffix]
        unit = f"{stem}{unit_suffix}.service"
        if path in artifacts or unit in units:
            raise ValueError(f"Quadlet artifact or generated unit collision: {path}")
        artifacts.add(path)
        units.add(unit)

    def volume(name: str) -> None:
        if name in volumes:
            raise ValueError(f"Podman volume name collision: {name}")
        volumes.add(name)

    for network in inventory.networks:
        artifact(network.quadlet_path)

    used_ports = []
    for item in resources:
        resource_name(item.container_name)
        if item.container_name in containers:
            raise ValueError(f"Duplicate container name: {item.container_name}")
        containers.add(item.container_name)
        if len(item.networks) != len(set(item.networks)):
            raise ValueError(f"Duplicate network membership: {item.name}")
        for name in item.networks:
            if name not in networks or networks[name].host != item.host:
                raise ValueError(f"Unknown or cross-host network for {item.name}")
        for path in item.quadlet_artifact_paths:
            artifact(path)
        for port in item.ports:
            for other in used_ports:
                if (
                    port.host_port == other.host_port
                    and port.protocol == other.protocol
                    and bind_addresses_overlap(port.bind_address, other.bind_address)
                ):
                    raise ValueError(f"Port collision involving {item.name}")
            used_ports.append(port)

    legacy_names: set[str] = set()
    for instance in inventory.instances:
        if Path(instance.quadlet_path).stem != instance.name:
            raise ValueError("V2 instance name must match its Quadlet filename stem")
        instance.workspace_path = host_path(instance.workspace_path)
        instance.team_definition_path = host_path(instance.team_definition_path)
        instance.env_files = [host_path(path) for path in instance.env_files]
        instance.backup_policy.paths = [host_path(path) for path in instance.backup_policy.paths]
        volume(f"{instance.container_name}-state")
        if not instance.networks:
            if instance.container_name in podman_networks:
                raise ValueError("Private/shared Podman network collision")
            podman_networks.add(instance.container_name)
        targets = {"/workspace", "/team-definition", "/home/node/.openclaw"}
        for plugin in instance.plugins:
            if plugin.artifact_path is not None:
                plugin.artifact_path = host_path(plugin.artifact_path)
                targets.add(plugin.container_path)
        for mount in instance.mounts:
            mount.source = host_path(mount.source)
            mount.target = target_path(mount.target)
            if mount.target in targets:
                raise ValueError(f"Duplicate mount target for {instance.name}")
            targets.add(mount.target)
        for legacy in instance.legacy_names:
            resource_name(legacy)
            if f"{legacy}.service" in units or legacy in containers or legacy in legacy_names:
                raise ValueError(f"Legacy resource collision: {legacy}")
            legacy_names.add(legacy)

    for service in inventory.services:
        for entry in service.volumes:
            volume(service.volume_name(entry.name))
    dependency_order(inventory)
