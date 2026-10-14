from __future__ import annotations

import logging
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from clawake.config import InstanceSpec

logger = logging.getLogger(__name__)


def backup_sources(instance: InstanceSpec) -> list[Path]:
    ordered = [
        Path(instance.workspace_path).expanduser(),
        Path(instance.team_definition_path).expanduser(),
    ]
    workspace = Path(instance.workspace_path).expanduser()
    if not instance.runtime_state_path.is_relative_to(workspace):
        ordered.append(instance.runtime_state_path)
    ordered.extend(Path(path).expanduser() for path in instance.backup_policy.paths)

    unique: list[Path] = []
    seen: set[str] = set()
    for source in ordered:
        key = str(source)
        if key not in seen:
            seen.add(key)
            unique.append(source)
    return unique


def backup_instance(instance: InstanceSpec, output_dir: Path, execute: bool = False) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_path = output_dir / f"{instance.name}-{stamp}.tar.gz"

    if not execute:
        return archive_path

    warned_paths: set[Path] = set()

    def _safe_add(
        archive: tarfile.TarFile,
        source_path: Path,
        arcname: str,
        skip_legacy_workspace_runtime: bool = False,
    ) -> None:
        try:
            archive.add(source_path, arcname=arcname, recursive=False)
        except (PermissionError, OSError) as exc:
            if source_path not in warned_paths:
                warned_paths.add(source_path)
                logger.warning(
                    "Skipping unreadable backup path %s for instance %s: %s",
                    source_path,
                    instance.name,
                    exc,
                )
            return

        if not source_path.is_dir():
            return

        try:
            children = sorted(source_path.iterdir(), key=lambda item: item.name)
        except (PermissionError, OSError) as exc:
            if source_path not in warned_paths:
                warned_paths.add(source_path)
                logger.warning(
                    "Cannot list backup directory %s for instance %s: %s",
                    source_path,
                    instance.name,
                    exc,
                )
            return

        for child in children:
            if skip_legacy_workspace_runtime and child.name == ".clawake":
                logger.info("Skipping legacy workspace runtime path during backup: %s", child)
                continue
            _safe_add(
                archive,
                child,
                f"{arcname}/{child.name}",
                skip_legacy_workspace_runtime=skip_legacy_workspace_runtime,
            )

    with tarfile.open(archive_path, "w:gz") as archive:
        for idx, source in enumerate(backup_sources(instance), start=1):
            if source.exists():
                mount_identifier = source.name or f"path_{idx}"
                _safe_add(
                    archive,
                    source,
                    arcname=f"{idx:02d}__{mount_identifier}",
                    skip_legacy_workspace_runtime=(
                        source == Path(instance.workspace_path).expanduser()
                    ),
                )

    return archive_path


def prune_backups(output_dir: Path, prefix: str, retention: int) -> list[Path]:
    """Remove older matching backups while retaining the newest requested count."""
    candidates = sorted(
        (path for path in output_dir.glob(f"{prefix}*") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed = candidates[retention:]
    for path in removed:
        path.unlink()
    return removed
