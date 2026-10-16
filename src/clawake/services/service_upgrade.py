"""Pinned generic service upgrades with offline preview and pre-mutation backups."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from clawake.config import ServiceImageSpec, load_inventory
from clawake.services.resources import lifecycle, select_resources
from clawake.services.systemd import SystemdService
from clawake.services.upgrade import _update_image_fields


def upgrade_service(
    config: Path,
    name: str,
    tag: str,
    digest: str,
    template_root: Path,
    *,
    execute: bool = False,
    include_dependents: bool = False,
) -> list[str]:
    inventory = load_inventory(config)
    selected = select_resources(inventory, None, name)
    resource = selected[0]
    desired = ServiceImageSpec(repository=resource.image.repository, tag=tag, digest=digest)
    if resource.image.tag == desired.tag and resource.image.digest == desired.digest:
        return [f"unchanged {name}"]
    # Writable data must be declared in the backup policy; a partial backup is unsafe.
    required = {m.source for m in resource.mounts if not m.read_only}
    if not resource.backup_policy.enabled or not resource.backup_policy.pre_mutation:
        raise ValueError("Service upgrade requires pre-mutation backup policy")
    if not required <= set(resource.backup_policy.paths):
        raise ValueError("Writable bind paths must be declared in service backup policy")
    affected = {name}
    resources = [*inventory.instances, *inventory.services]
    while True:
        more = {r.name for r in resources if set(r.depends_on) & affected}
        if more <= affected:
            break
        affected |= more
    if len(affected) > 1 and not include_dependents:
        raise ValueError(
            "Dependent resources require an explicit --with-dependents maintenance plan"
        )
    ordered = [r for r in select_resources(inventory, None, None) if r.name in affected]
    plan = [f"stop {r.name}" for r in reversed(ordered)]
    plan += [f"backup {name}", f"upgrade {name} to {desired.repository}:{tag}@{digest}"]
    plan += [f"restart {r.name}" for r in ordered]
    if not execute:
        return plan
    before = config.read_bytes()
    systemd = SystemdService()
    if any(not Path(p).exists() for p in resource.backup_policy.paths):
        raise ValueError("Service backup source is missing; upgrade halted")
    for r in reversed(ordered):
        if systemd.stop(r.name, execute=True).return_code:
            raise ValueError("Cannot stop resource before consistent backup")
    root = config.parent / ".backups"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("Service backup root must be private and owned")
    backup = Path(tempfile.mkdtemp(prefix=name + "-", dir=root))
    archive_path = backup / "binds.tar.gz"
    descriptor = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with (
        os.fdopen(descriptor, "wb") as stream,
        tarfile.open(fileobj=stream, mode="w:gz", dereference=False) as archive,
    ):
        for index, path in enumerate(resource.backup_policy.paths):
            source = Path(path)
            if not source.exists():
                raise ValueError("Service backup source is missing; upgrade halted")
            archive.add(source, arcname=f"path-{index}")
    for volume in resource.volumes:
        path = backup / f"{volume.name}.tar"
        path.touch(mode=0o600, exist_ok=False)
        result = subprocess.run(
            [
                "podman",
                "volume",
                "export",
                "--output",
                str(path),
                resource.volume_name(volume.name),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise ValueError("Service volume backup failed; upgrade halted")
        path.chmod(0o600)
    if config.read_bytes() != before:
        raise ValueError("Inventory changed during backup; upgrade halted")
    updates = {"tag": tag, "digest": digest, "known_good_tag": resource.image.tag}
    if resource.image.digest:
        updates["known_good_digest"] = resource.image.digest
    _update_image_fields(config, name, updates, section="services")
    changed = load_inventory(config)
    lifecycle(changed, "setup", None, name, template_root, execute=True, selected_names=affected)
    return plan
