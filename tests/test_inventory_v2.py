from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from clawake.cli import app
from clawake.config import Inventory, ServiceSpec, load_inventory
from clawake.services.deployment import plan_deployment
from clawake.services.render import render_inventory


def service(name="api", **changes):
    return {
        "name": name,
        "host": "local",
        "image": {"repository": "example.invalid/api", "tag": "1.0.0"},
        "networks": ["team"],
        **changes,
    }


@pytest.fixture
def data():
    return {
        "version": 2,
        "cluster": {"name": "example", "primary_host": "local"},
        "hosts": [{"name": "local"}],
        "networks": [{"name": "team"}],
        "services": [service()],
    }


@pytest.fixture
def mixed(data):
    member = load_inventory(Path("examples/staff/team.yml")).instances[0].model_dump()
    member.update(
        name="member",
        host="local",
        container_name="member",
        quadlet_path="member.container",
        ports=[],
        networks=["team"],
        legacy_names=[],
        gateway_runtime={"enabled": False},
    )
    data["instances"] = [member]
    return data


@pytest.mark.parametrize(
    "filename",
    [
        "examples/staff/team.yml",
        "personal-team/team.yml",
        "robotics-team/team.yml",
    ],
)
def test_v1_inventories_round_trip_and_keep_explicit_network_hosts(filename):
    inventory = load_inventory(Path(filename))
    assert inventory.version == 1
    assert inventory.services == []
    assert Inventory.model_validate(inventory.model_dump()) == inventory
    assert all(network.host for network in inventory.networks)


@pytest.mark.parametrize(
    "filename",
    [
        "examples/staff/team.yml",
        "personal-team/team.yml",
        "robotics-team/team.yml",
    ],
)
def test_existing_instances_are_accepted_in_v2(filename):
    original = load_inventory(Path(filename))
    upgraded = Inventory.model_validate({**original.model_dump(), "version": 2})
    assert upgraded.instances == original.instances
    assert upgraded.networks == original.networks


def test_v2_defaults_do_not_mutate_input(data):
    original = deepcopy(data)
    inventory = Inventory.model_validate(data)
    assert data == original
    assert inventory.instances == []
    assert inventory.networks[0].host == "local"
    assert inventory.services[0].container_name == "api"
    assert inventory.services[0].quadlet_path == "api.container"
    assert Inventory.model_validate(inventory.model_dump()) == inventory


def test_v2_fixture_expands_and_normalizes_paths_without_creating_them(tmp_path, monkeypatch):
    project = tmp_path / "project"
    monkeypatch.setenv("CLAWAKE_PROJECT_ROOT", str(project))
    inventory = load_inventory(Path("tests/fixtures/services-v2.yml"))
    api = inventory.services[0]
    assert api.mounts[1].source == str(tmp_path / "ledger")
    assert api.backup_policy.paths[1] == str(tmp_path / "ledger")
    assert api.env_files == [str(project / "config/api.env")]
    assert api.health.scheme == "https"
    assert api.quadlet_artifact_paths == ["api.container", "api-cache.volume"]
    assert api.secrets[0].container_path == "/run/secrets/provider-key"
    assert not list(tmp_path.iterdir())


def test_mixed_inventory_and_shared_secret_references(mixed):
    mixed["services"] = [
        service("api", depends_on=["db"], secrets=[{"name": "shared-key"}]),
        service("db", secrets=[{"name": "shared-key"}]),
    ]
    mixed["instances"][0]["depends_on"] = ["api"]
    inventory = Inventory.model_validate(mixed)
    assert inventory.dependency_order() == ["db", "api", "member"]
    inventory.validate_secret_references({"shared-key"})
    with pytest.raises(ValueError, match="Missing Podman secret references: shared-key"):
        inventory.validate_secret_references(set())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=3),
        lambda d: d.update(version=1, instances=[]),  # services cannot enter v1
        lambda d: d["networks"][0].update(host="missing"),
        lambda d: d["services"][0].update(host="missing"),
        lambda d: d["services"][0].update(networks=["missing"]),
        lambda d: d["services"][0].update(networks=["team", "team"]),
        lambda d: d["services"].append(service()),
        lambda d: d["networks"].append({"name": "team"}),
        lambda d: d["networks"].append({"name": "api"}),
        lambda d: d["hosts"].append({"name": "local"}),
        lambda d: d["cluster"].update(primary_host="missing"),
        lambda d: d["hosts"][0].update(quadlet_root="relative"),
    ],
)
def test_invalid_topology_rejected(data, mutate):
    mutate(data)
    with pytest.raises(ValidationError):
        Inventory.model_validate(data)


