from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clawake.config import InstanceSpec, PluginSpec


@dataclass(frozen=True)
class PluginSyncResult:
    plugin_id: str
    digest: str
    commands: list[list[str]]


def artifact_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def verify_plugin(plugin: PluginSpec) -> str:
    if plugin.source_type == "npm":
        assert plugin.integrity is not None
        return plugin.integrity
    assert plugin.artifact_path is not None
    assert plugin.sha256 is not None
    path = Path(plugin.artifact_path)
    if not path.is_file():
        raise ValueError(f"Plugin artifact does not exist: {path}")
    actual = artifact_digest(path)
    if actual != plugin.sha256:
        raise ValueError(
            f"Plugin '{plugin.id}' digest mismatch: expected {plugin.sha256}, got {actual}"
        )
    return actual


def plugin_commands(instance: InstanceSpec, plugin: PluginSpec) -> list[list[str]]:
    install_command = [
        "podman",
        "exec",
        instance.container_name,
        "openclaw",
        "plugins",
        "install",
        "--force",
        "--accept-capabilities",
        plugin.install_target,
    ]
    if plugin.source_type == "npm":
        install_command.append("--pin")
    return [install_command]


def _verify_mounted_artifact(instance: InstanceSpec, plugin: PluginSpec) -> None:
    assert plugin.sha256 is not None
    command = ["podman", "exec", instance.container_name, "sha256sum", plugin.container_path]
    process = subprocess.run(command, check=False, capture_output=True, text=True)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"Cannot verify mounted plugin '{plugin.id}': {detail}")
    mounted_hash = process.stdout.strip().split(maxsplit=1)[0]
    mounted_digest = f"sha256:{mounted_hash}"
    if mounted_digest != plugin.sha256:
        raise RuntimeError(
            f"Mounted plugin '{plugin.id}' digest mismatch: expected {plugin.sha256}, "
            f"got {mounted_digest}. Run clawake setup --execute before syncing."
        )


def npm_inspect_command(instance: InstanceSpec, plugin: PluginSpec) -> list[str]:
    """Read-only `plugins inspect --runtime --json` argv for the npm skip check."""
    return [
        "podman",
        "exec",
        instance.container_name,
        "openclaw",
        "plugins",
        "inspect",
        plugin.id,
        "--runtime",
        "--json",
    ]


def npm_install_mismatch(plugin: PluginSpec, return_code: int, stdout: str) -> str | None:
    """Return a reason code if the installed npm package differs from the pin, else None.

    Same comparison as the post-install check (resolved spec and sha512 integrity); the
    output is parsed but never echoed.
    """
    if return_code != 0:
        return "plugin_missing"
    try:
        payload = json.loads(stdout)
        installed = payload["install"]
        actual_spec = installed["resolvedSpec"]
        actual_integrity = installed["integrity"]
        actual_id = payload["plugin"]["id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return "inspect_unparseable"
    if actual_id != plugin.id or actual_spec != plugin.npm_spec:
        return "version_mismatch"
    if actual_integrity != plugin.integrity:
        return "integrity_mismatch"
    return None


def _verify_installed_npm_plugin(instance: InstanceSpec, plugin: PluginSpec) -> None:
    assert plugin.npm_spec is not None
    assert plugin.integrity is not None
    command = [
        "podman",
        "exec",
        instance.container_name,
        "openclaw",
        "plugins",
        "inspect",
        plugin.id,
        "--runtime",
        "--json",
    ]
    process = subprocess.run(command, check=False, capture_output=True, text=True)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"Cannot inspect installed plugin '{plugin.id}': {detail}")
    try:
        payload = json.loads(process.stdout)
        installed = payload["install"]
        actual_spec = installed["resolvedSpec"]
        actual_integrity = installed["integrity"]
        actual_id = payload["plugin"]["id"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Cannot verify installed npm metadata for plugin '{plugin.id}'"
        ) from exc
    if actual_id != plugin.id or actual_spec != plugin.npm_spec:
        raise RuntimeError(
            f"Installed plugin '{plugin.id}' package mismatch: expected {plugin.npm_spec}, "
            f"got {actual_spec}"
        )
    if actual_integrity != plugin.integrity:
        raise RuntimeError(
            f"Installed plugin '{plugin.id}' integrity mismatch: expected "
            f"{plugin.integrity}, got {actual_integrity}"
        )


def sync_plugin(
    instance: InstanceSpec, plugin: PluginSpec, execute: bool = False
) -> PluginSyncResult:
    digest = verify_plugin(plugin)
    commands = plugin_commands(instance, plugin)
    if execute:
        if plugin.source_type == "archive":
            _verify_mounted_artifact(instance, plugin)
        for index, command in enumerate(commands):
            process = subprocess.run(command, check=False, capture_output=True, text=True)
            if process.returncode != 0:
                detail = process.stderr.strip() or process.stdout.strip()
                raise RuntimeError(f"Plugin command failed ({' '.join(command)}): {detail}")
            if index == 0 and plugin.source_type == "npm":
                _verify_installed_npm_plugin(instance, plugin)
    return PluginSyncResult(plugin.id, digest, commands)
