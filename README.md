# Clawake

**A reproducible home for your OpenClaw team.** Clawake turns a declarative team
inventory into rootless Podman containers managed by Quadlet and systemd user
services. It brings deployment, diagnostics and upgrades into one local CLI.

Use it as the operational foundation for teams working on software, robotics,
project coordination, customer and supplier relationships, or personal assistance.
Define each agent's responsibilities in role documents and provide the workspaces
and tools needed for your projects.

## Why it exists

Running an agent is only the beginning. A useful team needs persistent workspaces,
clear access boundaries, predictable startup and a way to recover when an upgrade
fails. Maintaining those details by hand makes each deployment harder to explain
and reproduce.

Clawake makes those operational decisions explicit. You describe the members,
images, mounts and ports in YAML, review the proposed changes, then apply them.
Each member gets its own workspace and runtime state, while team definitions are
mounted read-only. This gives experiments a repeatable foundation and makes
long-lived teams easier to maintain.

## Purpose and vision

Clawake owns the **deployment lifecycle** around OpenClaw: inventory validation,
Quadlet generation, service operations, dashboard diagnostics and controlled
upgrades. OpenClaw owns agent behavior, conversations and runtime data. Podman and
systemd provide container execution and service supervision. Team-specific roles,
communication and collaboration workflows are configured in OpenClaw and the
agents' workspaces; deploying a team does not automatically orchestrate its work.

Our vision is that operating an agent team becomes as understandable as
maintaining its team definition: inspect the desired state, preview a change,
apply it deliberately and see what is running. The current implementation targets
a local Linux host with rootless Podman and systemd user services. Remote fleet
management, a web interface and automatic recovery are future directions, not
features of the current CLI. Container isolation depends on the configured images,
mounts and host permissions; Clawake does not provide a separate security boundary.

## Get started

You need Python 3.11+, `uv`, and—for runtime operations—a Linux host with rootless
Podman, Quadlet support and a working systemd user session.

```bash
make install-dev
export CLAWAKE_PROJECT_ROOT="$PWD"
uv run clawake --help
uv run clawake validate -c examples/staff/team.yml
uv run clawake setup -c examples/staff/team.yml
```

Adapt the example inventory's images, host paths, ports and environment files to
your team before applying it. Validation checks the inventory, rendering and
installed artifact differences; it does not verify image availability or host
runtime readiness.

For a complete three-member setup, see the [robotics team](robotics-team/README.md),
whose three leads combine product and technical leadership, robotics engineering,
and project, customer and supplier coordination. They communicate through OpenClaw
A2A on a shared Podman network and delegate bounded work to native sub-agents.

```bash
uv run clawake setup -c examples/staff/team.yml --execute
uv run clawake status -c examples/staff/team.yml
uv run clawake logs -c examples/staff/team.yml -m excalibot-product-owner -n 50
```

Lifecycle changes default to a preview. `setup` renders and compares in memory;
without `--execute` it writes no files and starts no services. Applying setup
prepares managed runtime configuration, writes changed Quadlets, reloads systemd
and restarts every selected member, including members with unchanged artifacts.
This can interrupt running work.

For a user-level command, run `make install-tool`. Existing Make shortcuts and
long command names such as `setup-quadlets` remain supported.

## CLI

Every command takes `--config/-c <inventory.yml>`. Commands that operate on a team
accept `--member/-m <name>` to limit their scope.

| Command | Purpose |
| --- | --- |
| `validate` | Check inventory and artifact rendering without changes |
| `setup` | Preview or apply the team's deployment |
| `status --format text\|json` | Inspect systemd service state and failure diagnostics |
| `logs -m NAME [-n 100]` | Read a member's journal entries |
| `restart` | Preview or restart selected services |
| `onboard -m NAME` | Preview or run interactive OpenClaw onboarding |
| `dashboard [--format json]` | Show local dashboard URLs and token presence |
| `upgrade -m NAME --to TAG --digest sha256:…` | Preview or perform a pinned image upgrade |
| `sync-plugins -m NAME` | Verify and install digest-pinned plugins declared in inventory |
| `teardown` | Preview or remove selected containers and Quadlets |

Add `--execute` to apply `setup`, `restart`, `onboard`, `upgrade` or `teardown`.
Dashboard tokens remain hidden unless `--show-token-url` is supplied. Teardown
preserves workspace data. Status reports systemd state; successful status alone is
not an application health check. Upgrade additionally checks HTTP health and the
running image/version, and creates backups when enabled by inventory policy.

Exit codes: `0` for success (including previews), `1` for operational failure,
`2` for invalid CLI arguments or inventory. JSON status is emitted on stdout and
remains available when an unhealthy service causes exit code `1`.

Compatibility names: `setup-quadlets`, `restart-quadlets`, `status-quadlets`,
`teardown-quadlets`, `onboard-member`, `diagnose-dashboard`.

## Architecture

```text
CLI → validated inventory → deployment plan → explicit application
                              ↓                      ↓
                         Quadlet renderer      filesystem / systemd / Podman
```

`config.py` defines and validates the domain. `services/deployment.py` compares
desired and installed artifacts without writing, and exposes a separate apply
operation. Runtime adapters and upgrade helpers live in `services/`. `cli.py`
provides commands, output and lifecycle orchestration. Setup and upgrade share the
same artifact planning/application path. Further extraction of lifecycle
orchestration can build on this boundary without introducing a second runtime.

## Operations and security

Start with the [operator manual](docs/manual.md) for deployment, diagnostics,
upgrades and channel configuration. The
[user journey](docs/user_journey.md) distinguishes today's commands from the CLI
vision: explain compatibility and access changes before applying a member-scoped plan.

Our [security assessment](docs/security.md) evaluates the operational boundaries of
multi-agent deployments. The outer container limits the gateway and its plugins;
Quadlet makes its configuration reproducible. Clawake must earn its maintenance cost
through reliable checks and recovery. The assessment also covers when native OpenClaw
or manually maintained Quadlets are sufficient, and records current hardening gaps.

See also [architecture and tradeoffs](docs/architecture.md), the
[upgrade runbook](docs/upgrade-playbook.md), [roadmap](docs/repo-roadmap.md), and
[example teams](examples/staff/README.md).

## Development

```bash
uv run pytest
uv run ruff check .
```

Tests exercise rendering, validation and lifecycle operations with temporary files
and simulated runtime adapters. They do not require deploying a live team.
