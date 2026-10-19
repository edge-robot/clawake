"""Runtime pin overlay (ADR-004): loading, merge, validation, file safety and writers.

All values are synthetic. Every file lives below tmp_path.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from clawake.cli import app
from clawake.config import load_inventory, load_inventory_layers
from clawake.services import runtime_pins
from clawake.services.runtime_pins import (
    BACKUP_DIRNAME,
    BACKUP_RETENTION,
    RuntimePinsError,
    RuntimePinsWarning,
    read_pins,
    set_plugin_pin,
    write_overlay,
)
from clawake.services.upgrade import apply_upgrade, build_upgrade_plan, rollback_to_known_good

runner = CliRunner()

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
SHA_P = "sha256:" + "1" * 64
SHA_Q = "sha256:" + "2" * 64
IMAGE_KEYS = ("tag", "digest", "known_good_tag", "known_good_digest")
PLUGIN_KEYS = ("artifact_path", "sha256")


def _member(name: str, root: Path, port: int) -> dict:
    return {
        "name": name,
        "host": "example-host",
        "role": "developer",
        "workspace_path": str(root / name / "workspace"),
        "state_path": str(root / name / "state"),
        "team_definition_path": str(root / "roles" / name),
        "quadlet_path": f"{name}.container",
        "container_name": name,
        "image": {
            "repository": "ghcr.io/example/openclaw",
            "tag": "2026.1.1",
            "digest": DIGEST_A,
            "known_good_tag": "2026.1.0",
            "known_good_digest": DIGEST_B,
        },
        "ports": [{"bind_address": "127.0.0.1", "host_port": port, "container_port": 18789}],
        "dashboard": {"friendly_name": name},
        "plugins": [
            {
                "id": "example-cad",
                "artifact_path": str(root / "packages" / "example-cad-0.0.1.tgz"),
                "sha256": SHA_P,
                "config": {"liveReads": False},
            },
            {
                "id": "example-npm",
                "npm_spec": "@example/tool@1.2.3",
                "integrity": "sha512-ZXhhbXBsZQ==",
            },
        ],
    }


def _full_raw(root: Path) -> dict:
    return {
        "version": 1,
        "cluster": {"name": "example-cad", "primary_host": "example-host"},
        "hosts": [{"name": "example-host", "quadlet_root": str(root / "quadlets")}],
        "instances": [
            _member("example-lead", root, 19001),
            _member("example-engineer", root, 19002),
        ],
    }


def _full_overlay(root: Path) -> dict:
    members = {}
    for name in ("example-lead", "example-engineer"):
        members[name] = {
            "image": {
                "tag": "2026.1.1",
                "digest": DIGEST_A,
                "known_good_tag": "2026.1.0",
                "known_good_digest": DIGEST_B,
            },
            "plugins": {
                "example-cad": {
                    "artifact_path": str(root / "packages" / "example-cad-0.0.1.tgz"),
                    "sha256": SHA_P,
                }
            },
        }
    return {"version": 1, "cluster": "example-cad", "members": members}


def _split_raw(root: Path, pins: str = "private/pins.json") -> dict:
    raw = _full_raw(root)
    raw["runtime_pins"] = pins
    for member in raw["instances"]:
        for key in IMAGE_KEYS:
            member["image"].pop(key)
        for key in PLUGIN_KEYS:
            member["plugins"][0].pop(key)
    return raw


def _write_json(path: Path, data: object, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _write_yaml(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _split_setup(tmp_path: Path, overlay: dict | None = None, raw: dict | None = None):
    inventory = _write_yaml(tmp_path / "team.yml", raw if raw is not None else _split_raw(tmp_path))
    pins = _write_json(
        tmp_path / "private" / "pins.json",
        overlay if overlay is not None else _full_overlay(tmp_path),
    )
    return inventory, pins


def _clock(start: datetime | None = None):
    current = [start or datetime(2026, 1, 1, tzinfo=UTC)]

    def now() -> datetime:
        current[0] += timedelta(seconds=1)
        return current[0]

    return now


# --- merge -----------------------------------------------------------------------------


def test_merge_overlay_supplies_plugin_pins_and_image_digests(tmp_path: Path) -> None:
    inventory_path, _ = _split_setup(tmp_path)
    split = load_inventory(inventory_path)
    full = load_inventory(_write_yaml(tmp_path / "full.yml", _full_raw(tmp_path)))

    for member in split.instances:
        assert member.image.digest == DIGEST_A
        assert member.image.known_good_digest == DIGEST_B
        assert member.plugins[0].sha256 == SHA_P
        assert member.plugins[0].source_type == "archive"
    assert [m.model_dump() for m in split.instances] == [m.model_dump() for m in full.instances]
    assert split.runtime_pins == str(tmp_path / "private" / "pins.json")


def test_merge_equal_duplicates_are_allowed(tmp_path: Path) -> None:
    raw = _full_raw(tmp_path)
    raw["runtime_pins"] = "private/pins.json"
    inventory_path, _ = _split_setup(tmp_path, raw=raw)

    inventory = load_inventory(inventory_path)

    assert inventory.instances[0].image.tag == "2026.1.1"


def test_merge_different_duplicates_fail_naming_both_sources(tmp_path: Path) -> None:
    raw = _split_raw(tmp_path)
    raw["instances"][0]["image"]["digest"] = DIGEST_C
    inventory_path, pins = _split_setup(tmp_path, raw=raw)

    with pytest.raises(RuntimePinsError) as excinfo:
        load_inventory(inventory_path)

    message = str(excinfo.value)
    assert "instances[example-lead].image.digest" in message
    assert str(inventory_path) in message
    assert str(pins) in message
    assert DIGEST_C in message
    assert DIGEST_A in message


def test_merge_plugin_conflict_fails(tmp_path: Path) -> None:
    raw = _split_raw(tmp_path)
    raw["instances"][1]["plugins"][0]["sha256"] = SHA_Q
    inventory_path, _ = _split_setup(tmp_path, raw=raw)

    with pytest.raises(RuntimePinsError, match=r"instances\[example-engineer\]"):
        load_inventory(inventory_path)


def test_overlay_values_are_not_expanded_but_reference_is(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXAMPLE_PINS_DIR", str(tmp_path / "private"))
    inventory_path, _ = _split_setup(
        tmp_path, raw=_split_raw(tmp_path, "${EXAMPLE_PINS_DIR}/pins.json")
    )

    assert load_inventory(inventory_path).runtime_pins == str(tmp_path / "private" / "pins.json")


def test_absolute_reference_is_used_as_is(tmp_path: Path) -> None:
    pins = _write_json(tmp_path / "elsewhere" / "p.json", _full_overlay(tmp_path))
    inventory_path = _write_yaml(tmp_path / "inv" / "team.yml", _split_raw(tmp_path, str(pins)))

    assert load_inventory(inventory_path).runtime_pins == str(pins)


def test_inventory_without_runtime_pins_is_unchanged(tmp_path: Path) -> None:
    inventory = load_inventory(_write_yaml(tmp_path / "team.yml", _full_raw(tmp_path)))

    assert inventory.runtime_pins is None


# --- validation ---------------------------------------------------------------------------


def _expect_rejection(tmp_path: Path, overlay: dict, *needles: str) -> str:
    inventory_path, pins = _split_setup(tmp_path, overlay=overlay)
    with pytest.raises(RuntimePinsError) as excinfo:
        load_inventory(inventory_path)
    message = str(excinfo.value)
    assert str(pins) in message
    for needle in needles:
        assert needle in message, message
    return message


@pytest.mark.parametrize(
    ("mutate", "location"),
    [
        (lambda o: o.update(extra=1), "extra"),
        (lambda o: o["members"]["example-lead"].update(env={}), "members.example-lead.env"),
        (
            lambda o: o["members"]["example-lead"]["image"].update(repository="x"),
            "members.example-lead.image.repository",
        ),
        (
            lambda o: o["members"]["example-lead"]["plugins"]["example-cad"].update(config={}),
            "members.example-lead.plugins.example-cad.config",
        ),
    ],
)
def test_unknown_keys_are_rejected_at_every_level(tmp_path: Path, mutate, location: str) -> None:
    overlay = _full_overlay(tmp_path)
    mutate(overlay)

    _expect_rejection(tmp_path, overlay, location, "unknown key")


def test_cluster_mismatch_is_rejected(tmp_path: Path) -> None:
    overlay = _full_overlay(tmp_path)
    overlay["cluster"] = "other-cluster"

    _expect_rejection(tmp_path, overlay, "cluster", "other-cluster", "example-cad")


def test_missing_version_or_cluster_is_rejected(tmp_path: Path) -> None:
    overlay = _full_overlay(tmp_path)
    del overlay["version"]
    _expect_rejection(tmp_path, overlay, "version")
    overlay = _full_overlay(tmp_path)
    overlay["version"] = 2
    _expect_rejection(tmp_path, overlay, "version")
    overlay = _full_overlay(tmp_path)
    del overlay["cluster"]
    _expect_rejection(tmp_path, overlay, "cluster")


def test_unknown_member_is_rejected(tmp_path: Path) -> None:
    overlay = _full_overlay(tmp_path)
    overlay["members"]["example-ghost"] = {}

    _expect_rejection(tmp_path, overlay, "members.example-ghost", "unknown member")


def test_undeclared_plugin_is_rejected(tmp_path: Path) -> None:
    overlay = _full_overlay(tmp_path)
    overlay["members"]["example-lead"]["plugins"]["example-other"] = {"sha256": SHA_Q}

    _expect_rejection(
        tmp_path, overlay, "members.example-lead.plugins.example-other", "not declared"
    )


def test_npm_plugin_pins_are_rejected(tmp_path: Path) -> None:
    overlay = _full_overlay(tmp_path)
    overlay["members"]["example-lead"]["plugins"]["example-npm"] = {"sha256": SHA_Q}

    _expect_rejection(tmp_path, overlay, "members.example-lead.plugins.example-npm", "npm")


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("image", "digest", "sha256:abcd"),
        ("image", "known_good_digest", "md5:" + "a" * 32),
        ("plugin", "sha256", "sha256:" + "A" * 64),
    ],
)
def test_malformed_digests_are_rejected(tmp_path: Path, section: str, key: str, value: str) -> None:
    overlay = _full_overlay(tmp_path)
    member = overlay["members"]["example-engineer"]
    target = member["image"] if section == "image" else member["plugins"]["example-cad"]
    target[key] = value

    _expect_rejection(tmp_path, overlay, "members.example-engineer.", key, "digest")


def test_non_string_values_are_rejected(tmp_path: Path) -> None:
    overlay = _full_overlay(tmp_path)
    overlay["members"]["example-lead"]["image"]["tag"] = 7

    _expect_rejection(tmp_path, overlay, "members.example-lead.image.tag")


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    pins.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimePinsError, match="invalid JSON"):
        load_inventory(inventory_path)


def test_v2_rejects_runtime_pins(tmp_path: Path) -> None:
    raw = {
        "version": 2,
        "runtime_pins": "pins.json",
        "cluster": {"name": "example-cad", "primary_host": "example-host"},
        "hosts": [{"name": "example-host"}],
        "networks": [{"name": "example-net"}],
        "services": [],
    }
    path = _write_yaml(tmp_path / "v2.yml", raw)

    with pytest.raises(ValueError, match="runtime_pins.*version 1"):
        load_inventory(path)


def test_missing_overlay_file_fails(tmp_path: Path) -> None:
    inventory_path = _write_yaml(tmp_path / "team.yml", _split_raw(tmp_path))

    with pytest.raises(RuntimePinsError, match="does not exist"):
        load_inventory(inventory_path)


def test_missing_overlay_file_is_allowed_for_migration(tmp_path: Path) -> None:
    raw = _full_raw(tmp_path)
    raw["runtime_pins"] = "private/pins.json"
    inventory_path = _write_yaml(tmp_path / "team.yml", raw)

    layers = load_inventory_layers(inventory_path, allow_missing_pins=True)

    assert layers.overlay is None
    assert layers.pins_path == tmp_path / "private" / "pins.json"


# --- file safety ----------------------------------------------------------------------------


def test_group_writable_overlay_is_refused(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    pins.chmod(0o620)

    with pytest.raises(RuntimePinsError, match="group- or world-writable"):
        load_inventory(inventory_path)


def test_world_writable_overlay_is_refused(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    pins.chmod(0o602)

    with pytest.raises(RuntimePinsError, match="group- or world-writable"):
        load_inventory(inventory_path)


def test_readable_overlay_warns(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    pins.chmod(0o644)

    with pytest.warns(RuntimePinsWarning, match="group- or world-readable"):
        load_inventory(inventory_path)


def test_private_overlay_does_not_warn(tmp_path: Path) -> None:
    inventory_path, _ = _split_setup(tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimePinsWarning)
        load_inventory(inventory_path)


def test_write_is_atomic_private_and_creates_backup(tmp_path: Path, monkeypatch) -> None:
    pins = _write_json(tmp_path / "pins.json", {"version": 1, "cluster": "old"})
    previous = pins.read_bytes()
    seen: dict[str, object] = {}
    real_replace = os.replace
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        seen.setdefault("fsync", 0)
        seen["fsync"] = int(seen["fsync"]) + 1
        real_fsync(fd)

    def spy_replace(src, dst) -> None:
        source = Path(src)
        seen["tmp_dir"] = source.parent
        seen["tmp_mode"] = stat.S_IMODE(source.stat().st_mode)
        seen["target_during"] = Path(dst).read_bytes()
        real_replace(src, dst)

    monkeypatch.setattr(runtime_pins.os, "replace", spy_replace)
    monkeypatch.setattr(runtime_pins.os, "fsync", spy_fsync)

    backup = write_overlay(pins, {"version": 1, "cluster": "new"}, now=_clock())

    assert seen["tmp_dir"] == tmp_path
    assert seen["tmp_mode"] == 0o600
    assert seen["target_during"] == previous
    assert int(seen["fsync"]) >= 1
    assert json.loads(pins.read_text())["cluster"] == "new"
    assert stat.S_IMODE(pins.stat().st_mode) == 0o600
    assert backup is not None
    assert backup.parent == tmp_path / BACKUP_DIRNAME
    assert backup.name == "20260101T000001.000000Z.json"
    assert backup.read_bytes() == previous
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_failed_write_leaves_previous_overlay(tmp_path: Path, monkeypatch) -> None:
    pins = _write_json(tmp_path / "pins.json", {"version": 1, "cluster": "old"})
    previous = pins.read_bytes()

    def broken_replace(src, dst) -> None:
        raise OSError("disk full (synthetic)")

    monkeypatch.setattr(runtime_pins.os, "replace", broken_replace)

    with pytest.raises(OSError, match="synthetic"):
        write_overlay(pins, {"version": 1, "cluster": "new"}, now=_clock())

    assert pins.read_bytes() == previous
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_backup_retention_keeps_newest_twenty(tmp_path: Path) -> None:
    pins = _write_json(tmp_path / "pins.json", {"version": 1, "cluster": "v0"})
    now = _clock()
    backups = []
    for index in range(1, BACKUP_RETENTION + 6):
        backups.append(write_overlay(pins, {"version": 1, "cluster": f"v{index}"}, now=now))
    unrelated = tmp_path / BACKUP_DIRNAME / "operator-notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    write_overlay(pins, {"version": 1, "cluster": "last"}, now=now)

    kept = sorted(p for p in (tmp_path / BACKUP_DIRNAME).glob("*.json"))
    assert BACKUP_RETENTION == 20
    assert len(kept) == 20
    assert kept[-1].name > backups[-1].name
    assert json.loads(kept[0].read_text())["cluster"] == "v6"
    assert unrelated.exists()


def test_symlink_target_is_refused_for_writes(tmp_path: Path) -> None:
    real = _write_json(tmp_path / "real.json", {"version": 1, "cluster": "old"})
    link = tmp_path / "pins.json"
    link.symlink_to(real)

    with pytest.raises(RuntimePinsError, match="symlink"):
        write_overlay(link, {"version": 1, "cluster": "new"}, now=_clock())

    assert json.loads(real.read_text())["cluster"] == "old"
    assert link.is_symlink()


def test_symlinked_backup_directory_is_refused(tmp_path: Path) -> None:
    pins = _write_json(tmp_path / "pins.json", {"version": 1, "cluster": "old"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / BACKUP_DIRNAME).symlink_to(elsewhere)

    with pytest.raises(RuntimePinsError, match="symlink"):
        write_overlay(pins, {"version": 1, "cluster": "new"}, now=_clock())

    assert list(elsewhere.iterdir()) == []
    assert json.loads(pins.read_text())["cluster"] == "old"


def test_create_only_refuses_existing_file(tmp_path: Path) -> None:
    pins = _write_json(tmp_path / "pins.json", {"version": 1, "cluster": "old"})

    with pytest.raises(RuntimePinsError, match="already exists"):
        write_overlay(pins, {"version": 1, "cluster": "new"}, create_only=True)

    assert json.loads(pins.read_text())["cluster"] == "old"


# --- writers ---------------------------------------------------------------------------------


def test_upgrade_and_rollback_write_overlay_and_leave_inventory_identical(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    before = inventory_path.read_bytes()

    plan = build_upgrade_plan(inventory_path, "example-lead", "2026.2.0", DIGEST_C)
    assert (plan.previous_tag, plan.previous_digest) == ("2026.1.1", DIGEST_A)
    applied = apply_upgrade(inventory_path, "example-lead", "2026.2.0", DIGEST_C)

    assert inventory_path.read_bytes() == before
    image = json.loads(pins.read_text())["members"]["example-lead"]["image"]
    assert image == {
        "tag": "2026.2.0",
        "digest": DIGEST_C,
        "known_good_tag": "2026.1.1",
        "known_good_digest": DIGEST_A,
    }
    assert applied.pins_backup is not None and applied.pins_backup.exists()
    engineer = json.loads(pins.read_text())["members"]["example-engineer"]["image"]
    assert engineer["digest"] == DIGEST_A
    assert load_inventory(inventory_path).instances[0].image.digest == DIGEST_C

    rolled = rollback_to_known_good(inventory_path, "example-lead")

    assert inventory_path.read_bytes() == before
    assert (rolled.next_tag, rolled.next_digest) == ("2026.1.1", DIGEST_A)
    image = json.loads(pins.read_text())["members"]["example-lead"]["image"]
    assert (image["tag"], image["digest"]) == ("2026.1.1", DIGEST_A)
    assert len(list((pins.parent / BACKUP_DIRNAME).glob("*.json"))) == 2
    assert stat.S_IMODE(pins.stat().st_mode) == 0o600


def test_upgrade_refuses_when_inventory_duplicates_the_image_field(tmp_path: Path) -> None:
    raw = _full_raw(tmp_path)
    raw["runtime_pins"] = "private/pins.json"
    inventory_path, pins = _split_setup(tmp_path, raw=raw)
    before_inventory = inventory_path.read_bytes()
    before_pins = pins.read_bytes()

    with pytest.raises(RuntimePinsError, match="conflict"):
        apply_upgrade(inventory_path, "example-lead", "2026.2.0", DIGEST_C)

    assert inventory_path.read_bytes() == before_inventory
    assert pins.read_bytes() == before_pins
    assert not (pins.parent / BACKUP_DIRNAME).exists()


LEGACY_INVENTORY = """# operator comment
version: 1
instances:
  - name: one
    image:
      repository: ghcr.io/example/openclaw
      tag: "2026.1.0" # pinned by hand
      digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    labels: {a: b}
