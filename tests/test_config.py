from pathlib import Path

import pytest

from clawake.config import load_inventory


def test_load_example_inventory() -> None:
    inventory = load_inventory(Path("examples/staff/team.yml"))
    assert inventory.cluster.mode == "single_host"
    assert len(inventory.instances) == 2
    assert {instance.role for instance in inventory.instances} == {"product_owner", "developer"}
    for instance in inventory.instances:
        assert instance.workspace_path.endswith("/workspace")
        assert instance.team_definition_path.endswith("/role")


def test_invalid_dns_server_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "invalid-dns.yml"
    config.write_text(
        """
cluster: {name: c, primary_host: local}
hosts: [{name: local}]
instances:
  - name: one
    host: local
    role: developer
    workspace_path: /srv/one/workspace
    team_definition_path: /srv/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: example.invalid/openclaw, tag: "1"}
    dns_servers: [not-an-address]
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="DNS server must be"):
        load_inventory(config)


def test_legacy_name_that_could_be_a_command_option_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "invalid-legacy-name.yml"
    config.write_text(
        """
cluster: {name: c, primary_host: local}
hosts: [{name: local}]
instances:
  - name: one
    legacy_names: [--help]
    host: local
    role: developer
    workspace_path: /srv/one/workspace
    team_definition_path: /srv/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: example.invalid/openclaw, tag: "1"}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid legacy instance name"):
        load_inventory(config)


def test_inventory_defaults_project_root_to_repository() -> None:
    inventory = load_inventory(Path("personal-team/team.yml"))
    repository_root = Path.cwd().resolve()

    navigator = next(item for item in inventory.instances if item.name == "alltags-navigator")
    assert navigator.workspace_path == str(repository_root / "personal-team/workspaces/navigator")
    assert navigator.team_definition_path == str(repository_root / "personal-team/roles/navigator")

    pflegebetreuer = next(item for item in inventory.instances if item.name == "pflegebetreuer")
    assert pflegebetreuer.legacy_names == ["fokus-partner"]
    whatsapp = next(item for item in pflegebetreuer.plugins if item.id == "whatsapp")
    assert whatsapp.source_type == "npm"
    assert whatsapp.npm_spec == "@openclaw/whatsapp@2026.9.2"

    plugin = next(item for item in pflegebetreuer.plugins if item.id == "pflege-google-limited")
    assert plugin.id == "pflege-google-limited"
    assert plugin.artifact_path == str(
        repository_root / "personal-team/plugins/pflege-google-limited/releases/"
        "pflege-google-limited-0.6.0-openclaw-2026.9.2.tgz"
    )
    assert plugin.container_path == "/opt/clawake/plugins/pflege-google-limited.tgz"

    vault = next(item for item in pflegebetreuer.plugins if item.id == "pflege-vault")
    assert vault.sha256 == (
        "sha256:5ae4df070439ac2a7bcd281fe4c3f8c8d70efb41bb57a59180ecde58b5e59a9f"
    )
    assert vault.config["masterKey"] == {
        "source": "store",
        "provider": "default",
        "id": "PFLEGE_VAULT_MASTER_KEY",
    }
    assert pflegebetreuer.agent_tool_allow == {"main": ["pflege_vault"]}


def test_port_collision_validation(tmp_path: Path) -> None:
    collision = tmp_path / "collision.yaml"
    collision.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: /srv/a/one/workspace
    team_definition_path: /srv/a/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: One}
    ports:
      - {bind_address: 127.0.0.1, host_port: 9010, container_port: 8080, protocol: tcp}
  - name: two
    host: a
    role: product_owner
    workspace_path: /srv/a/two/workspace
    team_definition_path: /srv/a/two/role
    quadlet_path: two.container
    container_name: two
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: Two}
    ports:
      - {bind_address: 127.0.0.1, host_port: 9010, container_port: 8080, protocol: tcp}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Port collision"):
        load_inventory(collision)


def test_tcp_udp_same_port_allowed(tmp_path: Path) -> None:
    cfg = tmp_path / "mixed-protocol.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: /srv/a/one/workspace
    team_definition_path: /srv/a/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: One}
    ports:
      - {bind_address: 127.0.0.1, host_port: 9010, container_port: 8080, protocol: tcp}
  - name: two
    host: a
    role: product_owner
    workspace_path: /srv/a/two/workspace
    team_definition_path: /srv/a/two/role
    quadlet_path: two.container
    container_name: two
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: Two}
    ports:
      - {bind_address: 127.0.0.1, host_port: 9010, container_port: 8080, protocol: udp}
