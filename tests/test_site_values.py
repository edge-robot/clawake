"""Private site values and whole-string ``${site:<name>}`` references (ADR-005 items 1, 2).

All values are synthetic. Every file lives below tmp_path. Clawake never writes the site
values file; these tests only ever write it themselves, as the operator would.
"""

from __future__ import annotations

import json
import os
import stat
import warnings
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from clawake import config as config_module
from clawake.cli import app
from clawake.config import load_inventory, load_inventory_layers
from clawake.services import site_values
from clawake.services.runtime_pins import FILE_MODE
from clawake.services.site_values import SiteValuesError, SiteValuesWarning

runner = CliRunner()

GUILD = "100000000000000001"
CHANNEL = "100000000000000002"
APPROVER_A = "100000000000000003"
APPROVER_B = "100000000000000004"
TAG = "2026.1.1"


def _member(root: Path) -> dict:
    return {
        "name": "lead",
        "host": "example-host",
        "role": "product_owner",
        "workspace_path": str(root / "lead" / "workspace"),
        "state_path": str(root / "lead" / "state"),
        "team_definition_path": str(root / "roles" / "lead"),
        "quadlet_path": "lead.container",
        "container_name": "lead",
        "image": {"repository": "ghcr.io/example/openclaw", "tag": TAG},
        "env_files": [str(root / "env" / "lead.env")],
        "dashboard": {"friendly_name": "Lead"},
        "openclaw": {"lead_agent_id": "lead"},
        "plugins": [
            {
                "id": "example-cad",
                "npm_spec": "example-cad@1.2.3",
                "integrity": "sha512-AAAA",
                "config": {"approvers": "${site:discord.approvers}", "liveReads": False},
            }
        ],
    }


def _inventory(root: Path, **extra: object) -> dict:
    return {
        "version": 1,
        "cluster": {"name": "example-cad", "primary_host": "example-host"},
        "hosts": [{"name": "example-host", "quadlet_root": str(root / "quadlets")}],
        "instances": [_member(root)],
        "site_values": "private/site.json",
        **extra,
    }


def _values() -> dict:
    return {
        "discord.guild": GUILD,
        "discord.lead_channel": CHANNEL,
        "discord.approvers": [APPROVER_A, APPROVER_B],
    }


def _write(root: Path, data: dict, site: dict | None = None, *, mode: int = FILE_MODE) -> Path:
    path = root / "inventory.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    if site is not None:
        site_path = root / "private" / "site.json"
        site_path.parent.mkdir(mode=0o700, exist_ok=True)
        site_path.write_text(json.dumps(site, indent=2), encoding="utf-8")
        site_path.chmod(mode)
    return path


def _site(values: dict | None = None, **extra: object) -> dict:
    return {
        "version": 1,
        "cluster": "example-cad",
        "values": _values() if values is None else values,
        **extra,
    }


def _plugin_config(inventory) -> dict:
    return inventory.instances[0].plugins[0].config


# --- file format ------------------------------------------------------------------------


def test_site_values_substitute_string_and_list_values(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), _site())

    layers = load_inventory_layers(cfg)

    assert _plugin_config(layers.inventory)["approvers"] == [APPROVER_A, APPROVER_B]
    assert layers.inventory.site_values == str(tmp_path / "private" / "site.json")
    assert layers.site is not None
    assert layers.site.value_count == 3
    assert [ref.name for ref in layers.site.references] == ["discord.approvers"]
    assert layers.site.unused == ("discord.guild", "discord.lead_channel")


@pytest.mark.parametrize(
    ("site", "message"),
    [
        (_site(extra_key=True), "extra_key: unknown key (allowed: version, cluster, values)"),
        ({"version": 2, "cluster": "example-cad", "values": {}}, "version must be 1"),
        ({"version": True, "cluster": "example-cad", "values": {}}, "version must be 1"),
        ({"version": 1, "values": {}}, "cluster is required"),
        ({"version": 1, "cluster": "other", "values": {}}, "does not match inventory cluster.name"),
        ({"version": 1, "cluster": "example-cad"}, "values is required"),
        ({"version": 1, "cluster": "example-cad", "values": []}, "values must be an object"),
        (_site({"Discord.Guild": GUILD}), "values.Discord.Guild: invalid site value name"),
        (_site({"-guild": GUILD}), "values.-guild: invalid site value name"),
        (_site({"discord.guild": 1}), "values.discord.guild must be a string or a list of strings"),
        (_site({"discord.guild": [GUILD, 2]}), "values.discord.guild must be a string or a list"),
        (
            _site({"discord.guild": {"id": GUILD}}),
            "values.discord.guild must be a string or a list",
        ),
    ],
)
def test_malformed_site_files_are_rejected(tmp_path: Path, site: dict, message: str) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), site)

    with pytest.raises(SiteValuesError) as excinfo:
        load_inventory(cfg)

    text = str(excinfo.value)
    assert message in text
    assert str(tmp_path / "private" / "site.json") in text
    for value in (GUILD, APPROVER_A, APPROVER_B):
        assert value not in text