"""

LEGACY_AFTER_UPGRADE = """# operator comment
version: 1
instances:
  - name: one
    image:
      repository: ghcr.io/example/openclaw
      tag: "2026.2.0"
      digest: sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      known_good_tag: "2026.1.0"
      known_good_digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    labels: {a: b}
"""

LEGACY_AFTER_ROLLBACK = LEGACY_AFTER_UPGRADE.replace(
    'tag: "2026.2.0"\n      digest: sha256:' + "c" * 64,
    'tag: "2026.1.0"\n      digest: sha256:' + "a" * 64,
)


def test_without_runtime_pins_upgrade_writes_inventory_byte_for_byte_as_before(
    tmp_path: Path,
) -> None:
    inventory = tmp_path / "team.yml"
    inventory.write_text(LEGACY_INVENTORY, encoding="utf-8")

    plan = apply_upgrade(inventory, "one", "2026.2.0", DIGEST_C)
    assert inventory.read_text(encoding="utf-8") == LEGACY_AFTER_UPGRADE
    assert plan.pins_backup is None

    rollback_to_known_good(inventory, "one")
    assert inventory.read_text(encoding="utf-8") == LEGACY_AFTER_ROLLBACK
    assert sorted(p.name for p in tmp_path.iterdir()) == ["team.yml"]


def test_set_plugin_pin_round_trip(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    before = inventory_path.read_bytes()
    new_artifact = str(tmp_path / "packages" / "example-cad-0.0.2.tgz")

    backup = set_plugin_pin(
        inventory_path, "example-engineer", "example-cad", new_artifact, SHA_Q, now=_clock()
    )

    assert backup.exists() and backup.parent == pins.parent / BACKUP_DIRNAME
    assert inventory_path.read_bytes() == before
    report = read_pins(inventory_path)
    rows = {(r.member, r.field): r for r in report.rows}
    assert rows[("example-engineer", "plugins.example-cad.sha256")].value == SHA_Q
    assert rows[("example-engineer", "plugins.example-cad.artifact_path")].value == new_artifact
    assert rows[("example-engineer", "plugins.example-cad.sha256")].source == "overlay"
    assert rows[("example-lead", "plugins.example-cad.sha256")].value == SHA_P
    effective = load_inventory(inventory_path).instances[1].plugins[0]
    assert (effective.artifact_path, effective.sha256) == (new_artifact, SHA_Q)


def test_set_plugin_pin_validates_before_writing(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    before = pins.read_bytes()

    with pytest.raises(RuntimePinsError):
        set_plugin_pin(inventory_path, "example-lead", "example-npm", "/x/a.tgz", SHA_Q)
    with pytest.raises(RuntimePinsError):
        set_plugin_pin(inventory_path, "example-lead", "example-cad", "/x/a.tgz", "sha256:12")
    with pytest.raises(ValueError):
        set_plugin_pin(inventory_path, "example-lead", "example-cad", "/x/a.zip", SHA_Q)

    assert pins.read_bytes() == before
    assert not (pins.parent / BACKUP_DIRNAME).exists()


def test_set_plugin_pin_requires_runtime_pins(tmp_path: Path) -> None:
    inventory_path = _write_yaml(tmp_path / "team.yml", _full_raw(tmp_path))
    before = inventory_path.read_bytes()

    with pytest.raises(RuntimePinsError, match="does not declare runtime_pins"):
        set_plugin_pin(inventory_path, "example-lead", "example-cad", "/x/a.tgz", SHA_Q)

    assert inventory_path.read_bytes() == before


# --- CLI ---------------------------------------------------------------------------------------


def test_pins_show_reports_sources(tmp_path: Path) -> None:
    raw = _split_raw(tmp_path)
    raw["instances"][0]["image"]["tag"] = "2026.1.1"
    raw["instances"][1]["image"]["known_good_tag"] = "2026.0.9"
    overlay = _full_overlay(tmp_path)
    del overlay["members"]["example-engineer"]["image"]["known_good_tag"]
    inventory_path, pins = _split_setup(tmp_path, overlay=overlay, raw=raw)

    result = runner.invoke(app, ["pins", "show", "-c", str(inventory_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["runtime_pins"] == str(pins)
    lead = data["members"]["example-lead"]
    engineer = data["members"]["example-engineer"]
    assert lead["image.tag"] == {
        "value": "2026.1.1",
        "source": "overlay",
        "also_in_inventory": True,
    }
    assert lead["image.digest"] == {
        "value": DIGEST_A,
        "source": "overlay",
        "also_in_inventory": False,
    }
    assert engineer["image.known_good_tag"]["source"] == "inventory"
    assert engineer["image.known_good_tag"]["value"] == "2026.0.9"
    assert engineer["plugins.example-cad.sha256"]["source"] == "overlay"
    assert "plugins.example-npm.sha256" not in engineer

    text = runner.invoke(app, ["pins", "show", "-c", str(inventory_path)])
    assert text.exit_code == 0, text.output
    assert f"runtime pins: {pins}" in text.output
    assert "image.known_good_tag = 2026.0.9 [inventory]" in text.output
    assert f"image.digest = {DIGEST_A} [overlay]" in text.output
    assert "image.tag = 2026.1.1 [overlay, also in inventory]" in text.output


def test_pins_show_without_overlay_reports_inventory(tmp_path: Path) -> None:
    inventory_path = _write_yaml(tmp_path / "team.yml", _full_raw(tmp_path))

    result = runner.invoke(app, ["pins", "show", "-c", str(inventory_path)])

    assert result.exit_code == 0, result.output
    assert "runtime pins: none declared" in result.output
    assert "[overlay" not in result.output
    assert f"image.digest = {DIGEST_A} [inventory]" in result.output


def test_pins_init_preview_writes_nothing(tmp_path: Path) -> None:
    inventory_path = _write_yaml(tmp_path / "team.yml", _full_raw(tmp_path))
    before = inventory_path.read_bytes()
    output = tmp_path / "private" / "pins.json"
    output.parent.mkdir()

    result = runner.invoke(
        app, ["pins", "init", "-c", str(inventory_path), "--output", str(output)]
    )

    assert result.exit_code == 0, result.output
    assert not output.exists()
    assert sorted(p.name for p in output.parent.iterdir()) == []
    assert inventory_path.read_bytes() == before
    start = result.output.index("{")
    end = result.output.index("\n}\n") + 2
    assert json.loads(result.output[start:end]) == _full_overlay(tmp_path)
    assert "instances[example-lead].image.digest" in result.output
    assert "instances[example-engineer].plugins[example-cad].sha256" in result.output
    assert "instances[example-lead].plugins[example-npm]" not in result.output
    assert "instances[example-lead].image.repository" not in result.output
    assert f"runtime_pins: {output}" in result.output
    assert "DRY RUN" in result.output


def test_pins_init_execute_writes_overlay_and_split_loads_identically(tmp_path: Path) -> None:
    inventory_path = _write_yaml(tmp_path / "team.yml", _full_raw(tmp_path))
    before = inventory_path.read_bytes()
    output = tmp_path / "private" / "pins.json"
    output.parent.mkdir()

    preview = runner.invoke(
        app, ["pins", "init", "-c", str(inventory_path), "--output", str(output)]
    )
    result = runner.invoke(
        app, ["pins", "init", "-c", str(inventory_path), "--output", str(output), "--execute"]
    )

    assert result.exit_code == 0, result.output
    assert inventory_path.read_bytes() == before
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    start = preview.output.index("{")
    end = preview.output.index("\n}\n") + 3
    assert output.read_text(encoding="utf-8") == preview.output[start:end]
    assert not (output.parent / BACKUP_DIRNAME).exists()
    split = _write_yaml(tmp_path / "split.yml", _split_raw(tmp_path, str(output)))
    full = load_inventory(inventory_path)
    assert load_inventory(split).model_dump(exclude={"runtime_pins"}) == full.model_dump(
        exclude={"runtime_pins"}
    )


def test_pins_init_refuses_to_overwrite(tmp_path: Path) -> None:
    inventory_path = _write_yaml(tmp_path / "team.yml", _full_raw(tmp_path))
    output = _write_json(tmp_path / "pins.json", {"keep": True})
    before = output.read_bytes()

    for extra in ([], ["--execute"]):
        result = runner.invoke(
            app, ["pins", "init", "-c", str(inventory_path), "--output", str(output), *extra]
        )
        assert result.exit_code != 0
        assert "already exists" in result.output
    assert output.read_bytes() == before


def test_pins_init_with_declared_but_missing_overlay(tmp_path: Path) -> None:
    raw = _full_raw(tmp_path)
    raw["runtime_pins"] = "pins.json"
    inventory_path = _write_yaml(tmp_path / "team.yml", raw)

    result = runner.invoke(
        app, ["pins", "init", "-c", str(inventory_path), "--output", str(tmp_path / "pins.json")]
    )

    assert result.exit_code == 0, result.output
    assert "runtime_pins already declared" in result.output


def test_validate_reports_overlay(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)

    result = runner.invoke(app, ["validate", "-c", str(inventory_path)])

    assert result.exit_code == 0, result.output
    assert f"runtime pins: {pins} (2 members, 12 fields)" in result.output


def test_validate_shows_readable_overlay_warning(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    pins.chmod(0o640)

    result = runner.invoke(app, ["validate", "-c", str(inventory_path)])

    assert result.exit_code == 0, result.output
    assert "WARNING:" in result.output and "group- or world-readable" in result.output


def test_validate_conflict_exits_with_error(tmp_path: Path) -> None:
    raw = _split_raw(tmp_path)
    raw["instances"][0]["image"]["tag"] = "2026.9.9"
    inventory_path, _ = _split_setup(tmp_path, raw=raw)

    result = runner.invoke(app, ["validate", "-c", str(inventory_path)])

    assert result.exit_code == 2
    assert "runtime pins conflict" in " ".join(result.output.split())


def test_upgrade_preview_names_overlay_and_mutates_nothing(tmp_path: Path) -> None:
    inventory_path, pins = _split_setup(tmp_path)
    before = (inventory_path.read_bytes(), pins.read_bytes())

    result = runner.invoke(
        app,
        [
            "upgrade",
            "-c",
            str(inventory_path),
            "-m",
            "example-lead",
            "--to",
            "2026.2.0",
            "--digest",
            DIGEST_C,
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"pins: {pins}" in result.output
    assert (inventory_path.read_bytes(), pins.read_bytes()) == before
    assert not (pins.parent / BACKUP_DIRNAME).exists()


def test_upgrade_preview_fails_on_inventory_duplicate(tmp_path: Path) -> None:
    raw = _full_raw(tmp_path)
    raw["runtime_pins"] = "private/pins.json"
    inventory_path, pins = _split_setup(tmp_path, raw=raw)

    result = runner.invoke(
        app,
        [
            "upgrade",
            "-c",
            str(inventory_path),
            "-m",
            "example-lead",
            "--to",
            "2026.2.0",
            "--digest",
            DIGEST_C,
        ],
    )

    assert result.exit_code != 0
    assert "conflict" in result.output


def test_overlay_copy_is_not_mutated_by_merge(tmp_path: Path) -> None:
    raw = _split_raw(tmp_path)
    overlay = _full_overlay(tmp_path)
    snapshot = (copy.deepcopy(raw), copy.deepcopy(overlay))

    runtime_pins.apply_overlay(
        raw, overlay, inventory_source=tmp_path / "a", overlay_source=tmp_path / "b"
    )

    assert (raw, overlay) == snapshot
