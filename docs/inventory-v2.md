# Inventory v2: model and validation boundary

Version 1 retains its established runtime behavior. Version 2 supports instances,
services or both, deterministic combined plans, dependency-ordered setup/restart,
persistence-safe teardown and resource status/logs. `validate` stays entirely
offline: it does not render files, contact Podman or inspect secret values.

Setup/restart previews have no side effects. `--execute` checks referenced secret
names and declared service inputs, applies a drift-checked plan and reloads once.
HTTP readiness comes from the container health check (the image must provide curl).
TLS verification remains enabled; privately issued certificates need an explicit
CA trust mount/environment configuration. A failing dependency blocks its consumers.

Generic `upgrade-service --service NAME --to TAG --digest SHA256` previews a pinned
upgrade and private backups. Writable bind paths must be in the backup policy;
named volumes are exported. `--with-dependents` explicitly includes dependent
resources in the maintenance plan. Execution stops them in reverse dependency
order, backs up data, updates only the selected image mapping and reconciles the
ordered selection. Backup failure or inventory drift halts the upgrade. No
OpenClaw doctor or application-specific provider behavior is invoked. Backups are
retained; deletion/retention is a separate operation.

Full generic managed TLS/CA distribution, service bindings and dashboard integration
remain separate work. Unsupported `tls` and `service_bindings` fields still fail
rather than becoming silently ineffective settings.

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
  below `/run/secrets`; optional numeric uid/gid control ownership and the renderer
  uses mode 0400. Duplicate names/targets and shadowing mounts fail. There is
  no second top-level secret catalog and no secret value field.
- Parsing checks secret syntax, not the host's secret store. The pure
  `validate_secret_references(available_names)` preflight accepts a host-supplied
  set of names and rejects missing references without inspecting values. Runtime
  discovery uses `podman secret ls` names only during executed setup/restart.
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

## Focused Onshape MCP exception (2026-10-12)

Independent of the generic v2 services above, `clawake onshape-mcp --config service.yml
--action plan|apply|status|restart [--execute]` operates a narrow private rootless
external-token service using the existing artifact/systemd lifecycle. It requires
an image digest, private credential_directory, config_file and quadlet_root;
it never reads or renews tokens. `InstanceSpec.onshape_mcp: true` reconciles the
known local native server in disabled state; activation/agent invocation still
requires its separate supported authority boundary. See Claw-Onshape's
self-hosted-mcp-architecture.md for the exact mode and credential contract.

## Operation examples

```bash
clawake setup -c team.yml --service example-api
clawake setup -c team.yml --service example-api --execute
clawake status -c team.yml --service example-api --format json
clawake logs -c team.yml --service example-api -n 100
clawake restart -c team.yml --service example-api --execute
clawake teardown -c team.yml --service example-api --execute
```

`--member` and `--service` are mutually exclusive. Omitting both selects the whole
team for lifecycle/status; logs require exactly one resource. Setup/restart includes
transitive dependencies; teardown rejects dependencies used by unselected resources.
Podman volumes and bind data survive teardown. Shared network definitions survive
while unselected resources still use them. Services may set
quadlet_default_dependencies: false on hosts that cannot supply user network-online;
explicit resource ordering remains active.

Status reads only selected safe Podman fields: unit state, container state,
configured image/digest, running image ID/digest, restart count, health/readiness and
failed dependencies. It never prints full inspect output, environment or health logs.
Integrity-pinned plugin sync works for v2 members after their reviewed setup.
Onboard/dashboard and the original member upgrade remain v1-only;
service upgrades use the explicit generic command above.


A service may set numeric `user: "UID:GID"` to match private host-bind ownership
under keep-id. The image's USER is otherwise retained; keep-id alone does not
replace an explicit image USER. `read_only: true` makes the image filesystem
read-only while declared writable data mounts remain writable. Internal curl
health probes use --noproxy to reach their own service DNS directly.

## MCP server models and the conflict rule

Two inventory models provision OpenClaw's native MCP client configuration
(`mcp.servers.<name>`); OpenClaw remains the transport and Clawake never handles
OAuth tokens.

- **Instance flags** `onshape_mcp` / `onshape_mcp_enabled` on an instance manage the
  fixed local server `onshape` (`http://clawonshape-mcp:8080/mcp`) of the focused
  Onshape MCP service above. `onshape_mcp_enabled` requires `onshape_mcp`. A disabled
  hosted Onshape connector that is not yet managed is adopted once.
- **`openclaw.mcp_servers`** declares generic remote HTTPS OAuth servers by name; see
  [mcp-provisioning.md](mcp-provisioning.md).

Both models may be used on the same instance, but they must not name the same server.
`onshape_mcp: true` together with `openclaw.mcp_servers.onshape` fails validation; neither
model silently overrides the other. Every server either model writes is owned through
the runtime ledger: a managed entry that was changed outside Clawake stops planning with
`Managed field changed outside Clawake`, and an unchanged re-run plans no change.
