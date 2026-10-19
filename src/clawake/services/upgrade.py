from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from clawake.services import runtime_pins


@dataclass
class UpgradePlan:
    instance: str
    previous_tag: str
    previous_digest: str | None
    next_tag: str
    next_digest: str | None
    # ADR-004: backup of the previous runtime pin overlay when the overlay was written.
    pins_backup: Path | None = None


def _load_config(config_path: Path) -> dict:
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Failed to read config file {config_path}: {exc}") from exc

    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {config_path}: {exc}") from exc


def _find_instance(config: dict, instance_name: str) -> dict:
    for instance in config.get("instances", []):
        if instance.get("name") == instance_name:
            return instance
    raise ValueError(f"Unknown instance '{instance_name}'")


def _format_image_value(key: str, value: str) -> str:
    if not isinstance(value, str):
        # Never render Python's ``None`` (or any non-string) into an inventory (#27).
        raise ValueError(f"image.{key} must be a string, got {value!r}")
    return json.dumps(value) if key.endswith("tag") else value


def _write_text_atomic(path: Path, text: str) -> None:
    """Replace ``path`` atomically and keep its permission bits; never leave a partial file."""
    mode = path.stat().st_mode & 0o7777
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        # Leave the previous inventory untouched and remove the partial file, then re-raise.
        temporary_path.unlink(missing_ok=True)
        raise


