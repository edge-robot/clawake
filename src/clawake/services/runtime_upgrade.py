from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clawake.config import ImageSpec, InstanceSpec


@dataclass
class RuntimeCheck:
    healthy: bool
    version: str = ""
    image_name: str = ""
    error: str = ""


def image_ref(image: ImageSpec) -> str:
    base = f"{image.repository}:{image.tag}"
    return f"{base}@{image.digest}" if image.digest else base


def runtime_version_from_tag(tag: str) -> str:
    """Return the OpenClaw version encoded in official image variant tags."""
    return re.sub(r"-(?:browser|slim)(?:-(?:amd64|arm64))?$", "", tag)


def is_browser_image(image: ImageSpec) -> bool:
    return bool(re.search(r"-browser(?:-(?:amd64|arm64))?$", image.tag))


def browser_cache_path(instance: InstanceSpec) -> Path:
    return instance.runtime_state_path / "cache" / "openclaw-1000"


def ensure_browser_cache(instance: InstanceSpec) -> Path:
    path = browser_cache_path(instance)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def doctor_command(instance: InstanceSpec, image: ImageSpec) -> list[str]:
    command = ["podman", "run", "--rm", "--userns=keep-id:uid=1000,gid=1000"]
    if is_browser_image(image):
        command.extend(
            ["--volume", f"{browser_cache_path(instance)}:/home/node/.cache/openclaw-1000"]
        )
    for env_file in instance.env_files:
        command.extend(["--env-file", env_file])
    for dns_server in instance.dns_servers:
        command.extend(["--dns", dns_server])
    command.extend(
        [
            "--volume",
            f"{instance.workspace_path}:/workspace",
            "--volume",
            f"{instance.runtime_state_path}:/home/node/.openclaw",
            "--volume",
            f"{instance.team_definition_path}:/team-definition:ro",
        ]
    )
    for mount in instance.mounts:
        suffix = ":ro" if mount.read_only else ""
        command.extend(["--volume", f"{mount.source}:{mount.target}{suffix}"])
    command.extend([image_ref(image), "openclaw", "doctor", "--fix", "--non-interactive", "--yes"])
    return command


def run_doctor(instance: InstanceSpec, image: ImageSpec) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        doctor_command(instance, image), check=False, capture_output=True, text=True
    )


def verify_runtime(instance: InstanceSpec, expected: ImageSpec) -> RuntimeCheck:
    version = subprocess.run(
        ["podman", "exec", instance.container_name, "openclaw", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if version.returncode != 0:
        return RuntimeCheck(False, error=version.stderr.strip() or version.stdout.strip())

    inspected = subprocess.run(
        ["podman", "inspect", instance.container_name, "--format", "{{.ImageName}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        return RuntimeCheck(False, version=version.stdout.strip(), error=inspected.stderr.strip())

    running_image = inspected.stdout.strip()
    expected_ref = image_ref(expected)
    accepted_refs = {expected_ref}
    if expected.digest:
        # Podman normalizes tag@digest to repository@digest in container metadata.
        accepted_refs.add(f"{expected.repository}@{expected.digest}")
    if running_image not in accepted_refs:
        return RuntimeCheck(
            False,
            version=version.stdout.strip(),
            image_name=running_image,
            error=f"running image '{running_image}' does not match '{expected_ref}'",
        )
    expected_version = runtime_version_from_tag(expected.tag)
    if expected_version not in version.stdout:
        return RuntimeCheck(
            False,
            version=version.stdout.strip(),
            image_name=running_image,
            error=f"runtime version does not contain expected version '{expected_version}'",
        )
    return RuntimeCheck(True, version=version.stdout.strip(), image_name=running_image)


def _gateway_host_url(instance: InstanceSpec) -> str:
    gateway_port = instance.gateway_runtime.gateway_container_port
    published = next(
        (
            port
            for port in instance.ports
            if port.container_port == gateway_port and port.protocol == "tcp"
        ),
        None,
    )
    host_port = published.host_port if published else gateway_port
    return f"http://127.0.0.1:{host_port}/"