def test_v1_requires_explicit_network_host_and_instances():
    with pytest.raises(ValidationError):
        Inventory.model_validate(
            {
                "version": 1,
                "cluster": {"name": "c", "primary_host": "local"},
                "hosts": [{"name": "local"}],
                "instances": [],
                "networks": [{"name": "team"}],
            }
        )
    with pytest.raises(ValidationError):
        Inventory.model_validate(
            {
                "version": 1,
                "cluster": {"name": "c", "primary_host": "local"},
                "hosts": [{"name": "local"}],
            }
        )


@pytest.mark.parametrize("name", ["../api", "-api", "api-", "UPPER", "api.service", "a" * 64, ""])
def test_invalid_service_names(name):
    with pytest.raises(ValidationError):
        ServiceSpec.model_validate(service(name))


@pytest.mark.parametrize(
    "changes",
    [
        {"container_name": "custom"},
        {"quadlet_path": "custom.container"},
        {"networks": []},
        {"command": []},
        {"command": "serve --port 8080"},
        {"command": ["serve\nExecStart=bad"]},
        {"restart_policy": "unless-stopped"},
        {"image": {"repository": "https://example.invalid/api", "tag": "1"}},
        {"image": {"repository": "example.invalid/api:other", "tag": "1"}},
        {"image": {"repository": "example.invalid/api", "tag": "latest"}},
        {"image": {"repository": "example.invalid/api", "tag": "1", "digest": "sha256:short"}},
        {"image": {"repository": "example.invalid/api", "tag": "1\nExec=bad"}},
        {"health": {"port": 0}},
        {"health": {"port": 8080, "scheme": "ftp"}},
        {"health": {"port": 8080, "path": "relative"}},
        {"health": {"port": 8080, "retries": 0}},
        {"health": {"port": 8080, "timeout_seconds": -1}},
        {"health": {"port": 8080, "interval_seconds": 0}},
        {"interfaces": {"api": {"url": "http://user:password@api:8080"}}},
        {"interfaces": {"api": {"url": "http://api/?token=hidden"}}},
        {"interfaces": {"api": {"url": "http://api/#token"}}},
        {"interfaces": {"api": {"url": "file:///tmp"}}},
        {"interfaces": {"api": {"url": "http://api:99999"}}},
        {"interfaces": {"api": {"url": "http://api/\nextra"}}},
        {"tls": {"mode": "managed"}},
    ],
)
def test_service_contract_rejects_invalid_or_unimplemented_fields(changes):
    with pytest.raises(ValidationError):
        ServiceSpec.model_validate(service(**changes))


def test_digest_pins_floating_tag_and_registry_port_is_supported():
    api = ServiceSpec.model_validate(
        service(
            image={
                "repository": "localhost:5000/example/api",
                "tag": "latest",
                "digest": "sha256:" + "a" * 64,
            }
        )
    )
    assert api.image.repository == "localhost:5000/example/api"


@pytest.mark.parametrize(
    "changes",
    [
        {"mounts": [{"source": "relative", "target": "/data"}]},
        {"mounts": [{"source": "/", "target": "/data"}]},
        {"mounts": [{"source": "/srv/data", "target": "/"}]},
        {"mounts": [{"source": "/srv/data:ro", "target": "/data"}]},
        {"mounts": [{"source": "/srv/data", "target": "/data/../escape"}]},
        {"mounts": [{"source": "/srv/data", "target": "/data/./child"}]},
        {"mounts": [{"source": "/srv/data", "target": "/data\nExec=bad"}]},
        {"mounts": [{"source": "/srv/%h", "target": "/data"}]},
        {"mounts": [{"source": "/srv/a b", "target": "/data"}]},
        {"env_files": ["${UNSET_TEST_VAR}/api.env"]},
        {"env_files": ["/srv/api.env", "/srv/../srv/api.env"]},
        {"backup_policy": {"paths": ["relative"]}},
        {"volumes": [{"name": "../data", "target": "/data"}]},
        {"volumes": [{"name": "data", "target": "/one"}, {"name": "data", "target": "/two"}]},
        {"volumes": [{"name": "one", "target": "/data"}, {"name": "two", "target": "/data/"}]},
        {
            "mounts": [{"source": "/srv/data", "target": "/data"}],
            "volumes": [{"name": "data", "target": "/data"}],
        },
        {"secrets": [{"name": "../key"}]},
        {"secrets": [{"name": "key", "target": "/absolute"}]},
        {"secrets": [{"name": "key"}, {"name": "key"}]},
        {"secrets": [{"name": "key"}, {"name": "other", "target": "key"}]},
        {"secrets": [{"name": "key"}], "mounts": [{"source": "/srv/data", "target": "/run"}]},
        {"secrets": [{"name": "key"}], "volumes": [{"name": "data", "target": "/run/secrets/key"}]},
    ],
)
def test_invalid_storage_and_secret_references(changes, monkeypatch):
    monkeypatch.delenv("UNSET_TEST_VAR", raising=False)
    with pytest.raises(ValidationError):
        ServiceSpec.model_validate(service(**changes))