def _json_document(raw: str) -> dict | None:
    """The parsed document if the inventory is JSON, else ``None`` (YAML)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _update_json_image_fields(
    config_path: Path,
    data: dict,
    instance_name: str,
    updates: Mapping[str, str],
    section: str,
) -> None:
    """Rewrite a JSON inventory as JSON: original key order, 2-space indent (#27)."""
    entries = data.get(section)
    if not isinstance(entries, list):
        raise ValueError(f"Inventory {config_path} has no '{section}' list")
    selected = next(
        (item for item in entries if isinstance(item, dict) and item.get("name") == instance_name),
        None,
    )
    if selected is None:
        raise ValueError(f"Unknown instance '{instance_name}'")
    image = selected.get("image")
    if not isinstance(image, dict):
        raise ValueError(f"Instance '{instance_name}' does not define image")
    image.update(updates)
    _write_text_atomic(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _update_image_fields(
    config_path: Path,
    instance_name: str,
    updates: Mapping[str, str | None],
    *,
    section: str = "instances",
) -> None:
    """Update one image mapping without reformatting the rest of the document.

    JSON inventories stay JSON; YAML inventories keep their formatting. A ``None`` value is
    never written: the key is left as it is (#27).
    """
    values = {key: value for key, value in updates.items() if value is not None}
    raw = config_path.read_text(encoding="utf-8")
    json_data = _json_document(raw)
    if json_data is not None:
        _update_json_image_fields(config_path, json_data, instance_name, values, section)
        return
    lines = raw.splitlines(keepends=True)
    document = yaml.compose(raw)
    instances = next(value for key, value in document.value if key.value == section)
    selected = next(
        (
            node
            for node in instances.value
            if any(
                key.value == "name" and value.value == instance_name for key, value in node.value
            )
        ),
        None,
    )
    if selected is None:
        raise ValueError(f"Unknown instance '{instance_name}'")
    image_pair = next(((key, value) for key, value in selected.value if key.value == "image"), None)
    if image_pair is None:
        raise ValueError(f"Instance '{instance_name}' does not define image")
    image_key, image_node = image_pair
    # Editing an alias would also modify another instance's image.
    if any(
        value is image_node
        for node in instances.value
        if node is not selected
        for key, value in node.value
        if key.value == "image"
    ):
        raise ValueError("Shared YAML image aliases must be expanded before upgrade")
    if image_node.flow_style:
        data = yaml.safe_load(raw)
        image = dict(next(item for item in data[section] if item["name"] == instance_name)["image"])
        image.update(values)
        replacement = yaml.safe_dump(image, default_flow_style=True, sort_keys=False).strip()
        config_path.write_text(
            raw[: image_node.start_mark.index] + replacement + raw[image_node.end_mark.index :],
            encoding="utf-8",
        )
        return
    image_index = image_key.start_mark.line
    instance_end = selected.end_mark.line

    image_indent = len(lines[image_index]) - len(lines[image_index].lstrip())
    child_indent = image_indent + 2
    image_end = instance_end
    positions: dict[str, int] = {}
    field_pattern = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):")
    for index in range(image_index + 1, instance_end):
        stripped = lines[index].lstrip()
        if stripped and len(lines[index]) - len(stripped) <= image_indent:
            image_end = index
            break
        match = field_pattern.match(lines[index])
        if match and len(match.group(1)) == child_indent:
            positions[match.group(2)] = index

    for key, value in values.items():
        rendered = f"{' ' * child_indent}{key}: {_format_image_value(key, value)}\n"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            lines.insert(image_end, rendered)
            image_end += 1
            positions = {
                existing_key: existing_index + (1 if existing_index >= image_end - 1 else 0)
                for existing_key, existing_index in positions.items()
            }

    config_path.write_text("".join(lines), encoding="utf-8")


def _plan_from_image(
    instance_name: str,
    image: dict,
    next_tag: str | None,
    next_digest: str | None,
) -> UpgradePlan:
    return UpgradePlan(
        instance=instance_name,
        previous_tag=image["tag"],
        previous_digest=image.get("digest"),
        next_tag=next_tag or image["tag"],
        next_digest=next_digest if next_digest is not None else image.get("digest"),
    )


def _declares_runtime_pins(config: object) -> bool:
    return isinstance(config, dict) and config.get("runtime_pins") is not None


def _effective_image(config_path: Path, instance_name: str) -> dict:
    """Image mapping of one member after the runtime pin overlay was applied (ADR-004)."""
    # Lazy import: config imports services.runtime_pins, which this module also uses.
    from clawake.config import load_inventory_layers

    layers = load_inventory_layers(config_path)
    return dict(_find_instance(layers.merged, instance_name)["image"])


def build_upgrade_plan(
    config_path: Path,
    instance_name: str,
    next_tag: str | None,
    next_digest: str | None,
) -> UpgradePlan:
    config = _load_config(config_path)
    if _declares_runtime_pins(config):
        image = _effective_image(config_path, instance_name)
        return _plan_from_image(instance_name, image, next_tag, next_digest)
    instance = _find_instance(config, instance_name)
    return _plan_from_image(instance_name, instance["image"], next_tag, next_digest)


def plan_overlay_upgrade(
    config_path: Path,
    instance_name: str,
    next_tag: str | None,
    next_digest: str | None,
) -> Path | None:
    """Validate the overlay write of an upgrade without writing; returns the overlay path.

    Returns ``None`` when the inventory does not declare ``runtime_pins``.
    """
    if not _declares_runtime_pins(_load_config(config_path)):
        return None
    image = _effective_image(config_path, instance_name)
    plan = _plan_from_image(instance_name, image, next_tag, next_digest)
    layers, _overlay = runtime_pins.plan_image_pins(
        config_path, instance_name, _upgrade_updates(plan, image)
    )
    return layers.pins_path


def _upgrade_updates(plan: UpgradePlan, image: dict) -> dict[str, str | None]:
    updates = {
        "tag": plan.next_tag,
        "digest": plan.next_digest,
        "known_good_tag": image["tag"],
    }
    if image.get("digest"):
        updates["known_good_digest"] = image["digest"]
    return updates


def apply_upgrade(
    config_path: Path,
    instance_name: str,
    next_tag: str | None,
    next_digest: str | None,
) -> UpgradePlan:
    config = _load_config(config_path)
    if _declares_runtime_pins(config):
        # ADR-004: write the overlay, never the inventory file.
        image = _effective_image(config_path, instance_name)
        plan = _plan_from_image(instance_name, image, next_tag, next_digest)
        plan.pins_backup = runtime_pins.set_image_pins(
            config_path, instance_name, _upgrade_updates(plan, image)
        )
        return plan
    instance = _find_instance(config, instance_name)
    image = instance["image"]

    plan = _plan_from_image(instance_name, image, next_tag, next_digest)

    updates = {
        "tag": plan.next_tag,
        "digest": plan.next_digest,
        "known_good_tag": image["tag"],
    }
    if image.get("digest"):
        updates["known_good_digest"] = image["digest"]

    _update_image_fields(config_path, instance_name, updates)
    return plan


def rollback_to_known_good(config_path: Path, instance_name: str) -> UpgradePlan:
    config = _load_config(config_path)
    overlay = _declares_runtime_pins(config)
    if overlay:
        image = _effective_image(config_path, instance_name)
    else:
        instance = _find_instance(config, instance_name)
        image = instance["image"]
    known_good = image.get("known_good_digest")
    if not known_good:
        raise ValueError(f"Instance '{instance_name}' does not define image.known_good_digest")

    known_good_tag = image.get("known_good_tag")
    if not known_good_tag:
        raise ValueError(f"Instance '{instance_name}' does not define image.known_good_tag")

    plan = _plan_from_image(instance_name, image, known_good_tag, known_good)

    if overlay:
        # ADR-004: write the overlay, never the inventory file.
        plan.pins_backup = runtime_pins.set_image_pins(
            config_path, instance_name, {"tag": known_good_tag, "digest": known_good}
        )
        return plan
    _update_image_fields(
        config_path,
        instance_name,
        {"tag": known_good_tag, "digest": known_good},
    )
    return plan


def backup_config(config_path: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # Keep the original suffix: a JSON inventory is backed up as .json (#27).
    backup_path = output_dir / f"{config_path.stem}-{stamp}{config_path.suffix}"
    backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup_path
