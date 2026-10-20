"""Runtime pin overlay (ADR-004).

A v1 inventory may reference a private, Clawake-owned JSON file via ``runtime_pins``. The
overlay carries per-rollout values only: image tag/digest pins and archive plugin pins. It is
applied to the raw inventory data after ``${...}`` expansion and before model validation, so
every existing validator sees the effective data. Conflicting values fail closed.

This module has no import-time dependency on ``clawake.config``; the high-level API imports
it lazily because ``config.load_inventory`` uses the pure functions below.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import re
import stat
import tempfile
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

PINS_VERSION = 1
IMAGE_FIELDS = ("tag", "digest", "known_good_tag", "known_good_digest")
IMAGE_DIGEST_FIELDS = ("digest", "known_good_digest")
PLUGIN_FIELDS = ("artifact_path", "sha256")
TOP_LEVEL_KEYS = ("version", "cluster", "members")
MEMBER_KEYS = ("image", "plugins")
BACKUP_DIRNAME = "pins-backups"
BACKUP_RETENTION = 20
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_BACKUP_NAME = re.compile(r"\d{8}T\d{6}\.\d{6}Z(?:-\d+)?\.json")
_NPM_FIELDS = ("npm_spec", "integrity")

Clock = Callable[[], datetime]
Source = Literal["inventory", "overlay"]


class RuntimePinsError(ValueError):
    """The runtime pin overlay is missing, unsafe, malformed or conflicts with the inventory."""


class RuntimePinsWarning(UserWarning):
    """Non-fatal overlay finding, for example a group- or world-readable file."""


# --- reference and reading ------------------------------------------------------------------


def resolve_pins_path(reference: str, inventory_path: Path) -> Path:
    """Resolve an already expanded ``runtime_pins`` reference against the inventory directory."""
    path = Path(reference).expanduser()
    if not path.is_absolute():
        path = inventory_path.resolve().parent / path
    return Path(os.path.normpath(path))


def read_private_json(
    path: Path,
    *,
    label: str,
    error: type[ValueError],
    warning: type[UserWarning],
) -> object:
    """Read an operator- or Clawake-owned private JSON file with the ADR-004 safety rules.

    Refuses group/world-writable and non-regular files, warns on group/world-readable ones.
    Error messages name the file, never its content.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError as exc:
        raise error(f"{label} {path} does not exist") from exc
    except OSError as exc:
        raise error(f"cannot open {label} {path}: {exc.strerror}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise error(f"{label} {path} is not a regular file")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise error(
                f"{label} {path} is group- or world-writable (mode {mode:04o}); "
                f"run: chmod 600 {path}"
            )
        if mode & 0o044:
            warnings.warn(
                f"{label} {path} is group- or world-readable (mode {mode:04o}); "
                f"run: chmod 600 {path}",
                warning,
                stacklevel=3,
            )
        raw = handle.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        position = f" at line {exc.lineno}" if isinstance(exc, json.JSONDecodeError) else ""
        raise error(f"{label} {path}: invalid JSON{position}") from None


def read_overlay_file(path: Path) -> object:
    """Read and parse the overlay; refuse group/world-writable files, warn on readable ones."""
    return read_private_json(
        path, label="runtime pins file", error=RuntimePinsError, warning=RuntimePinsWarning
    )


# --- validation -----------------------------------------------------------------------------