def test_invalid_json_is_rejected_without_content(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), _site())
    (tmp_path / "private" / "site.json").write_text('{"values": {"x": "' + GUILD, encoding="utf-8")

    with pytest.raises(SiteValuesError, match="invalid JSON") as excinfo:
        load_inventory(cfg)

    assert GUILD not in str(excinfo.value)


def test_missing_site_file_is_an_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path))

    with pytest.raises(SiteValuesError, match="site values file .*site.json does not exist"):
        load_inventory(cfg)


@pytest.mark.parametrize("mode", [0o620, 0o602])
def test_group_or_world_writable_site_file_is_refused(tmp_path: Path, mode: int) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), _site(), mode=mode)

    with pytest.raises(SiteValuesError, match="group- or world-writable"):
        load_inventory(cfg)


def test_readable_site_file_warns(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), _site(), mode=0o644)

    with pytest.warns(SiteValuesWarning, match="site values file .* group- or world-readable"):
        load_inventory(cfg)


def test_private_site_file_does_not_warn(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), _site())

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        load_inventory(cfg)


def test_v2_rejects_site_values(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    data["version"] = 2
    cfg = _write(tmp_path, data, _site())

    with pytest.raises(ValueError, match="site_values is only supported in inventory version 1"):
        load_inventory(cfg)


def test_site_values_module_has_no_write_api() -> None:
    public = {name for name in dir(site_values) if not name.startswith("_")}

    assert not {name for name in public if "write" in name or "init" in name or "save" in name}


def test_loading_never_writes_the_site_file(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), _site())
    path = tmp_path / "private" / "site.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode))

    load_inventory(cfg)
    runner.invoke(app, ["validate", "-c", str(cfg)])

    assert (path.read_bytes(), path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode)) == before
    assert sorted(os.listdir(path.parent)) == ["site.json"]


# --- references -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "x-${site:discord.guild}",
        "${site:discord.guild}-x",
        "${site:discord.guild}${site:a}",
        "${site:",
    ],
)
def test_partial_reference_is_an_error(tmp_path: Path, text: str) -> None:
    data = _inventory(tmp_path)
    data["instances"][0]["plugins"][0]["config"]["channel"] = text
    cfg = _write(tmp_path, data, _site())

    with pytest.raises(SiteValuesError) as excinfo:
        load_inventory(cfg)

    assert str(excinfo.value) == (
        "instances[lead].plugins[example-cad].config.channel: '${site:' is only allowed as a "
        "whole-string reference '${site:<name>}'; partial references are not substituted"
    )


