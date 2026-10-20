# Omarchy harness bootstrap

The repo-scoped Codex skill is
[clawake-harness](../.codex/skills/clawake-harness/SKILL.md). Invoke it for host
bootstrap and operations. It is neither Robotics Lead nor Mechanical Engineer.
Clawake owns deployment; OpenClaw owns models/A2A; domain capabilities stay in
ClawCAD. Read its [bootstrap reference](../.codex/skills/clawake-harness/references/omarchy-bootstrap.md).

Discover both checkouts and inspect instructions/Git/host resources first. A fresh
checkout may coexist with old runtime: do not adopt or replace it by name alone.
Use Arch repository packages (Python >=3.11, uv, podman, github-cli), inspect
transactions and avoid incidental system upgrades. Verify rootless Podman, the
Quadlet generator and user systemd before proceeding.

From the verified Clawake checkout:

```bash
make install-dev
export CLAWAKE_PROJECT_ROOT="$PWD"
bash .codex/skills/clawake-harness/scripts/doctor.sh --clawcad /verified/clawcad
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Use the already verified ClawCAD profile preparation rather than inventing agent
roles here. Pass its generated inventory path explicitly to every Clawake command:

```bash
uv run clawake validate -c /verified/clawcad/state/demo/team.yml
uv run clawake setup -c /verified/clawcad/state/demo/team.yml
# After reviewing ownership, tokens, mounts, image pins, ports and topology:
uv run clawake setup -c /verified/clawcad/state/demo/team.yml --execute
uv run clawake sync-plugins -c /verified/clawcad/state/demo/team.yml
uv run clawake sync-plugins -c /verified/clawcad/state/demo/team.yml --execute
uv run clawake status -c /verified/clawcad/state/demo/team.yml
```

Set up model access with the documented OpenClaw CLI inside each running container.
Model keys need not be placed in env files. Gateway/A2A secrets must already exist
in the profile's private env files. Do not add a third named agent or widen tools:

```bash
uv run clawake onboard -c /verified/clawcad/state/demo/team.yml -m clawcad-robotics-lead --execute
uv run clawake onboard -c /verified/clawcad/state/demo/team.yml -m clawcad-mechanical-engineer --execute
bash .codex/skills/clawake-harness/scripts/doctor.sh --inventory /verified/clawcad/state/demo/team.yml
podman exec -i clawcad-robotics-lead node --input-type=module < .codex/skills/clawake-harness/scripts/a2a-smoke.mjs
```

The smoke runs from Lead's container using its A2A token and network, waits for the
original Engineer task and requires its final HARNESS_OK response. It does not
prove Lead's model decided to delegate. Container health, a send receipt or a
READY FOR PREVIEW doctor result alone never establishes completed runtime acceptance.
Record tests, reviewed preview, running gateways, policies and final same-task
completion before declaring READY FOR CLAWCAD DEVELOPMENT. Stop there.

On Omarchy with Podman 6, an inactive system network-online.target can block
podman-user-wait-network-online.service. Inspect before changing anything.
`quadlet_default_dependencies: false` on selected instances and networks opts out
of Quadlet network startup ordering for that inventory only. It defaults to true
and leaves other deployments/global services unchanged. Applications must tolerate
connectivity changes. The [bootstrap reference](../.codex/skills/clawake-harness/references/omarchy-bootstrap.md)
explains the diagnosed case and generator test.

Recovery uses the same inventory and Clawake status/logs/restart/teardown previews;
never replace it with manually maintained Quadlets or a second supervisor.

For Discord attachments, the product-owned inventory must explicitly include
`message` in `agent_tool_allow` for the named Lead agent:

```yaml
agent_tool_allow:
  clawcad-robotics-lead:
    - clawcad_delegate_render
    - message
```

Clawake reconciles these grants into that agent's `tools.alsoAllow` when it has
no explicit `tools.allow`. This matters after onboarding selects the `coding`
profile: a global `tools.allow` containing `message` alone does not widen the
profile. A Discord skill supplies instructions, not tool permissions. Keep the
grant in the inventory generator as well, so regeneration preserves it. Global
and provider allow/deny policies still apply; inspect the effective tool catalog
for the named agent, including Control UI WebChat sessions.

Review `sync-plugins -c <inventory> -m clawcad-robotics-lead`, then run it with
`--execute` to reconcile the policy and restart the Lead gateway. Do not grant
Discord delivery to the Engineer. The native message tool accepts `action: send`,
`channel: discord`, `target: channel:<id>`, `message`, and `media`. Pass the
renderer's `filePath` value as `media`; the installed agent tool schema does not
expose a `filePath` parameter. Verify a real attachment
upload separately; a local path or renderer checksum does not prove delivery.

If the host already has a restrictive global `tools.allow`, an operator may add
explicit names through the inventory's `global_tool_allow`. Clawake requires an
existing nonempty allowlist, preserves its unmanaged entries and all deny/profile
settings, and removes only the names it added when the grant is withdrawn. Groups,
wildcards and duplicates are rejected; a withdrawal that would empty the global
allowlist fails rather than widening the default policy. Agent grants still
determine which named role can use the tool. Preview reconciliation before apply.

## Separate persisted state

An instance can set an absolute `state_path`. Clawake uses it consistently for
OpenClaw mounts, configuration, plugin ledgers, cache, backups and upgrade Doctor.
Existing inventories still default to `workspace_path/.openclaw`. Before moving
paths, stop only selected managed services, take and verify a complete private
backup, and copy all native state offline. Preview setup against the copied state
before applying. `clawake stop -c INVENTORY [--member NAME]` previews service stops;
`--execute` applies without removing definitions/data. MCP uses its existing
`onshape-mcp --action stop` lifecycle. Preserve agent identities during migration.