def _instances_by_name(raw: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    instances = raw.get("instances", [])
    if not isinstance(instances, list):
        raise RuntimePinsError("inventory instances must be a list to apply runtime pins")
    result: dict[str, Mapping[str, Any]] = {}
    for item in instances:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            result[item["name"]] = item
    return result


def _declared_plugins(member: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    plugins = member.get("plugins", [])
    if not isinstance(plugins, list):
        return {}
    return {
        plugin["id"]: plugin
        for plugin in plugins
        if isinstance(plugin, Mapping) and isinstance(plugin.get("id"), str)
    }


def _require_mapping(value: object, source: Path, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimePinsError(f"runtime pins file {source}: {location} must be an object")
    return value


def _reject_unknown(
    value: Mapping[str, Any], allowed: tuple[str, ...], source: Path, prefix: str
) -> None:
    for key in value:
        if key not in allowed:
            location = f"{prefix}.{key}" if prefix else str(key)
            raise RuntimePinsError(
                f"runtime pins file {source}: {location}: unknown key "
                f"(allowed: {', '.join(allowed)})"
            )


def _require_string(value: object, source: Path, location: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value:
        raise RuntimePinsError(
            f"runtime pins file {source}: {location} must be a non-empty single-line string"
        )
    return value


def _require_digest(value: object, source: Path, location: str) -> str:
    text = _require_string(value, source, location)
    if not _DIGEST.fullmatch(text):
        raise RuntimePinsError(
            f"runtime pins file {source}: {location} must be a complete digest "
            "'sha256:<64 lowercase hex>'"
        )
    return text


def validate_overlay(data: object, raw: Mapping[str, Any], source: Path) -> dict[str, Any]:
    """Validate overlay data against the raw (expanded) inventory; return a normalized copy."""
    top = _require_mapping(data, source, "top level")
    _reject_unknown(top, TOP_LEVEL_KEYS, source, "")
    version = top.get("version")
    if isinstance(version, bool) or version != PINS_VERSION:
        raise RuntimePinsError(
            f"runtime pins file {source}: version must be {PINS_VERSION}, got {version!r}"
        )
    cluster = raw.get("cluster")
    expected_cluster = cluster.get("name") if isinstance(cluster, Mapping) else None
    if "cluster" not in top:
        raise RuntimePinsError(f"runtime pins file {source}: cluster is required")
    if top["cluster"] != expected_cluster:
        raise RuntimePinsError(
            f"runtime pins file {source}: cluster {top['cluster']!r} does not match "
            f"inventory cluster.name {expected_cluster!r}"
        )
    normalized: dict[str, Any] = {"version": PINS_VERSION, "cluster": expected_cluster}
    members = _require_mapping(top.get("members", {}), source, "members")
    declared = _instances_by_name(raw)
    normalized_members: dict[str, Any] = {}
    for name, entry in members.items():
        prefix = f"members.{name}"
        if name not in declared:
            raise RuntimePinsError(f"runtime pins file {source}: {prefix}: unknown member")
        entry = _require_mapping(entry, source, prefix)
        _reject_unknown(entry, MEMBER_KEYS, source, prefix)
        member_result: dict[str, Any] = {}
        if "image" in entry:
            image = _require_mapping(entry["image"], source, f"{prefix}.image")
            _reject_unknown(image, IMAGE_FIELDS, source, f"{prefix}.image")
            image_result: dict[str, str] = {}
            for key, value in image.items():
                location = f"{prefix}.image.{key}"
                if key in IMAGE_DIGEST_FIELDS:
                    image_result[key] = _require_digest(value, source, location)
                else:
                    text = _require_string(value, source, location)
                    if not _TAG.fullmatch(text):
                        raise RuntimePinsError(
                            f"runtime pins file {source}: {location} is not a valid image tag"
                        )
                    image_result[key] = text
            member_result["image"] = image_result
        if "plugins" in entry:
            plugins = _require_mapping(entry["plugins"], source, f"{prefix}.plugins")
            declared_plugins = _declared_plugins(declared[name])
            plugin_results: dict[str, dict[str, str]] = {}
            for plugin_id, pin in plugins.items():
                location = f"{prefix}.plugins.{plugin_id}"
                if plugin_id not in declared_plugins:
                    raise RuntimePinsError(
                        f"runtime pins file {source}: {location}: plugin not declared on "
                        f"member {name} in the inventory"
                    )
                if any(declared_plugins[plugin_id].get(key) is not None for key in _NPM_FIELDS):
                    raise RuntimePinsError(
                        f"runtime pins file {source}: {location}: npm plugins cannot be pinned "
                        "in the overlay; only archive plugins (artifact_path + sha256)"
                    )
                pin = _require_mapping(pin, source, location)
                _reject_unknown(pin, PLUGIN_FIELDS, source, location)
                pin_result: dict[str, str] = {}
                for key, value in pin.items():
                    if key == "sha256":
                        pin_result[key] = _require_digest(value, source, f"{location}.{key}")
                    else:
                        pin_result[key] = _require_string(value, source, f"{location}.{key}")
                plugin_results[plugin_id] = pin_result
            member_result["plugins"] = plugin_results
        normalized_members[name] = member_result
    normalized["members"] = normalized_members
    return normalized


# --- merge ----------------------------------------------------------------------------------


def _set_or_conflict(
    target: dict[str, Any],
    key: str,
    value: str,
    location: str,
    inventory_source: Path,
    overlay_source: Path,
) -> None:
    existing = target.get(key)
    if existing is not None and existing != value:
        raise RuntimePinsError(
            f"runtime pins conflict: {location} is {existing!r} in inventory {inventory_source} "
            f"but {value!r} in runtime pins {overlay_source}; remove it from one of them"
        )
    target[key] = value


def apply_overlay(
    raw: Mapping[str, Any],
    overlay: Mapping[str, Any],
    *,
    inventory_source: Path,
    overlay_source: Path,
) -> dict[str, Any]:
    """Return a deep copy of raw inventory data with a validated overlay applied."""
    merged = copy.deepcopy(dict(raw))
    instances = _instances_by_name(merged)
    for name, entry in overlay.get("members", {}).items():
        member = instances[name]
        assert isinstance(member, dict)
        if entry.get("image"):
            image = member.setdefault("image", {})
            if not isinstance(image, dict):
                raise RuntimePinsError(f"instances[{name}].image must be a mapping")
            for key, value in entry["image"].items():
                _set_or_conflict(
                    image,
                    key,
                    value,
                    f"instances[{name}].image.{key}",
                    inventory_source,
                    overlay_source,
                )
        plugins = _declared_plugins(member)
        for plugin_id, pin in entry.get("plugins", {}).items():
            plugin = plugins[plugin_id]
            assert isinstance(plugin, dict)
            for key, value in pin.items():
                _set_or_conflict(
                    plugin,
                    key,
                    value,
                    f"instances[{name}].plugins[{plugin_id}].{key}",
                    inventory_source,
                    overlay_source,
                )
    return merged


def count_fields(overlay: Mapping[str, Any]) -> tuple[int, int]:
    """Return (members, pinned fields) of a validated overlay."""
    members = overlay.get("members", {})
    fields = 0
    for entry in members.values():
        fields += len(entry.get("image", {}))
        fields += sum(len(pin) for pin in entry.get("plugins", {}).values())
    return len(members), fields


# --- writing --------------------------------------------------------------------------------


def render_overlay(data: Mapping[str, Any]) -> str:
    """Deterministic on-disk representation of an overlay."""
    return json.dumps(data, indent=2) + "\n"


def _refuse_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise RuntimePinsError(f"refusing to write {label} {path}: it is a symlink")


def _write_private_file(path: Path, content: bytes) -> None:
    """Create a new 0600 file without following symlinks; fail if it exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _read_previous(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def _backup_previous(path: Path, now: Clock) -> Path:
    backup_dir = path.parent / BACKUP_DIRNAME
    _refuse_symlink(backup_dir, "runtime pins backup directory")
    previous = _read_previous(path)
    backup_dir.mkdir(mode=DIRECTORY_MODE, exist_ok=True)
    if not backup_dir.is_dir():
        raise RuntimePinsError(f"runtime pins backup path {backup_dir} is not a directory")
    stamp = now().astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt}"
        candidate = backup_dir / f"{stamp}{suffix}.json"
        try:
            _write_private_file(candidate, previous)
        except FileExistsError:
            continue
        return candidate
    raise RuntimePinsError(f"cannot allocate a unique backup name in {backup_dir}")


def _prune_backups(backup_dir: Path, keep: int = BACKUP_RETENTION) -> list[Path]:
    backups = sorted(
        entry
        for entry in backup_dir.iterdir()
        if _BACKUP_NAME.fullmatch(entry.name) and entry.is_file() and not entry.is_symlink()
    )
    removed = backups[:-keep] if len(backups) > keep else []
    for entry in removed:
        entry.unlink()
    return removed


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError as exc:
        # Some filesystems refuse fsync on directories; the rename itself already happened.
        if exc.errno not in {errno.EINVAL, errno.EBADF}:
            raise
    finally:
        os.close(descriptor)


def write_overlay(
    path: Path,
    data: Mapping[str, Any],
    *,
    now: Clock | None = None,
    create_only: bool = False,
) -> Path | None:
    """Atomically write the overlay (mode 0600) and back up the previous version.

    Returns the backup path, or ``None`` when no previous overlay existed. With
    ``create_only`` an existing file is never replaced and no backup is made.
    """
    clock = now or (lambda: datetime.now(UTC))
    _refuse_symlink(path, "runtime pins file")
    directory = path.parent
    if not directory.is_dir():
        raise RuntimePinsError(f"runtime pins directory {directory} does not exist")
    exists = os.path.lexists(path)
    if create_only and exists:
        raise RuntimePinsError(f"runtime pins file {path} already exists; refusing to overwrite")
    if exists and not path.is_file():
        raise RuntimePinsError(f"runtime pins file {path} is not a regular file")
    backup = _backup_previous(path, clock) if exists else None

    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(render_overlay(data).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            # link() fails instead of replacing a file that appeared after the check.
            try:
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise RuntimePinsError(
                    f"runtime pins file {path} already exists; refusing to overwrite"
                ) from exc
            temporary_path.unlink()
        else:
            os.replace(temporary_path, path)
    except BaseException:
        # Leave the previous overlay untouched and remove the partial file, then re-raise.
        temporary_path.unlink(missing_ok=True)
        raise
    _fsync_directory(directory)
    if backup is not None:
        _prune_backups(backup.parent)
    return backup


# --- high-level API (rollout uses plan_plugin_pins and set_plugin_pins) ------------------


@dataclass(frozen=True)
class PinRow:
    member: str
    field: str
    value: str
    source: Source
    also_in_inventory: bool = False


@dataclass(frozen=True)
class PinsReport:
    cluster: str
    pins_path: Path | None
    rows: list[PinRow] = field(default_factory=list)

    def by_member(self) -> dict[str, list[PinRow]]:
        result: dict[str, list[PinRow]] = {}
        for row in self.rows:
            result.setdefault(row.member, []).append(row)
        return result


def pin_rows(raw: Mapping[str, Any], overlay: Mapping[str, Any] | None) -> list[PinRow]:
    """Effective pin fields per member with their source, in inventory order."""
    members = (overlay or {}).get("members", {})
    rows: list[PinRow] = []
    for name, member in _instances_by_name(raw).items():
        entry = members.get(name, {})
        image = member.get("image") if isinstance(member.get("image"), Mapping) else {}
        overlay_image = entry.get("image", {})
        for key in IMAGE_FIELDS:
            rows.extend(_row(name, f"image.{key}", image.get(key), overlay_image.get(key)))
        overlay_plugins = entry.get("plugins", {})
        for plugin_id, plugin in _declared_plugins(member).items():
            if any(plugin.get(key) is not None for key in _NPM_FIELDS):
                continue
            pin = overlay_plugins.get(plugin_id, {})
            for key in PLUGIN_FIELDS:
                rows.extend(_row(name, f"plugins.{plugin_id}.{key}", plugin.get(key), pin.get(key)))
    return rows


def _row(member: str, name: str, inventory_value: object, overlay_value: object) -> list[PinRow]:
    if overlay_value is not None:
        return [PinRow(member, name, str(overlay_value), "overlay", inventory_value is not None)]
    if inventory_value is not None:
        return [PinRow(member, name, str(inventory_value), "inventory")]
    return []


def _layers(inventory_path: Path, *, allow_missing_pins: bool = False):
    # Lazy import: clawake.config imports this module for the merge step.
    from clawake.config import load_inventory_layers

    return load_inventory_layers(inventory_path, allow_missing_pins=allow_missing_pins)


def read_pins(inventory_path: Path) -> PinsReport:
    """Effective pins of a v1 inventory and the source of every field."""
    layers = _layers(Path(inventory_path))
    return PinsReport(
        cluster=layers.inventory.cluster.name,
        pins_path=layers.pins_path,
        rows=pin_rows(layers.raw, layers.overlay),
    )


def _validated_candidate(layers, new_overlay: dict[str, Any]):
    """Validate a prospective overlay; return it normalized plus the effective inventory.

    Same path as ``load_inventory_layers``: overlay validation, merge, model validation.
    """
    from clawake.config import Inventory

    assert layers.pins_path is not None
    normalized = validate_overlay(new_overlay, layers.raw, layers.pins_path)
    merged = apply_overlay(
        layers.raw,
        normalized,
        inventory_source=layers.inventory_path,
        overlay_source=layers.pins_path,
    )
    merged["runtime_pins"] = str(layers.pins_path)
    return normalized, Inventory.model_validate(merged)


def _validated_update(layers, new_overlay: dict[str, Any]) -> dict[str, Any]:
    """Validate a prospective overlay and the effective inventory it produces; no writes."""
    return _validated_candidate(layers, new_overlay)[0]


def _require_overlay(layers) -> dict[str, Any]:
    if layers.pins_path is None:
        raise RuntimePinsError(
            f"inventory {layers.inventory_path} does not declare runtime_pins; "
            "run 'clawake pins init' first"
        )
    return copy.deepcopy(layers.overlay)


def plan_image_pins(inventory_path: Path, member: str, updates: Mapping[str, str | None]):
    """Prospective overlay with new image fields for ``member``; validated, nothing written."""
    layers = _layers(Path(inventory_path))
    overlay = _require_overlay(layers)
    if any(value is None for value in updates.values()):
        missing = ", ".join(sorted(key for key, value in updates.items() if value is None))
        raise RuntimePinsError(f"runtime pins require a value for image field(s): {missing}")
    entry = overlay["members"].setdefault(member, {})
    entry.setdefault("image", {}).update(updates)
    return layers, _validated_update(layers, overlay)


def set_image_pins(
    inventory_path: Path,
    member: str,
    updates: Mapping[str, str | None],
    *,
    now: Clock | None = None,
) -> Path:
    """Write image fields for ``member`` to the overlay; returns the backup of the old overlay."""
    layers, overlay = plan_image_pins(inventory_path, member, updates)
    backup = write_overlay(layers.pins_path, overlay, now=now)
    assert backup is not None
    return backup


@dataclass(frozen=True)
class PluginPin:
    """One archive plugin pin as stored in the overlay."""

    artifact_path: str
    sha256: str


def plan_plugin_pins(layers, pins: Mapping[str, Mapping[str, PluginPin]]):
    """Overlay with new archive plugin pins per member, plus the effective inventory.

    Validated through the same path as ``load_inventory``; nothing is written. Returns
    ``(normalized_overlay, inventory)``.
    """
    overlay = _require_overlay(layers)
    for member, member_pins in pins.items():
        entry = overlay["members"].setdefault(member, {})
        plugins = entry.setdefault("plugins", {})
        for plugin_id, pin in member_pins.items():
            plugins[plugin_id] = {"artifact_path": pin.artifact_path, "sha256": pin.sha256}
    return _validated_candidate(layers, overlay)


def set_plugin_pins(
    inventory_path: Path,
    member: str,
    pins: Mapping[str, PluginPin],
    *,
    now: Clock | None = None,
) -> Path:
    """Pin archive plugins of ``member`` in one overlay write; returns the backup path.

    The prospective overlay and the resulting effective inventory are validated before the
    write, so an invalid pin never reaches disk. The inventory file is never touched.
    """
    layers = _layers(Path(inventory_path))
    normalized, _inventory = plan_plugin_pins(layers, {member: pins})
    backup = write_overlay(layers.pins_path, normalized, now=now)
    assert backup is not None
    return backup


def set_plugin_pin(
    inventory_path: Path,
    member: str,
    plugin_id: str,
    artifact_path: str,
    sha256: str,
    *,
    now: Clock | None = None,
) -> Path:
    """Pin one archive plugin of ``member`` in the overlay; returns the backup path."""
    return set_plugin_pins(
        inventory_path, member, {plugin_id: PluginPin(artifact_path, sha256)}, now=now
    )


def copy_overlay(path: Path, output_dir: Path, *, now: Clock | None = None) -> Path:
    """Copy the current overlay into ``output_dir`` as a new 0600 file; never overwrites."""
    clock = now or (lambda: datetime.now(UTC))
    _refuse_symlink(output_dir, "backup directory")
    previous = _read_previous(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt}"
        candidate = output_dir / f"{path.stem}-{stamp}{suffix}{path.suffix}"
        try:
            _write_private_file(candidate, previous)
        except FileExistsError:
            continue
        return candidate
    raise RuntimePinsError(f"cannot allocate a unique backup name in {output_dir}")


# --- migration ------------------------------------------------------------------------------


@dataclass(frozen=True)
class InitPlan:
    inventory_path: Path
    output: Path
    overlay: dict[str, Any]
    removals: list[str]
    reference_line: str
    already_declared: Path | None

    @property
    def content(self) -> str:
        return render_overlay(self.overlay)


def plan_init(inventory_path: Path, output: Path) -> InitPlan:
    """Extract the current pins of an inventory into a new overlay; nothing is written."""
    from clawake.config import Inventory

    inventory_path = Path(inventory_path)
    output = Path(os.path.abspath(Path(output).expanduser()))
    layers = _layers(inventory_path, allow_missing_pins=True)
    if layers.inventory.version != 1:
        raise RuntimePinsError("runtime pins require inventory version 1")
    if os.path.lexists(output):
        raise RuntimePinsError(f"runtime pins file {output} already exists; refusing to overwrite")
    if not output.parent.is_dir():
        raise RuntimePinsError(
            f"output directory {output.parent} does not exist; create it with mode 0700 first"
        )
    effective = layers.raw if layers.overlay is None else layers.merged
    members: dict[str, Any] = {}
    removals: list[str] = []
    raw_instances = _instances_by_name(layers.raw)
    for name, member in _instances_by_name(effective).items():
        raw_member = raw_instances[name]
        entry: dict[str, Any] = {}
        image = member.get("image") if isinstance(member.get("image"), Mapping) else {}
        raw_image = raw_member.get("image") if isinstance(raw_member.get("image"), Mapping) else {}
        image_pins = {key: image[key] for key in IMAGE_FIELDS if image.get(key) is not None}
        if image_pins:
            entry["image"] = image_pins
        removals.extend(
            f"instances[{name}].image.{key}"
            for key in IMAGE_FIELDS
            if raw_image.get(key) is not None
        )
        plugins: dict[str, Any] = {}
        raw_plugins = _declared_plugins(raw_member)
        for plugin_id, plugin in _declared_plugins(member).items():
            if any(plugin.get(key) is not None for key in _NPM_FIELDS):
                continue
            pin = {key: plugin[key] for key in PLUGIN_FIELDS if plugin.get(key) is not None}
            if pin:
                plugins[plugin_id] = pin
            removals.extend(
                f"instances[{name}].plugins[{plugin_id}].{key}"
                for key in PLUGIN_FIELDS
                if raw_plugins[plugin_id].get(key) is not None
            )
        if plugins:
            entry["plugins"] = plugins
        if entry:
            members[name] = entry
    overlay = {"version": PINS_VERSION, "cluster": layers.inventory.cluster.name}
    overlay["members"] = members
    normalized = validate_overlay(overlay, layers.raw, output)

    # Self-check: the split inventory plus this overlay must equal today's effective data.
    split = _strip_pins(layers.raw)
    merged = apply_overlay(
        split, normalized, inventory_source=inventory_path, overlay_source=output
    )
    merged.pop("runtime_pins", None)
    current = dict(effective)
    current.pop("runtime_pins", None)
    if Inventory.model_validate(merged) != Inventory.model_validate(current):
        raise RuntimePinsError("internal error: the extracted overlay does not reproduce the pins")

    if inventory_path.suffix == ".json":
        reference_line = f'"runtime_pins": {json.dumps(str(output))}'
    else:
        reference_line = f"runtime_pins: {output}"
    return InitPlan(
        inventory_path=inventory_path,
        output=output,
        overlay=normalized,
        removals=removals,
        reference_line=reference_line,
        already_declared=layers.pins_path,
    )


def _strip_pins(raw: Mapping[str, Any]) -> dict[str, Any]:
    split = copy.deepcopy(dict(raw))
    for member in _instances_by_name(split).values():
        assert isinstance(member, dict)
        image = member.get("image")
        if isinstance(image, dict):
            for key in IMAGE_FIELDS:
                image.pop(key, None)
        for plugin in _declared_plugins(member).values():
            assert isinstance(plugin, dict)
            if any(plugin.get(key) is not None for key in _NPM_FIELDS):
                continue
            for key in PLUGIN_FIELDS:
                plugin.pop(key, None)
    return split


def execute_init(plan: InitPlan) -> None:
    """Write the planned overlay as a new file; never overwrites and never edits the inventory."""
    write_overlay(plan.output, plan.overlay, create_only=True)
