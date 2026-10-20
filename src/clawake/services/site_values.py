"""Private site values and whole-string ``${site:<name>}`` references (ADR-005 items 1, 2).

A v1 inventory may reference an operator-written JSON file via ``site_values``. It carries
private, non-secret site data (for example Discord IDs) by name, so the versioned inventory
only contains names. Any inventory string that is exactly ``${site:<name>}`` is replaced by
the value after ``${VAR}`` expansion and before the runtime pin overlay and model validation.

Clawake never writes the site values file: this module has no write API. Error messages
name inventory paths and site value names, never values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from clawake.services.runtime_pins import read_private_json

SITE_VERSION = 1
TOP_LEVEL_KEYS = ("version", "cluster", "values")
REFERENCE_MARKER = "${site:"
SITE_VALUES_V1_ONLY = (
    "site_values is only supported in inventory version 1; v2 rollout does not exist yet"
)
FILE_LABEL = "site values file"

_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_REFERENCE = re.compile(r"\$\{site:([^}]*)\}")

SiteValue = str | list[str]
Location = tuple[str | int, ...]


class SiteValuesError(ValueError):
    """The site values file or a ``${site:…}`` reference is missing, unsafe or malformed."""


class SiteValuesWarning(UserWarning):
    """Non-fatal site values finding, for example a group- or world-readable file."""


@dataclass(frozen=True)
class SiteReference:
    """One substituted reference: where it was in the inventory and which name it used."""

    location: Location
    label: str
    name: str


@dataclass(frozen=True)
class SiteSubstitution:
    """The loaded site values file and the references substituted from it."""

    path: Path
    # Private, non-secret values; kept out of repr so they never reach logs by accident.
    values: Mapping[str, SiteValue] = field(repr=False)
    references: tuple[SiteReference, ...] = ()

    @property
    def value_count(self) -> int:
        return len(self.values)

    @property
    def unused(self) -> tuple[str, ...]:
        used = {reference.name for reference in self.references}
        return tuple(sorted(name for name in self.values if name not in used))


# --- file -----------------------------------------------------------------------------------


def read_site_values_file(path: Path) -> object:
    """Read the operator-written file; refuse group/world-writable, warn if readable."""
    return read_private_json(
        path, label=FILE_LABEL, error=SiteValuesError, warning=SiteValuesWarning
    )


def validate_site_values(
    data: object, raw: Mapping[str, Any], source: Path
) -> dict[str, SiteValue]:
    """Validate the file content against the (expanded) inventory; return the values."""
    prefix = f"{FILE_LABEL} {source}"
    if not isinstance(data, Mapping):
        raise SiteValuesError(f"{prefix}: top level must be an object")
    for key in data:
        if key not in TOP_LEVEL_KEYS:
            raise SiteValuesError(
                f"{prefix}: {key}: unknown key (allowed: {', '.join(TOP_LEVEL_KEYS)})"
            )
    version = data.get("version")
    if isinstance(version, bool) or version != SITE_VERSION:
        raise SiteValuesError(f"{prefix}: version must be {SITE_VERSION}")
    cluster = raw.get("cluster")
    expected_cluster = cluster.get("name") if isinstance(cluster, Mapping) else None
    if "cluster" not in data:
        raise SiteValuesError(f"{prefix}: cluster is required")
    if data["cluster"] != expected_cluster:
        raise SiteValuesError(
            f"{prefix}: cluster {data['cluster']!r} does not match inventory cluster.name "
            f"{expected_cluster!r}"
        )
    if "values" not in data:
        raise SiteValuesError(f"{prefix}: values is required")
    values = data["values"]
    if not isinstance(values, Mapping):
        raise SiteValuesError(f"{prefix}: values must be an object")
    result: dict[str, SiteValue] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SiteValuesError(
                f"{prefix}: values.{name}: invalid site value name (expected {_NAME.pattern})"
            )
        if isinstance(value, str):
            result[name] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[name] = list(value)
        else:
            raise SiteValuesError(f"{prefix}: values.{name} must be a string or a list of strings")
    return result


# --- references -----------------------------------------------------------------------------


def _item_label(parent_key: object, item: object, index: int) -> str:
    if isinstance(item, Mapping):
        for key in ("name", "id") if parent_key in ("instances", "services", "plugins") else ():
            value = item.get(key)
            if isinstance(value, str) and REFERENCE_MARKER not in value:
                return f"[{value}]"
    return f"[{index}]"


class _Substituter:
    def __init__(self, values: Mapping[str, SiteValue] | None, source: Path | None) -> None:
        self.values = values
        self.source = source
        self.references: list[SiteReference] = []

    def walk(self, node: object, location: Location, label: str) -> object:
        if isinstance(node, str):
            return self._string(node, location, label)
        if isinstance(node, list):
            parent = location[-1] if location else None
            return [
                self.walk(item, (*location, index), label + _item_label(parent, item, index))
                for index, item in enumerate(node)
            ]
        if isinstance(node, Mapping):
            result = {}
            for key, item in node.items():
                child = f"{label}.{key}" if label else str(key)
                if isinstance(key, str) and REFERENCE_MARKER in key:
                    raise SiteValuesError(f"{child}: mapping keys cannot be site value references")
                result[key] = self.walk(item, (*location, key), child)
            return result
        return node

    def _string(self, text: str, location: Location, label: str) -> object:
        if REFERENCE_MARKER not in text:
            return text
        match = _REFERENCE.fullmatch(text)
        if match is None:
            raise SiteValuesError(
                f"{label}: '{REFERENCE_MARKER}' is only allowed as a whole-string reference "
                "'${site:<name>}'; partial references are not substituted"
            )
        name = match.group(1)
        if not _NAME.fullmatch(name):
            raise SiteValuesError(
                f"{label}: invalid site value name '{name}' (expected {_NAME.pattern})"
            )
        if self.values is None:
            raise SiteValuesError(
                f"{label}: references site value '{name}' but the inventory declares no site_values"
            )
        if name not in self.values:
            raise SiteValuesError(
                f"{label}: unknown site value '{name}' (not defined in {FILE_LABEL} {self.source})"
            )
        self.references.append(SiteReference(location, label, name))
        value = self.values[name]
        return list(value) if isinstance(value, list) else value


def substitute_references(
    raw: Mapping[str, Any],
    values: Mapping[str, SiteValue] | None,
    source: Path | None,
) -> tuple[dict[str, Any], tuple[SiteReference, ...]]:
    """Return a copy of raw inventory data with every ``${site:<name>}`` replaced.

    ``values=None`` means the inventory declares no site values: any reference is an error.
    """
    substituter = _Substituter(values, source)
    result = substituter.walk(raw, (), "")
    assert isinstance(result, dict)
    return result, tuple(substituter.references)


def explain_validation_error(
    exc: ValidationError, references: Sequence[SiteReference]
) -> str | None:
    """Name the site values (never their content) involved in a model validation error."""
    hints: list[str] = []
    for error in exc.errors():
        loc = tuple(error["loc"])
        if len(loc) < 3:
            continue
        for reference in references:
            inside = reference.location[: len(loc)] == loc
            within = loc[: len(reference.location)] == reference.location
            if inside or within:
                hint = f"site value '{reference.name}' at {reference.label}"
                if hint not in hints:
                    hints.append(hint)
    if not hints:
        return None
    return "Involved site values (names only): " + "; ".join(hints)