def test_shared_volume_basename_is_service_scoped(data):
    data["services"] = [
        service(name, volumes=[{"name": "data", "target": "/data"}]) for name in ("api", "db")
    ]
    inventory = Inventory.model_validate(data)
    assert [s.volume_name("data") for s in inventory.services] == ["api-data", "db-data"]


def test_derived_volume_names_cannot_collide_across_services(data):
    data["services"] = [
        service("one-two", volumes=[{"name": "data", "target": "/data"}]),
        service("one", volumes=[{"name": "two-data", "target": "/data"}]),
    ]
    with pytest.raises(ValidationError, match="collision"):
        Inventory.model_validate(data)


def test_service_volume_cannot_reuse_instance_podman_volume_name(mixed):
    # Different Quadlet filenames can still claim the same Podman volume.
    mixed["instances"][0]["container_name"] = "api-data"
    mixed["services"][0]["volumes"] = [{"name": "data-state", "target": "/data"}]
    with pytest.raises(ValidationError, match="Podman volume name collision"):
        Inventory.model_validate(mixed)


def test_instance_mount_cannot_shadow_managed_workspace(mixed):
    mixed["instances"][0]["mounts"] = [{"source": "/srv/other", "target": "/workspace"}]
    with pytest.raises(ValidationError, match="Duplicate mount target"):
        Inventory.model_validate(mixed)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["services"][0].update(name="member"),
        lambda d: d["instances"][0].update(container_name="api"),
        lambda d: d["instances"][0].update(quadlet_path="api.container"),
        lambda d: d["instances"][0].update(quadlet_path="../member.container"),
        lambda d: d["instances"][0].update(quadlet_path="/tmp/member.container"),
        lambda d: d["instances"][0].update(legacy_names=["api"]),
        lambda d: d["services"].append(service("team-network")),
        lambda d: d["services"].append(service("member-state-volume")),
        lambda d: d["services"].append(service("api-data-volume")),
    ],
)
def test_cross_resource_names_and_artifacts_rejected(mixed, mutate):
    mixed["services"][0]["volumes"] = [{"name": "data", "target": "/data"}]
    mutate(mixed)
    with pytest.raises(ValidationError):
        Inventory.model_validate(mixed)


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "::", "::ffff:127.0.0.1"])
def test_ports_collide_between_member_and_service(mixed, address):
    mixed["instances"][0]["ports"] = [{"host_port": 8080, "container_port": 8080}]
    mixed["services"][0]["ports"] = [
        {
            "bind_address": address,
            "host_port": 8080,
            "container_port": 9000,
        }
    ]
    with pytest.raises(ValidationError, match="Port collision"):
        Inventory.model_validate(mixed)


@pytest.mark.parametrize(
    "second",
    [
        {"bind_address": "127.0.0.2"},
        {"protocol": "udp"},
        {"host_port": 8081},
    ],
)
def test_distinct_bindings_and_protocols_allowed(data, second):
    first = {"host_port": 8080, "container_port": 8080}
    data["services"][0]["ports"] = [first, {**first, **second}]
    Inventory.model_validate(data)


def test_duplicate_ports_within_service_rejected(data):
    data["services"][0]["ports"] = [{"host_port": 8080, "container_port": 8080}] * 2
    with pytest.raises(ValidationError, match="Port collision"):
        Inventory.model_validate(data)


@pytest.mark.parametrize("dependencies", [["missing"], ["api"], ["db", "db"], ["team"]])
def test_invalid_dependency_references(data, dependencies):
    data["services"] += [service("db")]
    data["services"][0]["depends_on"] = dependencies
    with pytest.raises(ValidationError, match="dependency|dependencies"):
        Inventory.model_validate(data)


def test_cycle_through_member_is_rejected(mixed):
    mixed["instances"][0]["depends_on"] = ["api"]
    mixed["services"][0]["depends_on"] = ["member"]
    with pytest.raises(ValidationError, match="Cyclic"):
        Inventory.model_validate(mixed)


