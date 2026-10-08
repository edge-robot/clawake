"""Generic v2 resource lifecycle; no provider or application-specific behavior."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from clawake.config import InstanceSpec, Inventory, ServiceSpec
from clawake.services.deployment import apply_artifacts, plan_deployment
from clawake.services.systemd import SystemdService

Resource = InstanceSpec | ServiceSpec


def select_resources(
    inventory: Inventory, member: str | None, service: str | None, *, dependencies: bool = False
) -> list[Resource]:
    if member and service:
        raise ValueError("Select either --member or --service")
    resources = {r.name: r for r in [*inventory.instances, *inventory.services]}
    if member:
        if member not in {r.name for r in inventory.instances}:
            raise ValueError("Unknown member")
        names = {member}
    elif service:
        if service not in {r.name for r in inventory.services}:
            raise ValueError("Unknown service")
        names = {service}
    else:
        names = set(resources)
    if dependencies:
        pending = list(names)
        while pending:
            for name in resources[pending.pop()].depends_on:
                if name not in names:
                    names.add(name)
                    pending.append(name)
    return [resources[name] for name in inventory.dependency_order() if name in names]


def secret_preflight(inventory: Inventory, resources: list[Resource]) -> None:
    required = {s.name for r in resources if isinstance(r, ServiceSpec) for s in r.secrets}
    if not required:
        return
    result = subprocess.run(
        ["podman", "secret", "ls", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError("Cannot verify Podman secret references")
    missing = required - set(result.stdout.splitlines())
    if missing:
        raise ValueError("Missing Podman secret references: " + ", ".join(sorted(missing)))


def readiness(resource: Resource) -> str:
    # Inspect only safe status fields, never container environment or health output.
    field = (
        "{{.State.Health.Status}}"
        if isinstance(resource, ServiceSpec) and resource.health
        else "{{.State.Status}}"
    )
    result = subprocess.run(
        ["podman", "inspect", "--format", field, resource.container_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    status = result.stdout.strip()
    return (
        status
        if not result.returncode
        and status in {"healthy", "unhealthy", "starting", "running", "exited", "created"}
        else "unavailable"
    )


def wait_ready(resource: Resource) -> bool:
    health = resource.health
    retries = health.retries if health else 3
    for attempt in range(retries):
        state = readiness(resource)
        if state == ("healthy" if isinstance(resource, ServiceSpec) and health else "running"):
            return True
        if attempt + 1 < retries:
            time.sleep(min(health.interval_seconds if health else 1, 5))
    return False


def lifecycle(
    inventory: Inventory,
    action: str,
    member: str | None,
    service_name: str | None,
    template_root: Path,
    *,
    execute: bool = False,
    prepare_instances: Callable[[list[InstanceSpec]], None] | None = None,
    selected_names: set[str] | None = None,
) -> list[str]:
    selected = select_resources(
        inventory, member, service_name, dependencies=action in {"setup", "restart"}
    )
    if selected_names is not None:
        selected = [r for r in select_resources(inventory, None, None) if r.name in selected_names]
    names = {r.name for r in selected}
    all_resources = [*inventory.instances, *inventory.services]
    if action == "teardown" and any(
        set(r.depends_on) & names for r in all_resources if r.name not in names
    ):
        raise ValueError("Unselected resources depend on the teardown selection")
    changes = plan_deployment(inventory, selected, template_root) if action == "setup" else []
    plan = [
        f"{action} {r.name}" for r in (reversed(selected) if action == "teardown" else selected)
    ]
    if action == "setup":
        plan = [f"write {c.destination}" for c in changes] + plan
    if not execute:
        return plan
    systemd = SystemdService()
    if action in {"setup", "restart"}:
        secret_preflight(inventory, selected)
        # Check declared inputs without reading secrets, env files or creating bind paths.
        for r in selected:
            if isinstance(r, ServiceSpec):
                if any(not Path(m.source).exists() for m in r.mounts) or any(
                    not Path(p).is_file() for p in r.env_files
                ):
                    raise ValueError(f"Missing declared input for service {r.name}")
        if prepare_instances is not None:
            prepare_instances([r for r in selected if isinstance(r, InstanceSpec)])
        if action == "setup":
            for r in selected:
                if isinstance(r, InstanceSpec):
                    (Path(r.workspace_path) / ".openclaw").mkdir(parents=True, exist_ok=True)
            apply_artifacts(changes)
            if systemd.daemon_reload(execute=True).return_code:
                raise ValueError("systemd reload failed")
        for r in selected:
            if systemd.restart(r.name, execute=True).return_code or not wait_ready(r):
                raise ValueError(f"Resource {r.name} failed readiness; dependents were not started")
    elif action == "teardown":
        roots = {h.name: Path(h.quadlet_root) for h in inventory.hosts}
        for r in reversed(selected):
            if systemd.stop(r.name, execute=True).return_code:
                raise ValueError(f"Cannot stop {r.name}; teardown halted")
            result = systemd.remove_container(r.container_name, execute=True)
            if result.return_code and not any(
                text in result.stderr.lower() for text in ("no such container", "does not exist")
            ):
                raise ValueError(f"Cannot remove container {r.name}")
            for artifact in r.quadlet_artifact_paths:
                if systemd.remove_quadlet(str(roots[r.host] / artifact), execute=True).return_code:
                    raise ValueError(f"Cannot remove definition for {r.name}")
        # Remove definitions only. Podman volumes and all bind data are preserved.
        for network in inventory.networks:
            owners = {r.name for r in all_resources if network.name in r.networks}
            if owners and owners <= names:
                if systemd.stop(f"{network.name}-network", execute=True).return_code:
                    raise ValueError("Cannot stop shared network")
                if systemd.remove_quadlet(
                    str(roots[network.host] / network.quadlet_path), execute=True
                ).return_code:
                    raise ValueError("Cannot remove shared network definition")
        if systemd.daemon_reload(execute=True).return_code:
            raise ValueError("systemd reload failed")
    else:
        raise ValueError("Unsupported lifecycle action")
    return plan


def status_rows(inventory: Inventory, member: str | None, service: str | None) -> list[dict]:
    rows = []
    systemd = SystemdService()
    resources = {r.name: r for r in [*inventory.instances, *inventory.services]}
    states = {}
    for r in select_resources(inventory, member, service):
        unit = systemd.status(r.name, execute=True)
        result = subprocess.run(
            [
                "podman",
                "inspect",
                "--format",
                "{{.Image}} {{.RestartCount}} {{.State.Status}}",
                r.container_name,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        match = re.fullmatch(
            r"((?:sha256:)?[0-9a-f]{64}) ([0-9]{1,10}) (running|exited|created|paused|stopped)",
            result.stdout.strip(),
        )
        image_id, restart_count, container_state = (
            (match[1], int(match[2]), match[3]) if match else (None, None, "unavailable")
        )
        if image_id and not image_id.startswith("sha256:"):
            image_id = "sha256:" + image_id
        digest = None
        if image_id:
            image = subprocess.run(
                ["podman", "image", "inspect", "--format", "{{.Digest}}", image_id],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if not image.returncode and re.fullmatch(r"sha256:[0-9a-f]{64}", image.stdout.strip()):
                digest = image.stdout.strip()
        failed = []
        for name in r.depends_on:
            if name not in states:
                states[name] = readiness(resources[name])
            dependency = resources[name]
            expected = (
                "healthy"
                if isinstance(dependency, ServiceSpec) and dependency.health
                else "running"
            )
            if states[name] != expected:
                failed.append(name)
        rows.append(
            {
                "name": r.name,
                "kind": "service" if isinstance(r, ServiceSpec) else "member",
                "unit": systemd.unit_name(r.name),
                "active": unit.return_code == 0,
                "readiness": readiness(r),
                "dependencies": list(r.depends_on),
                "failed_dependencies": failed,
                "image": r.image.repository + ":" + r.image.tag,
                "digest": r.image.digest,
                "running_image_id": image_id,
                "running_image_digest": digest,
                "restart_count": restart_count,
                "container_state": container_state,
            }
        )
    return rows


def status_json(inventory: Inventory, member: str | None, service: str | None) -> str:
    return json.dumps(
        {"cluster": inventory.cluster.name, "resources": status_rows(inventory, member, service)},
        indent=2,
    )