def test_reference_in_mapping_key_is_an_error(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    data["instances"][0]["plugins"][0]["config"]["${site:discord.guild}"] = True
    cfg = _write(tmp_path, data, _site())

    with pytest.raises(SiteValuesError, match="mapping keys cannot be site value references"):
        load_inventory(cfg)


def test_unknown_site_name_fails_loading(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    data["instances"][0]["plugins"][0]["config"]["approvers"] = "${site:discord.approver}"
    cfg = _write(tmp_path, data, _site())

    with pytest.raises(SiteValuesError) as excinfo:
        load_inventory(cfg)

    assert str(excinfo.value) == (
        "instances[lead].plugins[example-cad].config.approvers: unknown site value "
        f"'discord.approver' (not defined in site values file {tmp_path / 'private' / 'site.json'})"
    )


def test_invalid_reference_name_fails_loading(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    data["instances"][0]["plugins"][0]["config"]["approvers"] = "${site:Discord}"
    cfg = _write(tmp_path, data, _site())

    with pytest.raises(SiteValuesError, match="invalid site value name 'Discord'"):
        load_inventory(cfg)


def test_reference_without_site_values_declaration_fails(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    del data["site_values"]
    cfg = _write(tmp_path, data)

    with pytest.raises(SiteValuesError, match="the inventory declares no site_values"):
        load_inventory(cfg)


def test_list_value_in_string_position_is_a_model_type_error(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    data["instances"][0]["dashboard"]["owner"] = "${site:discord.approvers}"
    cfg = _write(tmp_path, data, _site())

    with pytest.raises(ValueError) as excinfo:
        load_inventory(cfg)

    text = str(excinfo.value)
    assert "instances.0.dashboard.owner" in text
    assert "valid string" in text
    assert "site value 'discord.approvers' at instances[lead].dashboard.owner" in text
    assert APPROVER_A not in text


def test_reference_replaces_whole_fields_in_typed_positions(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    data["instances"][0]["dashboard"]["tags"] = "${site:discord.approvers}"
    data["instances"][0]["dashboard"]["owner"] = "${site:discord.guild}"
    cfg = _write(tmp_path, data, _site())

    dashboard = load_inventory(cfg).instances[0].dashboard

    assert dashboard.tags == [APPROVER_A, APPROVER_B]
    assert dashboard.owner == GUILD


def test_plugin_config_substitution_is_generic(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    data["instances"][0]["plugins"][0]["config"]["bindings"] = [
        {
            "discordChannelId": "${site:discord.lead_channel}",
            "approvers": "${site:discord.approvers}",
        }
    ]
    cfg = _write(tmp_path, data, _site())

    config = _plugin_config(load_inventory(cfg))

    assert config["bindings"] == [
        {"discordChannelId": CHANNEL, "approvers": [APPROVER_A, APPROVER_B]}
    ]
    assert config["liveReads"] is False


def test_env_expander_never_looks_up_site_references() -> None:
    class RecordingEnv(dict):
        def __init__(self) -> None:
            super().__init__(HOME="/home/example")
            self.lookups: list[str] = []

        def get(self, key, default=None):
            self.lookups.append(key)
            return super().get(key, default)

    env = RecordingEnv()
    data = {"a": "${site:discord.guild}", "b": ["x-${site:a}", "$HOME/${HOME}"], "c": "${HOME}"}

    expanded = config_module._expand_string_values(data, env)

    assert expanded == {
        "a": "${site:discord.guild}",
        "b": ["x-${site:a}", "/home/example//home/example"],
        "c": "/home/example",
    }
    assert env.lookups == ["HOME", "HOME", "HOME"]
    assert not [key for key in env.lookups if key.startswith("site:")]


def test_env_variable_named_like_a_reference_is_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("site:discord.approvers", "from-environment")
    cfg = _write(tmp_path, _inventory(tmp_path), _site())

    assert _plugin_config(load_inventory(cfg))["approvers"] == [APPROVER_A, APPROVER_B]


def test_env_expansion_runs_before_site_substitution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXAMPLE_SITE_DIR", str(tmp_path / "private"))
    data = _inventory(tmp_path, site_values="${EXAMPLE_SITE_DIR}/site.json")
    cfg = _write(tmp_path, data, _site())

    layers = load_inventory_layers(cfg)

    assert layers.site is not None
    assert layers.site.path == tmp_path / "private" / "site.json"


def test_substitution_runs_before_the_runtime_pin_overlay(tmp_path: Path) -> None:
    data = _inventory(tmp_path, runtime_pins="private/pins.json")
    data["instances"][0]["image"]["tag"] = "${site:image.tag}"
    cfg = _write(tmp_path, data, _site({**_values(), "image.tag": TAG}))
    pins = tmp_path / "private" / "pins.json"
    pins.write_text(
        json.dumps(
            {"version": 1, "cluster": "example-cad", "members": {"lead": {"image": {"tag": TAG}}}}
        ),
        encoding="utf-8",
    )
    pins.chmod(FILE_MODE)

    layers = load_inventory_layers(cfg)

    # Equal values are allowed by the overlay; an unsubstituted reference would conflict.
    assert layers.inventory.instances[0].image.tag == TAG
    assert layers.raw["instances"][0]["image"]["tag"] == TAG


def test_inventory_without_site_values_is_unchanged(tmp_path: Path) -> None:
    data = _inventory(tmp_path)
    del data["site_values"]
    data["instances"][0]["plugins"][0]["config"] = {"liveReads": False}
    cfg = _write(tmp_path, data)

    layers = load_inventory_layers(cfg)

    assert layers.site is None
    assert layers.inventory.site_values is None
    assert layers.raw == layers.merged
    assert "site_values" not in layers.raw


# --- validate ---------------------------------------------------------------------------


def test_validate_reports_counts_and_unused_names_only(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), _site())

    result = runner.invoke(app, ["validate", "-c", str(cfg)])

    assert result.exit_code == 0, result.output
    site_path = tmp_path / "private" / "site.json"
    assert f"site values: {site_path} (3 values, 1 references)" in result.output
    assert (
        f"WARNING: site values file {site_path}: unused values: discord.guild, discord.lead_channel"
    ) in result.output
    for value in (GUILD, CHANNEL, APPROVER_A, APPROVER_B):
        assert value not in result.output


def test_validate_prints_readable_warning_for_site_file(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _inventory(tmp_path), _site(), mode=0o644)

    result = runner.invoke(app, ["validate", "-c", str(cfg)])

    assert result.exit_code == 0, result.output
    assert "WARNING: site values file" in result.output
    assert "group- or world-readable" in result.output