""",
        encoding="utf-8",
    )

    inventory = load_inventory(cfg)
    assert len(inventory.instances) == 2


def test_storage_path_collision_validation(tmp_path: Path) -> None:
    cfg = tmp_path / "collision-paths.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: /srv/shared/workspace
    team_definition_path: /srv/a/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: One}
  - name: two
    host: a
    role: product_owner
    workspace_path: /srv/a/two/workspace
    team_definition_path: /srv/shared/workspace
    quadlet_path: two.container
    container_name: two
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: Two}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Path collision"):
        load_inventory(cfg)


def test_relative_workspace_path_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "relative-path.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: srv/a/one/workspace
    team_definition_path: /srv/a/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be an absolute path"):
        load_inventory(cfg)


def test_mount_source_colon_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "unsafe-mount.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: /srv/a/one/workspace
    team_definition_path: /srv/a/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    mounts:
      - {source: /srv/a:bad, target: /data}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not contain ':'"):
        load_inventory(cfg)


def test_gateway_runtime_requires_explicit_bridge_port_mapping(tmp_path: Path) -> None:
    cfg = tmp_path / "gateway-runtime-missing-port.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: /srv/a/one/workspace
    team_definition_path: /srv/a/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    gateway_runtime:
      enabled: true
      bind: loopback
      bridge_container_port: 18790
    ports:
      - {bind_address: 127.0.0.1, host_port: 9010, container_port: 18789, protocol: tcp}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing container ports"):
        load_inventory(cfg)


def test_gateway_runtime_only_requires_gateway_port_by_default(tmp_path: Path) -> None:
    cfg = tmp_path / "gateway-runtime.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: a
  mode: single_host
  primary_host: local
hosts:
  - name: local
instances:
  - name: one
    host: local
    role: developer
    workspace_path: /srv/a/one/workspace
    team_definition_path: /srv/a/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    gateway_runtime:
      enabled: true
      bind: loopback
    ports:
      - {bind_address: 127.0.0.1, host_port: 9010, container_port: 18789, protocol: tcp}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )

    assert load_inventory(cfg).instances[0].gateway_runtime.bridge_container_port is None


def test_project_root_env_expands_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "env-paths.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: ${CLAWAKE_PROJECT_ROOT}/examples/workspaces/dev
    team_definition_path: ${CLAWAKE_PROJECT_ROOT}/examples/roles/dev
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWAKE_PROJECT_ROOT", "/tmp/project")

    inventory = load_inventory(cfg)
    instance = inventory.instances[0]
    assert instance.workspace_path == "/tmp/project/examples/workspaces/dev"
    assert instance.team_definition_path == "/tmp/project/examples/roles/dev"


def test_legacy_workspace_root_env_remains_compatible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "legacy-env-paths.yaml"
    cfg.write_text(
        """
version: 1
cluster: {name: c, mode: single_host, primary_host: a}
hosts:
  - {name: a}
instances:
  - name: one
    host: a
    role: developer
    workspace_path: ${CLAWAKE_WORKSPACE_ROOT}/workspace
    team_definition_path: ${CLAWAKE_WORKSPACE_ROOT}/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAWAKE_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("CLAWAKE_WORKSPACE_ROOT", "/tmp/legacy-project")

    instance = load_inventory(cfg).instances[0]

    assert instance.workspace_path == "/tmp/legacy-project/workspace"
    assert instance.team_definition_path == "/tmp/legacy-project/role"


def test_team_definition_path_must_be_absolute(tmp_path: Path) -> None:
    cfg = tmp_path / "invalid-team-definition-path.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: /srv/a/one/workspace
    team_definition_path: srv/a/one/role
    quadlet_path: one.container
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be an absolute path"):
        load_inventory(cfg)


def test_quadlet_path_must_end_with_container(tmp_path: Path) -> None:
    cfg = tmp_path / "invalid-quadlet-path.yaml"
    cfg.write_text(
        """
version: 1
cluster:
  name: c
  mode: single_host
  primary_host: a
hosts:
  - name: a
instances:
  - name: one
    host: a
    role: developer
    workspace_path: /srv/a/one/workspace
    team_definition_path: /srv/a/one/role
    quadlet_path: one.service
    container_name: one
    image: {repository: ghcr.io/x, tag: "1"}
    dashboard: {friendly_name: One}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must end with '.container'"):
        load_inventory(cfg)
