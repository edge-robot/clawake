import json
from pathlib import Path

import pytest
import yaml

from clawake.services.upgrade import (
    apply_upgrade,
    backup_config,
    build_upgrade_plan,
    rollback_to_known_good,
)

PRODUCT_OWNER_INSTANCE = "excalibot-product-owner"


def _load_image(config: Path, instance_name: str) -> dict[str, str]:
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    for instance in data["instances"]:
        if instance["name"] == instance_name:
            return instance["image"]
    raise AssertionError(f"Missing instance {instance_name}")


def test_build_upgrade_plan() -> None:
    config = Path("examples/staff/team.yml")
    image = _load_image(config, PRODUCT_OWNER_INSTANCE)
    plan = build_upgrade_plan(
        config,
        PRODUCT_OWNER_INSTANCE,
        next_tag="0.15.0",
        next_digest="sha256:abcd",
    )
    assert plan.previous_tag == image["tag"]
    assert plan.next_tag == "0.15.0"
    assert plan.next_digest == "sha256:abcd"


def test_apply_upgrade_and_rollback(tmp_path: Path) -> None:
    src = Path("examples/staff/team.yml")
    config = tmp_path / "product.yml"
    config.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # Keep this test independent from staff example defaults.
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    for instance in data["instances"]:
        if instance["name"] == PRODUCT_OWNER_INSTANCE:
            instance["image"]["known_good_digest"] = "sha256:known-good"
            break
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    original_image = _load_image(config, PRODUCT_OWNER_INSTANCE)

    applied = apply_upgrade(
        config,
        PRODUCT_OWNER_INSTANCE,
        next_tag="0.15.0",
        next_digest="sha256:new",
    )
    assert applied.next_tag == "0.15.0"
    assert applied.next_digest == "sha256:new"

    rolled = rollback_to_known_good(config, PRODUCT_OWNER_INSTANCE)
    assert rolled.previous_digest == "sha256:new"
    assert rolled.next_digest == original_image["known_good_digest"]
    assert rolled.next_tag == original_image["tag"]
    rolled_image = _load_image(config, PRODUCT_OWNER_INSTANCE)
    assert rolled_image["tag"] == original_image["tag"]


def test_upgrade_plan_unknown_instance_raises() -> None:
    config = Path("examples/staff/team.yml")
    with pytest.raises(ValueError):
        build_upgrade_plan(config, "missing", next_tag=None, next_digest=None)


def test_apply_upgrade_preserves_unrelated_yaml_formatting(tmp_path: Path) -> None:
    config = tmp_path / "team.yml"
    config.write_text(
        """instances:
  - name: one
    image:
      repository: ghcr.io/openclaw/openclaw
      tag: "old"
      digest: sha256:old
    dashboard:
      tags: [one, two]

  - name: two
    image:
      repository: example.invalid/two
      tag: "untouched"
""",
        encoding="utf-8",
    )

    apply_upgrade(config, "one", "new", "sha256:new")

    updated = config.read_text(encoding="utf-8")
    assert '      tag: "new"\n' in updated
    assert '      known_good_tag: "old"\n' in updated
    assert "      known_good_digest: sha256:old\n" in updated
    assert "      tags: [one, two]\n" in updated
    assert '      tag: "untouched"\n' in updated


# --- #27: inventory writes keep the file format ---------------------------------------------


def _json_inventory() -> dict:
    return {
        "version": 1,
        "cluster": {"name": "synthetic", "primary_host": "local"},
        "instances": [
            {
                "name": "one",
                "image": {
                    "repository": "ghcr.io/openclaw/openclaw",
                    "tag": "2026.9.9",
                    "digest": "sha256:" + "a" * 64,
                },
                "dashboard": {"friendly_name": "One", "tags": ["x", "y"]},
            },
            {
                "name": "two",
                "image": {"repository": "example.invalid/two", "tag": "untouched"},
            },
        ],
    }


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_apply_upgrade_keeps_json_inventory_valid_json(tmp_path: Path) -> None:
    config = tmp_path / "team.json"
    data = _json_inventory()
    _write_json(config, data)

    apply_upgrade(config, "one", "2026.10.1", "sha256:" + "b" * 64)

    text = config.read_text(encoding="utf-8")
    updated = json.loads(text)  # must stay JSON, not YAML flow style
    expected = _json_inventory()
    expected["instances"][0]["image"].update(
        {
            "tag": "2026.10.1",
            "digest": "sha256:" + "b" * 64,
            "known_good_tag": "2026.9.9",
            "known_good_digest": "sha256:" + "a" * 64,
        }
    )
    assert updated == expected
    # Stable key order and 2-space indent: byte-identical to a canonical dump.
    assert text == json.dumps(expected, indent=2) + "\n"


def test_rollback_keeps_json_inventory_valid_json(tmp_path: Path) -> None:
    config = tmp_path / "team.json"
    _write_json(config, _json_inventory())
    apply_upgrade(config, "one", "2026.10.1", "sha256:" + "b" * 64)

    rollback_to_known_good(config, "one")

    image = json.loads(config.read_text(encoding="utf-8"))["instances"][0]["image"]
    assert image["tag"] == "2026.9.9"
    assert image["digest"] == "sha256:" + "a" * 64
    assert list(image) == [
        "repository",
        "tag",
        "digest",
        "known_good_tag",
        "known_good_digest",
    ]


