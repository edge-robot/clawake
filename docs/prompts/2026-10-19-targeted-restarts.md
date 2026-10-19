---
target-repo: edge-robot/clawake
branch: feat/5-targeted-restarts
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#5
architecture-ref: clawake/docs/ADR.md#adr-003-member-readiness-model-and-one-step-rollout
base-commit: adff06a (feat/23-member-readiness, P1 accepted)
status: issued
---
# P2: Targeted restarts, readiness-gated plugin sync, `upgrade` start window (ADR-003, part 2 of 3)

## Goal

Make the v1 lifecycle commands safe for single-member rollouts:

- `setup` touches only members that need it.
- `sync-plugins` waits for readiness, skips identical packages and stops at the first
  failure.
- `upgrade` uses the member start window.
- Fix the channel lifecycle mapping from P1.

## Context

- **Decision:** `docs/ADR.md`, ADR-003 (accepted). This prompt implements decision items
  3, 4 and 6 and corrects item 1 for channels.
- **Issue:** edge-robot/clawake#5. Comment C1 describes the 2026-10-16 failure. #24 is the
  umbrella.
- **P1 (accepted):**
  - `docs/prompts/2026-10-19-member-readiness{,.report}.md`;
  - `src/clawake/services/readiness.py`, which provides `probe_member`,
    `wait_members_ready`, `ReadinessProbes` and `MemberReadiness`;
  - evidence in `docs/evidence/2026-10-19-member-readiness-status.md`.
- **Live facts** (OpenClaw 2026.9.9, recorded in that evidence):
  - **Start time.** After a restart, `/readyz` became ready after **46 s**. Directly
    after the restart, `status` showed `starting` / `readyz_unreachable`.
  - **`channels status --json`.** On a healthy member, every account in
    `channelAccounts.<channel>[]` has `lifecycle: "ready"`, `running: true` and
    `connected: true`. P1 accepts only `lifecycle == "running"`, which is wrong.
  - **`plugins list --json`.** Each plugin has `id`, `version`, `enabled` and `status`,
    but no sha256.
- **Current code to change:**
  - **`cli.setup_quadlets()` (v1).** After `daemon-reload`, it restarts every selected
    member: the loop at "Setup is an idempotent reconciliation operation". It was the fix
    for #5 problem 1 (unchanged units never started) and causes C1.
  - **`cli.sync_plugins()`.** It installs every declared plugin, continues after a
    failure, and restarts every member that had any install. It has no readiness wait.
  - **`services/plugins.py`.**
    - `sync_plugin()` verifies the mounted archive digest
      (`_verify_mounted_artifact`) and then runs `openclaw plugins install --force …`.
    - For archives, nothing records which digest was installed.
  - **`cli.upgrade_member()`.** It waits after the restart with
    `runtime_upgrade.wait_for_health()`: 3 × 30 s, roughly 60 s.
  - **`services/systemd.py` `SystemdService`.** It has `restart`, `stop` and `status`,
    but no `start`.

## Binding constraints and prohibitions

1. **`setup` restarts only what changed (v1).**
   - A member is **changed** if any of its rendered artifacts changes, or if its managed
     runtime config (`openclaw.json` / ledger via `plan_runtime_files` /
     `ensure_plugin_runtime_config`) would change. Prove the second case with a test:
     runtime config change only, no Quadlet change.
   - Per selected member the action is:
     - changed → `restart`;
     - unchanged and the unit is not active → `start` (add `SystemdService.start`). This
       keeps the #5 problem 1 fix.
     - unchanged and active → `none`.
   - The preview prints this action per member, plus the reason (artifact names or
     `runtime_config`). Reading the unit state in the preview is allowed; it is
     read-only.
   - With `--execute`, process members **sequentially**: act, then wait for
     `ready`/`degraded` with `wait_members_ready` and the member's start window, then go
     to the next member.
   - On the first failure (command error or `failed` readiness), stop. Do not touch the
     remaining members. Print a summary and exit non-zero. `none` members are not
     waited on.
2. **`sync-plugins` is readiness-gated and idempotent (v1).**
   - **Installed ledger.** After a verified install, record the installed archive
     `sha256` per member and plugin id in Clawake's existing managed plugin runtime ledger
     (the `clawake-managed-plugin-runtime.json` family; no new state location). Record it
     only after success.
   - **Skip rule.** Skip install and restart for a plugin if all of these hold:
     - the ledger digest equals the pin;
     - the readiness probe shows the plugin `enabled`, `loaded`, and at the version inside
       the pinned archive.

     For npm pins, the existing `plugins inspect --runtime --json` integrity check is
     the equivalent.
   - **Preview.** It prints, per member and plugin, `skip`, `install` or `install
     (unverified)` with the reason (e.g. `ledger_missing`, `digest_changed`,
     `version_mismatch`, `member_not_ready`). It also prints the members that would
     restart.
   - **Execute**, per member, sequentially:
     1. wait until `ready`/`degraded`; if the window runs out, fail;
     2. install the plugins that are not skipped;
     3. update the ledger;
     4. apply the runtime config / grants;
     5. restart **only if** something was installed or the config changed;
     6. wait for readiness again;
     7. verify that the readiness plugin check shows the pinned version.

     Stop at the first failing member and leave the later members untouched.
   - **Summary.** End with a table per member: action, plugin id, version, digest prefix,
     readiness, seconds to ready.
