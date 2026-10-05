---
name: clawake-harness
description: Bootstrap, deploy, diagnose and verify the ClawCAD OpenClaw harness through Clawake on Omarchy. Use for platform operations, not engineering or CAD work.
---

# Clawake Harness Agent

Operate Clawake as the reproducible deployment and lifecycle harness for ClawCAD
OpenClaw agents on Omarchy (Arch Linux). This is a Codex operations skill, not a
third permanent OpenClaw agent.

Read [architecture-boundary.md](references/architecture-boundary.md) before
operating and [omarchy-bootstrap.md](references/omarchy-bootstrap.md) for fresh
checkouts or missing prerequisites.

Discover both repositories; verify edge-robot remotes, local instructions, clean
Git state and remote branches/PRs. Work on a feature branch. The historical
Clawake demo baseline is `feat/clawcad-demo-profile`, descended from
`feat/clawcad-services-v2` and `feat/robotics-team`; verify current evidence before
choosing a base. Keep private ClawCAD roles/plugins in ClawCAD.

Run `bash .codex/skills/clawake-harness/scripts/doctor.sh --clawcad <path>
--inventory <path>` from Clawake. A fresh checkout does not imply a clean host.
Classify each relevant container, image, network, volume, unit, Quadlet, port and
workspace as CURRENT_WORKSPACE, KNOWN_OLD_CLAWCAD, UNRELATED or UNKNOWN using
paths and ownership evidence, never names alone. Report old resources before
replacement. Never mutate UNKNOWN or UNRELATED; old ClawCAD also needs an explicit
replacement/adoption decision. Diagnosis does not authorize teardown.

Use repository-supported `uv` setup and checks. Reuse ClawCAD's verified
`tools/prepare_demo.py` and dedicated two-agent inventory when present; inspect
its branch and overwrite behavior first. Preserve existing runtime files. The
historical read-only demo is on `feat/onshape-render-demo`; newer exploration
branches add engineering capabilities and must not be enabled for a harness smoke.

The flow is inventory → `clawake validate -c ...` → `clawake setup -c ...`
→ review → `clawake setup -c ... --execute`. Preview must show exactly
`clawcad-robotics-lead` and `clawcad-mechanical-engineer`, pinned images, correct
mounts/read-only boundaries, shared network, loopback ports, reciprocal A2A
references and no secret values or unrelated modifications. Historical ports
are 19389/19489; investigate conflicts instead of changing ports at random.

Execute only after rootless Podman, Quadlet, user systemd, tests, inventory,
preview, resource ownership, ports and required gateway/A2A secrets are verified.
Check required env files privately for presence and mode 0600; never print values.
Do not infer readiness from optional Onshape/Discord credentials. A working model
provider is required for an agent completion smoke, even when host readiness passes.

Use Clawake for setup/status/logs/restart/plugin synchronization/recovery/teardown.
Read-only Podman/systemd/journal commands are diagnostic escape hatches. No Docker,
Compose, Kubernetes, manual production Quadlets, long-lived podman run or second
supervisor. Teardown previews first and preserves workspace data; verify scope.

After setup check Clawake status, Podman containers/network and service journals.
Configure model access with `uv run clawake onboard -c <inventory> -m <member>
--execute`, the documented interactive OpenClaw CLI inside the container. Model
credentials need not be placed in env files. Recheck named-agent routing and
capability policies after onboarding.
Inspect actual OpenClaw tools/plugin policies (without exposing credentials), not
only ROLE.md: Lead has no Engineer CAD tools or engineer control socket; Engineer
has no manufacturing authority. Inventory grants may extend an existing allowlist,
so inspect both global and per-agent policies and effective tool availability.

Smoke through Lead → authenticated A2A → Engineer with a unique task ID:
“Harness smoke test. Return task_id and the literal string HARNESS_OK. Perform no
CAD, web, filesystem mutation or manufacturing action.” Inspect the pinned
OpenClaw A2A contract/client for its actual submission and polling API. Require
the final completed response for the SAME task ID; receipt, timeout or a new
reverse-direction task is not completion. Do not call the CAD delegation tool.

When GitHub CLI is authenticated and issue creation is authorized, record baseline,
old runtime, packages, doctor, topology, tests, preview, deployment, smoke and
blockers in edge-robot/clawcad; cross-reference Clawake commits/PRs. Never publish
private domain material in public Clawake. Stop after verified harness readiness;
do not start ClawCAD product work. Report partial progress honestly when credentials
or host permissions block completion.

For the verified 2026.9.4 demo, after model onboarding, run
`podman exec -i clawcad-robotics-lead node --input-type=module <
.codex/skills/clawake-harness/scripts/a2a-smoke.mjs`. This diagnostic submits from
Lead's network/token context and verifies Engineer's final same-task response.
It does not prove that Lead's model chose to delegate; verify that separately if
required. The script uses the existing authenticated SendMessage/GetTask contract,
prints only task identifiers and HARNESS_OK, and fails on receipt-only outcomes.
