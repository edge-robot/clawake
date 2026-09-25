# Inventory v2: model and validation boundary

This first increment adds **offline validation only**. Version 1 remains deployable
with its existing behavior. Version 2 may contain instances, services, or both;
`clawake validate -c team.yml` validates the entire inventory without contacting
Podman, reading secret values, rendering files or creating directories.

V2 rendering, deployment and lifecycle are deliberately blocked until the next
increments implement them. A valid model does not mean the service can be deployed.

## Implementation increments

1. Models and validation (this increment): v2 resources, references, namespaces,
   storage, ports, secrets, health declarations and an acyclic dependency graph.
2. Deterministic rendering and combined deployment planning.
3. Service selectors, dependency-ordered lifecycle and generic upgrades.
4. Readiness/status/log reporting and a runnable example.

Managed TLS provisioning and MCP service bindings are separate follow-on work.
Unsupported `tls` and `service_bindings` fields are rejected rather than accepted
as silently ineffective settings.

## Service shape

```yaml
version: 2
cluster: {name: example, primary_host: local}
hosts: [{name: local}]
networks: [{name: team}]
services:
  - name: example-api
    host: local
    image: {repository: example.invalid/api, tag: "1.0.0"}
    networks: [team]
    command: [serve, --port, "8080"]
    ports:
      - {host_port: 8080, container_port: 8080}
    env_files: [/srv/example/config/api.env]
    mounts:
      - {source: /srv/example/config, target: /etc/example, read_only: true}
    volumes:
      - {name: data, target: /var/lib/example}
    secrets:
      - {name: example-api-token, target: api-token}
    health: {scheme: http, port: 8080, path: /health/ready}
    restart_policy: on-failure
    backup_policy: {enabled: true, pre_mutation: true, paths: [/srv/example/config]}
    interfaces:
      api: {url: "http://example-api:8080/api"}
    dashboard: {friendly_name: Example API, tags: [example]}
```

The example is validation-only, uses a non-resolvable image host, and needs no
files, secrets or running containers to validate. Image tags must be explicit;
floating tags require a digest. Optional digests must be complete SHA-256 values.
`command` is an argument list, not a shell command string. Secret values belong
neither in the inventory nor in environment files.

## Names, networks and dependencies

- V2 resource names are lowercase DNS labels, at most 63 characters, with no
  leading/trailing hyphen. Instance/service/network names are unique across those
  declarations. Existing v1 naming behavior is retained.
- An omitted network host defaults to `cluster.primary_host` in v2; v1 still
  requires an explicit host. Both versions remain single-host.
- Service container name and `.container` filename derive from `name`.
- Volumes belong to a service: `name: data` in `example-api` derives
  `example-api-data.volume` and the Podman volume name `example-api-data`.
  The same local volume name in another service is independent.
- Generated Quadlet paths, systemd unit names and Podman resource names are
  checked for collisions, including existing instance artifacts and legacy units.
- `depends_on: [other-resource]` is allowed on v2 instances and services. References
  must identify a service or instance on the same host. Duplicates, self references,
  unknown targets and cycles fail. Dependencies describe start ordering; they are
  not proof of readiness. `dependency_order()` returns a deterministic topological
  order for later lifecycle code.

## Storage, secrets and validation limits

- Host paths are expanded and normalized lexically without creating or resolving
  filesystem objects. V2 bind sources support `..` after expansion; targets must
  be absolute container paths without dot segments. Root mounts, control characters,
  whitespace and Quadlet delimiter/specifier characters are rejected in this first
  model. Nested targets are allowed; identical targets are not.
- Environment files and backup paths are validated as paths without reading them.
- Secrets reference existing Podman secret names, with an optional target basename
  below `/run/secrets`. Duplicate names/targets and shadowing mounts fail. There is
  no second top-level secret catalog and no secret value field.
- Parsing checks secret syntax, not the host's secret store. The pure
  `validate_secret_references(available_names)` preflight accepts a host-supplied
  set of names and rejects missing references without inspecting values. Runtime
  discovery belongs to the lifecycle increment.
- Port collision checks cover instances and services together, wildcard and mapped
  IPv6 addresses, and distinguish TCP from UDP. Unpublished internal health ports
  are valid; reachability is a future runtime check.
- Interfaces allow HTTP(S) URLs without credentials, query strings or fragments.
  They declare endpoints, not automatic provider configuration.
- Offline validation does not verify image availability, writable storage, host
  permissions, certificate trust, HTTP readiness or actual secret existence.

The validation module remains provider-neutral and imports no runtime clients.
Tests use local fixtures and mocked side effects; the existing Quadlet-generator
test is a dry run and starts no containers.