def test_transitive_service_cycle_rejected(data):
    data["services"] = [
        service("api", depends_on=["db"]),
        service("db", depends_on=["cache"]),
        service("cache", depends_on=["api"]),
    ]
    with pytest.raises(ValidationError, match="Cyclic"):
        Inventory.model_validate(data)


def test_order_is_stable_across_declaration_order(data):
    data["services"] = [service("api", depends_on=["db"]), service("db"), service("cache")]
    expected = Inventory.model_validate(data).dependency_order()
    data["services"].reverse()
    assert Inventory.model_validate(data).dependency_order() == expected == ["cache", "db", "api"]


def test_v1_rejects_new_dependencies(mixed):
    mixed.update(version=1, services=[], networks=[{"name": "team", "host": "local"}])
    mixed["instances"][0]["depends_on"] = ["missing"]
    with pytest.raises(ValidationError, match="require inventory version 2"):
        Inventory.model_validate(mixed)


def test_v2_cli_validate_is_offline_and_hides_rejected_secret_values(data, tmp_path, monkeypatch):
    from clawake import cli

    config = tmp_path / "team.yml"
    config.write_text(yaml.safe_dump(data))
    forbidden = Mock(side_effect=AssertionError("validation must be offline"))
    monkeypatch.setattr(cli, "plan_deployment", forbidden)
    monkeypatch.setattr(cli, "SystemdService", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    result = CliRunner().invoke(app, ["validate", "-c", str(config)])
    assert result.exit_code == 0, result.output
    assert "1 service(s)" in result.output
    assert "Model validation only" in result.output
    assert list(tmp_path.iterdir()) == [config]
    data["services"][0]["secrets"] = [{"name": "key", "value": "DO-NOT-PRINT-TEST-MARKER"}]
    config.write_text(yaml.safe_dump(data))
    rejected = CliRunner().invoke(app, ["validate", "-c", str(config)])
    assert rejected.exit_code != 0
    assert "DO-NOT-PRINT-TEST-MARKER" not in rejected.output
    forbidden.assert_not_called()


def test_yaml_parser_errors_do_not_echo_input(tmp_path):
    config = tmp_path / "broken.yml"
    config.write_text("version: 2\nsecrets: [DO-NOT-PRINT-TEST-MARKER\n")
    result = CliRunner().invoke(app, ["validate", "-c", str(config)])
    assert result.exit_code != 0
    assert "invalid YAML" in result.output
    assert "DO-NOT-PRINT-TEST-MARKER" not in result.output


def test_member_selection_does_not_hide_invalid_services(mixed, tmp_path):
    mixed["services"][0]["networks"] = ["unknown"]
    config = tmp_path / "broken.yml"
    config.write_text(yaml.safe_dump(mixed))
    result = CliRunner().invoke(app, ["validate", "-c", str(config), "-m", "member"])
    assert result.exit_code != 0
    assert "network" in result.output


@pytest.mark.parametrize(
    "command,args",
    [
        ("setup", []),
        ("setup-quadlets", ["--execute"]),
        ("restart", ["--execute"]),
        ("teardown", ["--execute"]),
        ("status", []),
        ("logs", ["-m", "member"]),
        ("upgrade", ["-m", "member", "--to", "2.0.0"]),
        ("onboard", ["-m", "member"]),
        ("dashboard", []),
        ("sync-plugins", []),
    ],
)
def test_runtime_commands_reject_v2_before_any_side_effect(
    mixed,
    tmp_path,
    monkeypatch,
    command,
    args,
):
    from clawake import cli

    config = tmp_path / "team.yml"
    config.write_text(yaml.safe_dump(mixed))
    forbidden = Mock(side_effect=AssertionError("runtime must not be touched"))
    monkeypatch.setattr(cli, "SystemdService", forbidden)
    monkeypatch.setattr(cli, "plan_deployment", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    result = CliRunner().invoke(app, [command, "-c", str(config), *args])
    assert result.exit_code != 0
    assert "validation-only" in result.output
    forbidden.assert_not_called()
    assert list(tmp_path.iterdir()) == [config]


def test_public_rendering_and_planning_reject_v2_without_writes(data, tmp_path):
    inventory = Inventory.model_validate(data)
    with pytest.raises(ValueError, match="validation-only"):
        render_inventory(inventory, tmp_path / "out", Path("templates"))
    with pytest.raises(ValueError, match="validation-only"):
        plan_deployment(inventory, inventory.instances, Path("templates"))
    assert not list(tmp_path.iterdir())