3. **`upgrade` uses the start window.** Replace `wait_for_health` in `upgrade_member` with
   `wait_members_ready([member], timeout=<start window>)`. `degraded` is accepted with a
   warning; `failed` triggers the existing rollback path. Keep `wait_for_health` only if
   something else still uses it; otherwise remove it together with its tests.
4. **Channel lifecycle fix.** An account counts as up if `running is true`, `connected` is
   not `false`, and `lifecycle` is in `{"ready", "running"}`. Unknown lifecycle values
   stay `degraded` with `channel_lifecycle`. Update the P1 tests and add a fixture with
   the live shape (synthetic IDs).
5. **Readiness probes stay read-only and secret-free** (P1 rules). Never print raw command
   output. Inject every subprocess, HTTP and clock call. Waits are bounded.
6. **v2 is unchanged**, and so are `restart`, `stop` and `teardown`. #5 problem 3 (first-run
   gateway bootstrap) and the missing `ensure_browser_cache` in v2 are out of scope.
7. Public repository: synthetic data only. No new dependencies. Never run `--execute`,
   `podman`, `systemctl` or `openclaw` yourself.

## Steps

1. Verify the preconditions: the branch is `feat/5-targeted-restarts` and HEAD is
   `adff06a`. Run the checks and record the base test count.
2. Write failing tests first for each constraint. Use fakes for systemd (active,
   inactive, failed; restart, start and their failures), readiness (sequences over time),
   `podman exec` (install success and failure) and the clock. At minimum, cover:
   - **setup:** a two-member inventory where only member A changed → only A restarts;
     B is untouched and not waited on; B is inactive and unchanged → B is started;
     a runtime-config-only change → restart; A's readiness ends `failed` → B untouched,
     exit 1, summary printed.
   - **sync-plugins:** ledger equals pin and the probe matches → no install, no restart;
     a new digest → install, restart, wait, verify; the member is not ready before the
     install → wait, then install; the first member fails → the second member is
     untouched (no exec, no restart); the preview shows the actions and mutates nothing
     (no ledger write).
   - **upgrade:** waits up to the start window (fake clock passes 60 s and still
     succeeds at 90 s); `failed` → rollback path.
   - **channels:** the live shape with `lifecycle: "ready"` → up; `connected: false` →
     degraded; an unknown lifecycle → degraded.
3. Implement. Keep `cli.py` thin: put the per-member orchestration in a service module
   (e.g. `services/member_ops.py`) that P3 (`rollout`) can reuse.
4. Update `docs/manual.md` / `docs/inventory-v2.md`. Propose `architecture.md` text in
   the report.
5. Run the checks, commit and write the report.

## Acceptance criteria and tests

- [ ] Every scenario in step 2 has a named test that failed first. Run mutation checks on
      the restart selection, skip rule, stop-on-first-failure and channel mapping.
- [ ] The `setup` preview shows the per-member action and reason; with `--execute` it
      restarts only `restart` members and starts only `start` members.
- [ ] `sync-plugins` never installs on a member that is not ready, skips identical pins,
      stops at the first failure, writes the ledger only after verified success, and
      prints the summary table.
- [ ] `upgrade` uses the start window.
- [ ] Golden rendering is unchanged (`tests/test_rollout_foundation.py`).
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are all green. No deployment data in the
      diff.

## Branch and worktree rules

- Commit only to `feat/5-targeted-restarts` in this checkout. No other branches, no
  worktrees, no push, no rebase. Commit this prompt unchanged together with your report.
- Leave the worktree clean.

## Expected report / handoff format

`docs/prompts/2026-10-19-targeted-restarts.report.md` in the agent-definition format.
Include:

- example preview output for `setup` and `sync-plugins` from a synthetic two-member
  fixture: one changed member, one unchanged member, one plugin pin changed;
- the exact command sequence the architect should expect live for a single-member plugin
  rollout;
- the ledger format change, before and after;
- the migration behaviour for members without a ledger digest. The first run after
  deployment is expected to install once (`ledger_missing`); state it explicitly.

Set `e2e-request: required`. The architect will repeat the 2026-10-16 single-member plugin
rollout on the live cluster with the operator's consent.
