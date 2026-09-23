from __future__ import annotations

import os
import re
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, ConfigDict, Field, JsonValue, field_validator, model_validator
from pydantic import BaseModel as PydanticBaseModel

from clawake.inventory_validation import (
    dependency_order,
    host_path,
    interface_url,
    resource_name,
    single_line,
    target_path,
    validate_v2_inventory,
)


class BaseModel(PydanticBaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


ResourceName = Annotated[str, AfterValidator(resource_name)]
HostPath = Annotated[str, AfterValidator(host_path)]
TargetPath = Annotated[str, AfterValidator(target_path)]


def bind_addresses_overlap(first: str, second: str) -> bool:
    """Conservatively reserve IPv6 wildcard for dual-stack listeners too."""
    a, b = ip_address(first), ip_address(second)
    a = getattr(a, "ipv4_mapped", None) or a
    b = getattr(b, "ipv4_mapped", None) or b
    return a == b or str(a) == "::" or str(b) == "::" or (
        a.version == b.version and (a.is_unspecified or b.is_unspecified)
    )


def _project_root_from_environment(default: str = "") -> str:
    return os.environ.get(
        "CLAWAKE_PROJECT_ROOT",
        os.environ.get("CLAWAKE_WORKSPACE_ROOT", default),
    )


class ClusterSpec(BaseModel):
    name: str
    mode: Literal["single_host"] = "single_host"
    primary_host: str
    description: str | None = None


class HostSpec(BaseModel):
    name: str
    quadlet_root: str = "~/.config/containers/systemd"
    ssh_target: str | None = None


class NetworkSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    host: str

    @property
    def quadlet_path(self) -> str:
        return f"{self.name}.network"


class ImageSpec(BaseModel):
    repository: str
    tag: str
    digest: str | None = None
    known_good_digest: str | None = None
    known_good_tag: str | None = None


class PortSpec(BaseModel):
    bind_address: str = "127.0.0.1"
    host_port: int = Field(ge=1, le=65535)
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"

    @field_validator("bind_address")
    @classmethod
    def normalize_address(cls, value: str) -> str:
        return str(ip_address(value))


class MountSpec(BaseModel):
    source: str
    target: str
    read_only: bool = False


class PluginSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    artifact_path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    npm_spec: str | None = None
    integrity: str | None = Field(default=None, pattern=r"^sha512-[A-Za-z0-9+/]+={0,2}$")
    config: dict[str, JsonValue] = Field(default_factory=dict)
    enabled: bool = True
    custom_ui: bool = False

    @model_validator(mode="after")
    def validate_source(self) -> PluginSpec:
        archive_fields = (self.artifact_path, self.sha256)
        npm_fields = (self.npm_spec, self.integrity)
        has_archive = any(value is not None for value in archive_fields)
        has_npm = any(value is not None for value in npm_fields)
        if has_archive == has_npm:
            raise ValueError(
                "plugin must declare exactly one source: artifact_path + sha256 or "
                "npm_spec + integrity"
            )
        if has_archive and not all(value is not None for value in archive_fields):
            raise ValueError("archive plugins require artifact_path and sha256")
        if has_npm and not all(value is not None for value in npm_fields):
            raise ValueError("npm plugins require npm_spec and integrity")
        if self.npm_spec and not re.fullmatch(
            r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@"
            r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?",
            self.npm_spec,
        ):
            raise ValueError("npm_spec must contain an exact package version")
        return self

    @property
    def source_type(self) -> Literal["archive", "npm"]:
        return "archive" if self.artifact_path is not None else "npm"

    @property
    def container_path(self) -> str:
        return f"/opt/clawake/plugins/{self.id}.tgz"

    @property
    def install_target(self) -> str:
        if self.source_type == "archive":
            return self.container_path
        assert self.npm_spec is not None
        return f"npm:{self.npm_spec}"


class HealthSpec(BaseModel):
    path: str = "/health"
    interval_seconds: int = 30
    timeout_seconds: int = 5
    retries: int = 3


class UpdatePolicy(BaseModel):
    channel: str = "stable"
    strategy: str = "manual"
    auto_apply: bool = False


class BackupPolicy(BaseModel):
    enabled: bool = True
    pre_mutation: bool = True
    retention: int = Field(default=5, ge=1)
    paths: list[str] = Field(default_factory=list)


class GatewayRuntimeSpec(BaseModel):
    enabled: bool = False
    bind: Literal["loopback", "lan", "tailnet", "auto", "custom"] = "lan"
    gateway_container_port: int = Field(default=18789, ge=1, le=65535)
    bridge_container_port: int | None = Field(default=None, ge=1, le=65535)


class DashboardMeta(BaseModel):
    friendly_name: str
    owner: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class ServiceImageSpec(ImageSpec):
    """Explicit service image reference; legacy instance images stay compatible."""

    @model_validator(mode="after")
    def validate_reference(self) -> ServiceImageSpec:
        parts = self.repository.split("/")
        if len(parts) > 1 and ":" in parts[0]:
            registry, port = parts[0].rsplit(":", 1)
            if not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError("Invalid image registry port")
            parts[0] = registry
        if any(not re.fullmatch(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*", p) for p in parts):
            raise ValueError("Invalid image repository")
        for tag in (self.tag, self.known_good_tag):
            if tag is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
                raise ValueError("Invalid image tag")
        for digest in (self.digest, self.known_good_digest):
            if digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Image digest must be a complete SHA-256 digest")
        if self.tag.lower() in {"latest", "stable", "edge", "main", "dev"} and not self.digest:
            raise ValueError("Floating image tags require a digest")
        return self


class ServiceMountSpec(MountSpec):
    source: HostPath
    target: TargetPath


class VolumeSpec(BaseModel):
    name: ResourceName
    target: TargetPath
    read_only: bool = False


class SecretRef(BaseModel):
    name: ResourceName
    target: ResourceName | None = None

    @property
    def container_path(self) -> str:
        return f"/run/secrets/{self.target or self.name}"


class ServiceHealthSpec(HealthSpec):
    scheme: Literal["http", "https"] = "http"
    port: int = Field(ge=1, le=65535)
    path: str = Field(default="/health/ready", pattern=r"^/[A-Za-z0-9/_.-]*$")
    interval_seconds: int = Field(default=30, ge=1)
    timeout_seconds: int = Field(default=5, ge=1)
    retries: int = Field(default=3, ge=1)


class ServiceInterfaceSpec(BaseModel):
    url: Annotated[str, AfterValidator(interface_url)]


class ServiceBackupPolicy(BackupPolicy):
    paths: list[HostPath] = Field(default_factory=list)


class ServiceSpec(BaseModel):
    name: ResourceName
    host: ResourceName
    image: ServiceImageSpec
    networks: list[ResourceName] = Field(min_length=1)
    command: list[Annotated[str, AfterValidator(single_line)]] | None = Field(
        default=None, min_length=1,
    )
    env_files: list[HostPath] = Field(default_factory=list)
    ports: list[PortSpec] = Field(default_factory=list)
    mounts: list[ServiceMountSpec] = Field(default_factory=list)
    volumes: list[VolumeSpec] = Field(default_factory=list)
    secrets: list[SecretRef] = Field(default_factory=list)
    health: ServiceHealthSpec | None = None
    depends_on: list[ResourceName] = Field(default_factory=list)
    restart_policy: Literal["always", "on-failure", "no"] = "always"
    backup_policy: ServiceBackupPolicy = Field(default_factory=ServiceBackupPolicy)
    interfaces: dict[ResourceName, ServiceInterfaceSpec] = Field(default_factory=dict)
    dashboard: DashboardMeta | None = None

    @property
    def container_name(self) -> str:
        return self.name

    @property
    def quadlet_path(self) -> str:
        return f"{self.name}.container"

    def volume_name(self, name: str) -> str:
        return f"{self.name}-{name}"

    @property
    def quadlet_artifact_paths(self) -> list[str]:
        return [self.quadlet_path, *(
            f"{self.volume_name(v.name)}.volume" for v in self.volumes
        )]

    @model_validator(mode="after")
    def validate_storage(self) -> ServiceSpec:
        for values, label in (
            ([v.name for v in self.volumes], "volume names"),
            ([s.name for s in self.secrets], "secret references"),
            ([s.container_path for s in self.secrets], "secret targets"),
            (self.env_files, "environment paths"),
            (self.backup_policy.paths, "backup paths"),
            ([m.target for m in [*self.mounts, *self.volumes]], "mount targets"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label}")
        for secret in self.secrets:
            for mount in [*self.mounts, *self.volumes]:
                if (
                    Path(secret.container_path).is_relative_to(mount.target)
                    or Path(mount.target).is_relative_to(secret.container_path)
                ):
                    raise ValueError("Mount shadows a secret target")
        return self


class SubagentSpec(BaseModel):
    max_spawn_depth: int = Field(default=1, ge=1, le=5)
    max_children_per_agent: int = Field(default=3, ge=1, le=20)
    max_concurrent: int = Field(default=3, ge=1)
    run_timeout_seconds: int = Field(default=900, ge=1)


class PeerSpec(BaseModel):
    instance: str
    inbound_token_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    outbound_token_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")


class A2ASpec(BaseModel):
    enabled: bool = False
    peers: dict[str, PeerSpec] = Field(default_factory=dict)

    @field_validator("peers")
    @classmethod
    def validate_peer_names(cls, value: dict[str, PeerSpec]) -> dict[str, PeerSpec]:
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", key) for key in value):
            raise ValueError("Invalid A2A peer name")
        return value


class OpenClawSpec(BaseModel):
    lead_agent_id: str = Field(default="main", pattern=r"^[a-z][a-z0-9_-]*$")
    subagents: SubagentSpec | None = None
    a2a: A2ASpec = Field(default_factory=A2ASpec)


class InstanceSpec(BaseModel):
    name: str
    legacy_names: list[str] = Field(default_factory=list)
    host: str
    role: Literal["product_owner", "developer"]
    workspace_path: str
    team_definition_path: str
    service_scope: Literal["user"] = "user"
    quadlet_path: str
    container_name: str
    image: ImageSpec
    ports: list[PortSpec] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    openclaw: OpenClawSpec | None = None
    mounts: list[MountSpec] = Field(default_factory=list)
    plugins: list[PluginSpec] = Field(default_factory=list)
    agent_tool_allow: dict[str, list[str]] = Field(default_factory=dict)
    env_files: list[str] = Field(default_factory=list)
    dns_servers: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    public_url: str | None = None
    health: HealthSpec = Field(default_factory=HealthSpec)
    update_policy: UpdatePolicy = Field(default_factory=UpdatePolicy)
    backup_policy: BackupPolicy = Field(default_factory=BackupPolicy)
    gateway_runtime: GatewayRuntimeSpec = Field(default_factory=GatewayRuntimeSpec)
    dashboard: DashboardMeta

    @field_validator("dns_servers")
    @classmethod
    def validate_dns_servers(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                ip_address(value)
            except ValueError as exc:
                raise ValueError(f"DNS server must be an IPv4 or IPv6 address: '{value}'") from exc
        return values

    @field_validator("agent_tool_allow")
    @classmethod
    def validate_agent_tool_allow(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        agent_pattern = re.compile(r"^[A-Za-z0-9_.@-]+$")
        tool_pattern = re.compile(r"^[A-Za-z0-9_.*:-]+$")
        for agent_id, tools in value.items():
            if not agent_pattern.fullmatch(agent_id):
                raise ValueError(f"Invalid agent id in agent_tool_allow: '{agent_id}'")
            if not tools or len(tools) != len(set(tools)):
                raise ValueError(f"agent_tool_allow for '{agent_id}' must be non-empty and unique")
            for tool in tools:
                if not tool_pattern.fullmatch(tool):
                    raise ValueError(f"Invalid tool id in agent_tool_allow: '{tool}'")
        return value

    @field_validator("legacy_names")
    @classmethod
    def validate_legacy_names(cls, values: list[str]) -> list[str]:
        unit_name_pattern = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]*$")
        if len(values) != len(set(values)):
            raise ValueError("legacy_names must be unique")
        for value in values:
            if not unit_name_pattern.fullmatch(value):
                raise ValueError(f"Invalid legacy instance name: '{value}'")
        return values

    @model_validator(mode="after")
    def ensure_backup_defaults(self) -> InstanceSpec:
        if self.name in self.legacy_names:
            raise ValueError(f"instances[{self.name}].legacy_names must not include its name")
        quadlet = Path(self.quadlet_path)
        if quadlet.suffix != ".container":
            raise ValueError(
                f"instances[{self.name}].quadlet_path must end with '.container': "
                f"'{self.quadlet_path}'"
            )
        if not self.backup_policy.paths:
            self.backup_policy.paths = [
                self.workspace_path,
                self.team_definition_path,
            ]
        return self

    @property
    def network_quadlet_path(self) -> str:
        return str(Path(self.quadlet_path).with_suffix(".network"))

    @property
    def runtime_volume_quadlet_path(self) -> str:
        container_path = Path(self.quadlet_path)
        return str(container_path.with_name(f"{container_path.stem}-state.volume"))

    @property
    def quadlet_artifact_paths(self) -> list[str]:
        return [
            self.quadlet_path,
            *([] if self.networks else [self.network_quadlet_path]),
            self.runtime_volume_quadlet_path,
        ]


class Inventory(BaseModel):
    version: Literal[1, 2] = 1
    cluster: ClusterSpec
    hosts: list[HostSpec]
    instances: list[InstanceSpec]
    networks: list[NetworkSpec] = Field(default_factory=list)
    services: list[ServiceSpec] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def v2_defaults(cls, data: object) -> object:
        if not isinstance(data, Mapping) or data.get("version") != 2:
            return data
        result = dict(data)
        result.setdefault("instances", [])
        cluster = result.get("cluster")
        primary = (
            cluster.get("primary_host") if isinstance(cluster, Mapping)
            else getattr(cluster, "primary_host", None)
        )
        networks = result.get("networks")
        if primary and isinstance(networks, list):
            result["networks"] = [
                {"host": primary, **network} if isinstance(network, Mapping) else network
                for network in networks
            ]
        return result

    def dependency_order(self) -> list[str]:
        return dependency_order(self)

    def validate_secret_references(self, available_names: set[str]) -> None:
        required = {secret.name for service in self.services for secret in service.secrets}
        missing = sorted(required - available_names)
        if missing:
            raise ValueError("Missing Podman secret references: " + ", ".join(missing))

    @model_validator(mode="after")
    def validate_inventory(self) -> Inventory:
        def _expand_path(raw_path: str) -> str:
            expanded = os.path.expandvars(raw_path)
            expanded = expanded.replace(
                "${workspaceFolder}",
                _project_root_from_environment(),
            )
            return str(Path(expanded).expanduser())

        def _validate_safe_absolute_path(label: str, raw_path: str) -> str:
            path = Path(_expand_path(raw_path))
            if not path.is_absolute():
                raise ValueError(f"{label} must be an absolute path: '{raw_path}'")
            normalized = str(path)
            if "//" in normalized:
                raise ValueError(f"{label} must not contain repeated slashes: '{raw_path}'")
            parts = set(path.parts)
            if "." in parts or ".." in parts:
                raise ValueError(f"{label} must not contain dot segments: '{raw_path}'")
            return normalized

        host_names = {host.name for host in self.hosts}
        if len(host_names) != len(self.hosts):
            raise ValueError("Duplicate host names are not allowed")

        if len(self.hosts) != 1:
            raise ValueError("single_host mode requires exactly one host")

        if self.cluster.primary_host not in host_names:
            raise ValueError(
                f"cluster.primary_host '{self.cluster.primary_host}' does not match any host"
            )

        if self.version == 1:
            if self.services or any(item.depends_on for item in self.instances):
                raise ValueError("Services and dependencies require inventory version 2")
        else:
            validate_v2_inventory(self)

        instance_names: set[str] = set()
        network_map = {network.name: network for network in self.networks}
        if len(network_map) != len(self.networks):
            raise ValueError("Duplicate network names")
        for network in self.networks:
            if network.host not in host_names:
                raise ValueError(f"Unknown network host: {network.host}")
        container_names: set[tuple[str, str]] = set()
        artifact_paths = {
            (network.host, network.quadlet_path) for network in self.networks
        }
        used_ports: dict[tuple[str, str, int, str], str] = {}
        used_paths: dict[str, tuple[str, str]] = {}

        for instance in self.instances:
            if instance.host not in host_names:
                raise ValueError(
                    f"Instance '{instance.name}' references unknown host '{instance.host}'"
                )
            if instance.name in instance_names:
                raise ValueError(f"Duplicate instance name '{instance.name}'")
            instance_names.add(instance.name)
            container_key = (instance.host, instance.container_name)
            if container_key in container_names:
                raise ValueError(f"Duplicate container name: {instance.container_name}")
            container_names.add(container_key)
            if not instance.networks and instance.container_name in network_map:
                raise ValueError(
                    f"Private/shared Podman network collision: {instance.container_name}"
                )
            if instance.name in {f"{name}-network" for name in network_map}:
                raise ValueError(f"Shared network service collision: {instance.name}")
            if len(set(instance.networks)) != len(instance.networks):
                raise ValueError(f"Duplicate network membership: {instance.name}")
            for name in instance.networks:
                if name not in network_map or network_map[name].host != instance.host:
                    raise ValueError(f"Unknown or cross-host network: {name}")
            for path in instance.quadlet_artifact_paths:
                key = (instance.host, path)
                if key in artifact_paths:
                    raise ValueError(f"Quadlet artifact collision: {path}")
                artifact_paths.add(key)

            _validate_safe_absolute_path(
                f"instances[{instance.name}].workspace_path",
                instance.workspace_path,
            )
            _validate_safe_absolute_path(
                f"instances[{instance.name}].team_definition_path",
                instance.team_definition_path,
            )
            for env_file in instance.env_files:
                _validate_safe_absolute_path(
                    f"instances[{instance.name}].env_files",
                    env_file,
                )
            for mount in instance.mounts:
                source = _validate_safe_absolute_path(
                    f"instances[{instance.name}].mounts.source",
                    mount.source,
                )
                if ":" in source:
                    raise ValueError(
                        f"instances[{instance.name}].mounts.source must not contain ':'"
                    )
                _validate_safe_absolute_path(
                    f"instances[{instance.name}].mounts.target",
                    mount.target,
                )
            plugin_ids: set[str] = set()
            for plugin in instance.plugins:
                if plugin.id in plugin_ids:
                    raise ValueError(
                        f"Instance '{instance.name}' contains duplicate plugin '{plugin.id}'"
                    )
                plugin_ids.add(plugin.id)
                if plugin.artifact_path is not None:
                    artifact = _validate_safe_absolute_path(
                        f"instances[{instance.name}].plugins[{plugin.id}].artifact_path",
                        plugin.artifact_path,
                    )
                    if not artifact.endswith(".tgz"):
                        raise ValueError(f"Plugin artifact for '{plugin.id}' must end with '.tgz'")

            if instance.gateway_runtime.enabled:
                expected = {instance.gateway_runtime.gateway_container_port}
                if instance.gateway_runtime.bridge_container_port is not None:
                    expected.add(instance.gateway_runtime.bridge_container_port)
                actual = {port.container_port for port in instance.ports if port.protocol == "tcp"}
                if not expected.issubset(actual):
                    raise ValueError(
                        f"Instance '{instance.name}' enables gateway_runtime but is missing "
                        f"container ports {sorted(expected)}"
                    )

            for port in instance.ports:
                port_key = (instance.host, port.bind_address, port.host_port, port.protocol)
                for (host, address, number, protocol), owner in used_ports.items():
                    if (
                        host == instance.host and number == port.host_port
                        and protocol == port.protocol
                        and bind_addresses_overlap(address, port.bind_address)
                    ):
                        raise ValueError(
                            f"Port collision on host {instance.host} "
                            f"{port.bind_address}:{port.host_port} "
                            f"between {owner} and {instance.name}"
                        )
                used_ports[port_key] = instance.name

            storage_paths = {
                "workspace_path": instance.workspace_path,
                "team_definition_path": instance.team_definition_path,
            }
            for boundary, raw_path in storage_paths.items():
                normalized = _expand_path(raw_path)
                existing = used_paths.get(normalized)
                if existing is not None:
                    raise ValueError(
                        f"Path collision for '{normalized}' between "
                        f"{existing[0]}.{existing[1]} and {instance.name}.{boundary}"
                    )
                used_paths[normalized] = (instance.name, boundary)

        instance_map = {instance.name: instance for instance in self.instances}
        for instance in self.instances:
            settings = instance.openclaw
            if settings is None:
                continue
            if settings.a2a.peers and not settings.a2a.enabled:
                raise ValueError(f"A2A peers require enabled A2A: {instance.name}")
            inbound_refs = [p.inbound_token_env for p in settings.a2a.peers.values()]
            if len(set(inbound_refs)) != len(inbound_refs):
                raise ValueError(f"A2A inbound tokens must be unique per peer: {instance.name}")
            if settings.a2a.enabled and (
                not instance.gateway_runtime.enabled or instance.gateway_runtime.bind != "lan"
            ):
                raise ValueError(f"A2A requires a managed gateway with bind=lan: {instance.name}")
            targets: set[str] = set()
            for peer in settings.a2a.peers.values():
                target = instance_map.get(peer.instance)
                if target is None or target is instance or target.name in targets:
                    raise ValueError(f"Unknown, duplicate or self A2A target: {peer.instance}")
                targets.add(target.name)
                if not set(instance.networks).intersection(target.networks):
                    raise ValueError(
                        f"A2A peers need a shared network: {instance.name}, {target.name}"
                    )
                if target.openclaw is None or not target.openclaw.a2a.enabled:
                    raise ValueError(f"A2A target is not enabled: {target.name}")
                reverse = next((p for p in target.openclaw.a2a.peers.values()
                                if p.instance == instance.name), None)
                if reverse is None or (
                    reverse.inbound_token_env != peer.outbound_token_env
                    or reverse.outbound_token_env != peer.inbound_token_env
                ):
                    raise ValueError(
                        f"A2A peers require reciprocal token references: {target.name}"
                    )
        return self


def _expand_string_values(value: object, env: Mapping[str, str]) -> object:
    def _expand_with_env(text: str) -> str:
        pattern = re.compile(r"\$(\w+)|\$\{([^}]+)\}")

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1) or match.group(2)
            return env.get(key, match.group(0))

        return pattern.sub(_replace, text)

    if isinstance(value, str):
        return _expand_with_env(value)
    if isinstance(value, list):
        return [_expand_string_values(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _expand_string_values(item, env) for key, item in value.items()}
    return value


def load_inventory(path: str | Path) -> Inventory:
    inventory_path = Path(path)
    data = yaml.safe_load(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Inventory file must contain a YAML mapping")

    resolved_path = inventory_path.resolve()
    # Staff inventories live below the project root (for example
    # ``personal-team/team.yml`` or ``examples/staff/team.yml``).
    project_root = (
        resolved_path.parents[1] if len(resolved_path.parents) >= 2 else resolved_path.parent
    )
    configured_project_root = _project_root_from_environment(str(project_root))
    expansion_env = {
        **os.environ,
        "workspaceFolder": configured_project_root,
        "CLAWAKE_PROJECT_ROOT": configured_project_root,
        # Backward-compatible expansion for existing inventory files. New files
        # should use CLAWAKE_PROJECT_ROOT because it names the value accurately.
        "CLAWAKE_WORKSPACE_ROOT": configured_project_root,
    }
    expanded_data = _expand_string_values(data, expansion_env)
    return Inventory.model_validate(expanded_data)