def test_apply_upgrade_never_writes_literal_none_in_yaml(tmp_path: Path) -> None:
    config = tmp_path / "team.yml"
    config.write_text(
        """instances:
  - name: one
    image:
      repository: ghcr.io/openclaw/openclaw
      tag: "old"
""",
        encoding="utf-8",
    )

    # Same tag, no digest: the plan's digest stays None (#31 refuses a tag change without one).
    apply_upgrade(config, "one", "old", None)

    text = config.read_text(encoding="utf-8")
    assert "None" not in text
    assert "null" not in text
    image = yaml.safe_load(text)["instances"][0]["image"]
    assert image == {
        "repository": "ghcr.io/openclaw/openclaw",
        "tag": "old",
        "known_good_tag": "old",
    }


def test_apply_upgrade_never_writes_null_in_json(tmp_path: Path) -> None:
    config = tmp_path / "team.json"
    data = _json_inventory()
    del data["instances"][0]["image"]["digest"]
    _write_json(config, data)

    # Same tag, no digest: the plan's digest stays None (#31 refuses a tag change without one).
    apply_upgrade(config, "one", "2026.9.9", None)

    text = config.read_text(encoding="utf-8")
    assert "null" not in text
    image = json.loads(text)["instances"][0]["image"]
    assert "digest" not in image
    assert image["tag"] == "2026.9.9"
    assert image["known_good_tag"] == "2026.9.9"


@pytest.mark.parametrize("name", ["team.json", "team.yml", "team.yaml"])
def test_backup_config_keeps_the_original_suffix(tmp_path: Path, name: str) -> None:
    config = tmp_path / name
    config.write_text('{"version": 1}\n', encoding="utf-8")

    backup = backup_config(config, tmp_path / "backups")

    assert backup.suffix == config.suffix
    assert backup.name.startswith("team-")
    assert backup.read_bytes() == config.read_bytes()


# --- #31: a tag change without a digest fails closed in the library ------------------------

_UNPINNED_YAML = """instances:
  - name: one
    image:
      repository: ghcr.io/openclaw/openclaw
      tag: "old"
      digest: sha256:old
"""


def test_build_upgrade_plan_refuses_tag_change_without_digest(tmp_path: Path) -> None:
    config = tmp_path / "team.yml"
    config.write_text(_UNPINNED_YAML, encoding="utf-8")

    with pytest.raises(ValueError, match="digest is required"):
        build_upgrade_plan(config, "one", next_tag="new", next_digest=None)


def test_apply_upgrade_refuses_tag_change_without_digest_and_writes_nothing(
    tmp_path: Path,
) -> None:
    config = tmp_path / "team.yml"
    config.write_text(_UNPINNED_YAML, encoding="utf-8")
    before = config.read_bytes()

    with pytest.raises(ValueError, match="digest is required"):
        apply_upgrade(config, "one", "new", None)

    assert config.read_bytes() == before


def test_same_tag_without_digest_keeps_the_current_digest(tmp_path: Path) -> None:
    config = tmp_path / "team.yml"
    config.write_text(_UNPINNED_YAML, encoding="utf-8")

    plan = build_upgrade_plan(config, "one", next_tag="old", next_digest=None)

    assert (plan.next_tag, plan.next_digest) == ("old", "sha256:old")


# --- #30: YAML inventory writes are atomic ----------------------------------------------------

_BLOCK_YAML = """# operator comment
instances:
  - name: one
    image:
      repository: ghcr.io/openclaw/openclaw
      tag: "old" # pinned by hand
      digest: sha256:old
"""

_FLOW_YAML = """instances:
  - name: one
    image: {repository: ghcr.io/openclaw/openclaw, tag: old, digest: 'sha256:old'}
    labels: {a: b}
"""


@pytest.mark.parametrize("original", [_BLOCK_YAML, _FLOW_YAML], ids=["block", "flow"])
def test_interrupted_yaml_replace_leaves_the_original_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, original: str
) -> None:
    from clawake.services import upgrade

    config = tmp_path / "team.yml"
    config.write_text(original, encoding="utf-8")
    config.chmod(0o640)

    def interrupted_replace(source: object, target: object) -> None:
        raise OSError("synthetic interruption before rename")

    monkeypatch.setattr(upgrade.os, "replace", interrupted_replace)

    with pytest.raises(OSError, match="synthetic interruption"):
        apply_upgrade(config, "one", "new", "sha256:new")

    assert config.read_text(encoding="utf-8") == original
    assert sorted(path.name for path in tmp_path.iterdir()) == ["team.yml"]


def test_flow_style_yaml_upgrade_output_is_unchanged(tmp_path: Path) -> None:
    config = tmp_path / "team.yml"
    config.write_text(_FLOW_YAML, encoding="utf-8")
    config.chmod(0o640)

    apply_upgrade(config, "one", "new", "sha256:new")

    assert config.read_text(encoding="utf-8") == (
        "instances:\n"
        "  - name: one\n"
        "    image: {repository: ghcr.io/openclaw/openclaw, tag: new, digest: 'sha256:new',"
        " known_good_tag: old,\n"
        "  known_good_digest: 'sha256:old'}\n"
        "    labels: {a: b}\n"
    )
    assert config.stat().st_mode & 0o777 == 0o640
