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

    apply_upgrade(config, "one", "new", None)

    text = config.read_text(encoding="utf-8")
    assert "None" not in text
    assert "null" not in text
    image = yaml.safe_load(text)["instances"][0]["image"]
    assert image == {
        "repository": "ghcr.io/openclaw/openclaw",
        "tag": "new",
        "known_good_tag": "old",
    }


def test_apply_upgrade_never_writes_null_in_json(tmp_path: Path) -> None:
    config = tmp_path / "team.json"
    data = _json_inventory()
    del data["instances"][0]["image"]["digest"]
    _write_json(config, data)

    apply_upgrade(config, "one", "2026.10.1", None)

    text = config.read_text(encoding="utf-8")
    assert "null" not in text
    image = json.loads(text)["instances"][0]["image"]
    assert "digest" not in image
    assert image["tag"] == "2026.10.1"


@pytest.mark.parametrize("name", ["team.json", "team.yml", "team.yaml"])
def test_backup_config_keeps_the_original_suffix(tmp_path: Path, name: str) -> None:
    config = tmp_path / name
    config.write_text('{"version": 1}\n', encoding="utf-8")

    backup = backup_config(config, tmp_path / "backups")

    assert backup.suffix == config.suffix
    assert backup.name.startswith("team-")
    assert backup.read_bytes() == config.read_bytes()
