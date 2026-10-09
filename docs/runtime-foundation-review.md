# Runtime foundation review

Date: 2026-10-09. Integration branch: `feat/reusable-runtime-foundation`.
Base: `feat/robotics-team` at `cefbfe5`; main remains its ancestor `860380a`.
This work provisions and operates OpenClaw and generic services. It contains no
product-specific CAD operation, agent reasoning implementation or MCP write tool.

## Selection and dependencies

| Source | Decision | Result / reason |
| --- | --- | --- |
| `ef00072`, `96c468a` | KEEP, already baseline | Managed plugin configuration, per-agent grants and pinned npm verification are generic capability provisioning. |
| `3151eaa`, `44945a9`, `cc44633`, `313d12e` | KEEP, already baseline | Managed workspaces, lifecycle and pinned upgrades preserve OpenClaw ownership of runtime state. |
| `6f830b3`, `624ec9a`, `957f338`, `8bcb959`, `8a49e1b`, `d3f242d` | KEEP, already baseline | Agent/A2A setup, v2 validation, formatting/tests and harness readiness remain independent of any application architecture. |
| `0b63e9b` | KEEP → `843e3fd` | Generic services, shared networks and reviewed deployment plans. |
| `b7dd40f` | KEEP → `c8dbf38` | Dependency-ordered lifecycle/readiness, on top of generic rendering. |
| `53c76c6` | DROP as cherry-pick | Synchronization merge; target already includes its robotics-team parent. |
| `afe2c78` | KEEP → `aac0304` | Safe status fields and pinned upgrades with consistent backup/data preservation; depends on lifecycle. |
| `909b9c2` | KEEP → `95c7601` | Explicit mount UID/GID, read-only image root, direct internal health probes and v2 pinned plugin sync; depends on earlier v2 modules. |
| Grant reconciliation | ADAPT | Record ownership only for tools actually inserted by Clawake. Pre-existing same-name unmanaged grants survive inventory removal under both allow and alsoAllow. |
| sync-plugins/lifecycle | ADAPT | Zero-plugin inventories still reconcile retired managed grants. Failed plugin/config installs do not restart that member. A no-op zero-plugin reconciliation does not restart the runtime. |
| Agent setup/tool provisioning | KEEP | Generic setup plans preserve existing per-agent tools. OpenClaw owns agents and capabilities; inventory controls provisioning. Restart does not replace agent configuration. |

Cherry-picks used `-x` in the order above. The template conflict was resolved by
preserving existing network/default-dependency controls and adding declared
After/Requires service dependencies. No merge commit from the source branch was
cherry-picked. No live agent runtime was restarted, provisioned or otherwise changed.

## Ownership and limits

Clawake owns declarative deployment/lifecycle and additions tracked in its
ownership ledgers. OpenClaw owns runtime agent configuration/tool policies.
Unmanaged grants, explicit deny rules and other operator fields remain intact.
Tool grants do not approve application actions or authorize physical effects.

Existing legacy ledgers cannot distinguish a historically claimed grant from a
same-name tool that was already operator-owned. The correction preserves new
ownership evidence, not a retrospective migration. Config and ownership files
are each replaced atomically, but the two-file update is not a crash-atomic
transaction; live recovery needs review of both files. No stronger claim is made.

Validated with the existing local dependency environment (`uv run --no-sync`,
PYTHONPATH=src): 259 offline pytest tests and Ruff pass. Coverage includes v1/v2
rendering, shared networks, service setup/readiness/teardown, data-preserving
upgrade, same-name unmanaged grants, stale-grant removal without plugins,
no-op repeat sync, failed install without restart and preservation of tool grants
through a v2 setup plan. No containers, live credentials or external tools are
needed for these checks. Whitespace checks pass.
