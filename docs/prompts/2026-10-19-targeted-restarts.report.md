---
prompt: docs/prompts/2026-10-19-targeted-restarts.md
branch: feat/5-targeted-restarts
commits: 33b9475..<report-commit>
status: done
e2e-request: required
---
# Report: P2 targeted restarts, readiness-gated plugin sync, `upgrade` start window

## Summary

Implements ADR-003 items 3, 4 and 6 and corrects item 1 for channels, on base `adff06a`.

- **`setup` (v1).** Each selected member gets an action: `restart` (changed rendered
  artifact or managed runtime config), `start` (unchanged, unit not active) or `none`
  (unchanged, active). The preview prints the action and its reason. `--execute` handles
  one member at a time: act, then `wait_members_ready` with the member's start window.
  It stops at the first failure (command error or a `failed`/`stopped` readiness), prints
  a summary table and exits 1. `none` members are not waited on.
- **`sync-plugins`.** It is readiness-gated and idempotent. A plugin is skipped only if
  the ledger digest equals the pin **and** the probe shows it enabled, `loaded` and at the
  pinned version. npm pins use `plugins inspect --runtime --json` instead of the ledger.
  `--execute` handles one member at a time: wait, install, record the ledger, apply
  config/grants, restart only on change, wait, verify the pinned version. It stops at the
  first failing member and ends with a summary table.
- **`upgrade`.** `wait_members_ready([member])` with the start window replaces
  `wait_for_health`. `degraded` gives a warning; any other final state takes the existing
  failure path. `wait_for_health` had no other user and is removed.
- **Channels.** An account is up if `running is true`, `connected` is not `false` and
  `lifecycle` is in `{"ready", "running"}`.
- **Code layout.** The orchestration is in `src/clawake/services/member_ops.py`, so P3
  (`rollout`) can reuse it. `cli.py` only prints.

## Changed files

`git diff --stat adff06a..HEAD` (the report commit is not included):

```
 docs/inventory-v2.md                           |  16 +-
 docs/manual.md                                 |  84 ++-
 src/clawake/cli.py                             | 143 ++---
 src/clawake/config.py                          |   2 +-
 src/clawake/services/gateway_config.py         | 107 +++-
 src/clawake/services/member_ops.py             | 559 ++++++++++++++++++
 src/clawake/services/plugins.py                |  38 ++
 src/clawake/services/readiness.py              |  22 +-
 src/clawake/services/runtime_upgrade.py        |  22 -
 src/clawake/services/systemd.py                |   3 +
 tests/fixtures/channels-status-live-shape.json |  30 +
 tests/test_cli.py                              |  55 +-
 tests/test_coordination.py                     |  11 +-
 tests/test_member_ops.py                       | 766 +++++++++++++++++++++++++
 tests/test_readiness.py                        |  45 ++
 tests/test_reusable_plugin_sync.py             |  24 +-
 tests/test_systemd.py                          |   6 +
```

| File | Purpose |
|---|---|
| `docs/inventory-v2.md` | New channel "up" rule; start window now bounds the waits of setup, sync-plugins and upgrade |
| `docs/manual.md` | New section "Targeted `setup` and readiness-gated `sync-plugins`"; corrects the outdated "restarts even unchanged" sentence (section 2) |
| `src/clawake/cli.py` | Thin wiring: setup actions and summary, sync-plugins plan and summary, upgrade readiness wait; `Architecture-Ref` comment |
| `src/clawake/config.py` | `ReadinessSpec` docstring only |
| `src/clawake/services/gateway_config.py` | `plan_plugin_runtime_config` (pure), `installedPlugins` ledger read/record, preserves the ledger in `ensure_plugin_runtime_config` |
| `src/clawake/services/member_ops.py` | New: `plan_setup_actions`, `run_setup_actions`, `decide_plugin`, `plan_member_plugins`, `run_plugin_sync`, summaries |
| `src/clawake/services/plugins.py` | `npm_inspect_command`, `npm_install_mismatch` (pure, read-only) |
| `src/clawake/services/readiness.py` | Channel mapping fix; public `classify_unit` |
| `src/clawake/services/runtime_upgrade.py` | Removes `wait_for_health` and its now-unused imports |
| `src/clawake/services/systemd.py` | `SystemdService.start` |
| `tests/fixtures/channels-status-live-shape.json` | Live `channels status --json` shape, synthetic IDs |
| `tests/test_member_ops.py` | New: setup, sync-plugins, ledger and upgrade scenarios with a fake host (systemd, `/readyz`, `podman exec`, clock) |
| `tests/test_readiness.py` | Channel tests on the live shape |
| `tests/test_systemd.py` | `start` argv and dry run |
| `tests/test_cli.py`, `tests/test_coordination.py`, `tests/test_reusable_plugin_sync.py` | Adapted to the new semantics (see *Changed expectations*) |

## Checks

Base (`adff06a`, before any change): `uv run pytest -q` → **359 passed, 2 skipped**.

Final, on HEAD `1236a1d`:

| Command | Result |
|---|---|
| `uv sync --locked` | pass |
| `uv run ruff check .` | pass ("All checks passed!") |
| `uv run ruff format --check .` | pass (43 files already formatted) |
| `uv run pytest -q` | pass, **392 passed, 2 skipped** |
| `uv lock --check` | pass |

More verification, all from scratch trees (`git archive` + `PYTHONPATH`), so the checkout
was never modified:

- **Intermediate commits.** `33b9475` gives 366 passed, `0b4b5d4` gives 367 passed (both
  with 2 skipped).
- **Host guard.** A scratch pytest plugin made every `subprocess.run` of
  `systemctl`/`podman`/`journalctl`/`openclaw` and every `readiness.http_get` raise.
  `test_member_ops`, `test_cli`, `test_coordination`, `test_reusable_plugin_sync` and
  `test_readiness` gave 145 passed, 1 skipped. No test reaches the host.
- **Suppressions.** No `# noqa` or `# type: ignore` were added.

## Acceptance criteria

- **Named test for every step-2 scenario, failed first, with mutation checks → met.**
  - **Failed first.** All new scenario tests were run against base code.
    - `test_member_ops.py` imports the new ledger API. To get behavioural failures, it ran
      on base plus only the new `gateway_config.py` and `systemd.py`, without `upgrade`
      tests: 18 failed, 2 passed. The 2 passing tests are the ledger unit tests; their
      code was part of the overlay.
    - The 3 `upgrade` tests ran on base with a guard that refuses `urlopen` and disables
      `sleep`: 3 failed.
    - Channel tests on pure base: 7 failed. `test_systemd` start test on base: failed.
  - **Mapping of scenarios to tests:**
    - setup, only A changed → `test_setup_restarts_only_the_changed_member`
    - setup, B inactive and unchanged → `test_setup_starts_unchanged_inactive_member`
    - setup, runtime-config-only change → `test_setup_restarts_member_on_runtime_config_only_change`.
      The test asserts that the plan contains only `kind == "runtime"` changes.
    - setup, A ends `failed` → `test_setup_stops_after_first_member_fails_readiness`
      (plus `..._never_gets_ready` and `..._restart_command_fails`)
    - sync, ledger equals pin and probe matches → `test_sync_skips_identical_pin_without_install_or_restart`
    - sync, new digest → `test_sync_new_digest_installs_restarts_waits_and_verifies`
    - sync, member not ready before install → `test_sync_waits_for_readiness_before_installing`
      (plus `..._never_installs_on_member_that_does_not_get_ready` and `..._fails_fast_on_stopped_member`)
    - sync, first member fails → `test_sync_first_member_failure_leaves_second_untouched`
      (plus `..._post_restart_verification_failure_stops`)
    - sync preview mutates nothing → `test_sync_preview_shows_actions_and_writes_no_ledger`
      (plus `..._marks_not_ready_member_unverified`)
    - upgrade waits past 60 s and succeeds at 90 s → `test_upgrade_waits_for_the_start_window`
    - upgrade `failed` → rollback path → `test_upgrade_failed_readiness_takes_the_rollback_path`
    - channels, live shape `ready` → up → `test_channel_live_shape_with_lifecycle_ready_is_up`
    - channels, `connected: false` → `test_channel_live_shape_connected_false_is_degraded`
    - channels, unknown lifecycle → `test_channel_live_shape_unknown_lifecycle_is_degraded`
  - **Additional tests:**
    - `test_setup_unchanged_active_team_is_a_no_op`
    - `test_setup_preview_prints_actions_and_mutates_nothing`
    - `test_sync_member_without_ledger_installs_once_then_skips`
    - `test_sync_reinstalls_rebuilt_archive_with_same_version`
    - `test_sync_reinstalls_when_ledger_matches_but_runtime_differs`
    - `test_ledger_survives_grant_reconciliation_and_drops_undeclared`
    - `test_ledger_rejects_malformed_entries`
    - `test_upgrade_degraded_warns_and_completes`
    - `test_channel_running_without_lifecycle_is_degraded`
    - `test_channel_ready_lifecycle_but_not_running_is_degraded`
    - `test_start_is_a_dry_run_unless_executed`
  - **Mutation checks.** Each mutation ran on a scratch copy of HEAD `src`; the result is
    tests failing / tests in the selection. All mutants were killed:

    | Mutant | Result |
    |---|---|
    | restart-all (always `restart`) | 7 failed / 14 |
    | never-start (inactive → `none`) | 4 failed / 14 |
    | skip ignores ledger digest | 2 failed / 12 |
    | skip ignores probe | 2 failed / 12 |
    | setup continues after failure | 2 failed / 14 |
    | sync continues after failure (two variants) | 8 failed / 10 and 3 failed / 10 |
    | channel `{"running"}` only | 6 failed / 21 |
    | channel ignores `connected` | 1 failed / 21 |
    | channel accepts any lifecycle | 5 failed / 21 |
    | upgrade fixed 60 s window | 2 failed / 6 |

    The two skip-rule mutants were first caught by only one test each. I then added
    `test_sync_reinstalls_rebuilt_archive_with_same_version` and
    `test_sync_reinstalls_when_ledger_matches_but_runtime_differs`.
- **`setup` preview shows per-member action and reason; `--execute` restarts only `restart`
  members and starts only `start` members → met.** Evidence: the setup tests above and
  the `host.calls` assertions.
- **`sync-plugins` → met.** It never installs on a member that is not ready, skips
  identical pins, stops at the first failure, writes the ledger only after a verified
  install, and prints the summary table. The ledger is written right after the install
  succeeds, as step 3 of the prompt's execute order. "Verified" means: the mounted
  archive digest passed (`sha256sum` in the container) and `plugins install` exited 0.
  If the later restart or verification fails, the second skip condition (probe shows the
  pinned version, `loaded`) still forces a reinstall on the next run; see *Open points*.
- **`upgrade` uses the start window → met.**
- **Golden rendering unchanged → met.** `tests/test_rollout_foundation.py` and
  `tests/fixtures/rollout-foundation` are unchanged and green (14 passed). The test does
  not read `/workspaces/clawonshape/deploy/team.json`, so no inventory hash was needed.
- **All checks green; no deployment data in the diff → met.** Only synthetic names
  (`alpha`, `beta`, `example-cad`), generated archives and `fake-token-for-tests` are used.

### Changed expectations in existing tests

| Test | Before | After |
|---|---|---|
| `test_cli.py::test_setup_quadlets_execute_starts_unchanged_service` | two runs, `restart_calls == 2` | three runs: first `restart` (new), second `start` (unchanged, inactive), third nothing (unchanged, active); `calls == ["restart", "start"]`; message "starting only inactive members" |
| `test_cli.py::test_setup_quadlets_execute_deploys_files`, `..._retires_legacy_service_before_restart` | fakes without `status`, no readiness seams | fakes gain `status`; `cli.default_probes` patched (readyz 200); call order unchanged |
| `test_cli.py::test_upgrade_execute_runs_backup_migration_and_health_checks`, `test_coordination.py::test_upgrade_replans_after_migration_and_preserves_operator_settings` | patched `cli.wait_for_health` | patch `cli.default_probes`; call order unchanged |
| `test_reusable_plugin_sync.py` (both tests) | patched `cli.sync_plugin`, no probes | patch `member_ops.sync_plugin` and `cli.default_probes`; assertions unchanged |

## Behaviour changes for operators

**`setup` (v1)**

- New preview block `Member actions:` with lines
  ` - <member>: restart|start|none (<reasons>)`. The reasons are artifact file names,
  `runtime_config`, or `unchanged, <unit reason>`.
- The preview reads `systemctl --user status` for unchanged members only. This is
  read-only.
- With `--execute`, unchanged active members are no longer restarted. This fixes C1.
- New per-member lines `Waiting for <m> ...` and `WARNING: <m> is degraded: ...`.
- New `Setup summary:` table; exit 1 on the first failure.
- The message for an unchanged plan is now "No rendered changes detected; starting only
  inactive members."

**`sync-plugins`**

- The preview now probes members that have plugins: one `/readyz` GET plus
  `podman exec <ctr> openclaw plugins list --json`, and for npm pins
  `plugins inspect <id> --runtime --json`. All are read-only.
- Per-plugin output adds `version:` and `action:` lines. Per member it adds
  `readiness=<state>` and "managed plugin config would change". The preview ends with
  `Members to restart: …`.
- An invalid managed config now fails in the preview (exit 1). Before, it failed only
  during `--execute`. This is fail-closed.
- With `--execute`:
  - members run one at a time;
  - a stopped member fails at once;
  - identical pins are not reinstalled;
  - a member is restarted only if something was installed or its config changed;
  - members after the first failure are not touched;
  - the run ends with a `Plugin sync summary:` table; exit 1 on failure.

**`upgrade`**

- The preview adds the line `readiness wait: up to <N>s`.
- With `--execute`, Clawake waits up to `readiness.start_window_seconds` (default 180 s,
  was about 60 s).
- `Health check:` now prints `readiness=<state> after <s>s`. The failure text changes from
  "health check failed: …" to "readiness failed: <codes>".

**Channel readiness (`readiness.channels: true`)**

- `lifecycle: "ready"` now counts as up.
- `connected: false` gives the new reason `channel_not_connected`.
- A missing lifecycle with `running: true` is now `degraded` (`channel_lifecycle`). Before,
  this case was accepted.

**Unchanged:** `restart`, `stop`, `teardown`, every v2 command, rendered Quadlets.

## Example previews (synthetic two-member fixture)

Generated with the test fakes. Setup:

- `alpha` and `beta` are deployed and active, both pinned to `example-cad` 1.2.3, and the
  ledger records 1.2.3.
- Then `alpha`'s pin changes to a new archive, 1.2.4. Its `artifact_path` changes, so
  `alpha.container` changes.
- `beta` is unchanged.
- `<tmp>` is the pytest temporary directory.

```text
$ clawake setup -c <tmp>/inventory.yml   [exit 0]
Setup plan for cluster 'c' (single_host): 1/2 member(s) changed
 - alpha [developer] quadlet: write <tmp>/quadlets/alpha.container
Member actions:
 - alpha: restart (alpha.container)
 - beta: none (unchanged, unit_active)
  Apply: retire legacy services, prepare runtime config, reload systemd, then one member at a time: act, wait for ready/degraded within its start window; stop at the first failure.
DRY RUN setup-quadlets complete. Re-run with --execute to mutate state.

$ clawake setup -c <tmp>/inventory.yml --execute   [exit 0]
...
Deployed <tmp>/quadlets/alpha.container
$ systemctl --user daemon-reload
$ systemctl --user restart alpha.service
Waiting for alpha to become ready (start window 180s)...
WARNING: alpha is degraded: plugin_version_mismatch
Setup summary:
  member  action   result  readiness  ready_s  reasons
  alpha   restart  ok      degraded   46       plugin_version_mismatch
  beta    none     ok      -          -        -

$ clawake sync-plugins -c <tmp>/inventory.yml   [exit 0]
Plugin sync plan for 2 pinned plugin(s)
 - alpha: readiness=degraded
 - alpha: example-cad (sha256:72146761ae7f52d822abf2a2b5c9e15c4701362ea6befe88eda64f44b7b4a46b)
   mount: <tmp>/artifacts/example-cad-1.2.4.tgz -> /opt/clawake/plugins/example-cad.tgz:ro
   version: 1.2.4
   action: install (digest_changed, version_mismatch)
 - beta: readiness=ready
 - beta: example-cad (sha256:d8ee1453fd8b251d29c3ad6a0ec3f80ef940f48ab9dff18289482c817563cca2)
   mount: <tmp>/artifacts/example-cad-1.2.3.tgz -> /opt/clawake/plugins/example-cad.tgz:ro
   version: 1.2.3
   action: skip
Members to restart: alpha
DRY RUN sync-plugins complete. Run setup --execute first when an archive mount is new or changed, then re-run with --execute.

$ clawake sync-plugins -c <tmp>/inventory.yml --execute   [exit 0]
... (same plan) ...
$ podman exec alpha-ctr openclaw plugins install --force --accept-capabilities /opt/clawake/plugins/example-cad.tgz
Installed verified plugin example-cad on alpha
$ systemctl --user restart alpha.service
Waiting for alpha to become ready (start window 180s)...
Plugin sync summary:
  member  action   plugin       version  digest               readiness  ready_s
  alpha   install  example-cad  1.2.4    sha256:72146761ae7f  ready      46
  beta    skip     example-cad  1.2.3    sha256:d8ee1453fd8b  ready      -
```

`ready_s` is the fake 46 s start delay, taken from the P1 live evidence.

## Ledger format change

File: `<state_path>/clawake-managed-plugin-runtime.json`, same file and same `version: 1`.

Before:

```json
{"version": 1, "agentToolAllow": {...}, "globalToolAllow": [...]}
```

After (`installedPlugins` appears only after the first verified archive install):

```json
{
  "version": 1,
  "agentToolAllow": {...},
  "globalToolAllow": [...],
  "installedPlugins": {
    "example-cad": {"sha256": "sha256:<64 hex>", "version": "1.2.4"}
  }
}
```

- Entries are validated: a `sha256:` + 64-hex digest and a string version. A malformed
  ledger fails closed with `ValueError`.
- Entries for plugins that are no longer declared as archives are dropped when the grants
  are reconciled.
- npm pins get no ledger entry. Their skip check is `plugins inspect --runtime --json`.

**Migration.** No member has a ledger digest today. The **first `sync-plugins --execute`
after deployment installs every archive plugin once, with reason `ledger_missing`, and
restarts those members**, even if the running version already matches. From the next run
on, matching pins are skipped. An older Clawake that rewrites the ledger drops
`installedPlugins`; the next run then installs once more (`ledger_missing`).

## Live E2E request

This repeats the 2026-10-16 single-member plugin rollout. `<inv>` is the deployed
inventory and `<dev>` is the developer member whose plugin pin changes. Operator consent
is required.

1. `uv run clawake status -c <inv> --wait --timeout 60`
   → both members `ready`, exit 0. This is the baseline.
2. Change the pin of `<dev>` in the inventory (new `artifact_path`/`sha256`).
3. `uv run clawake setup -c <inv>`
   → `<dev>: restart (<dev>.container)`; the other member shows `none (unchanged, unit_active)`.
4. `uv run clawake setup -c <inv> --execute`
   → expected commands:
   - `systemctl --user daemon-reload`
   - `systemctl --user restart <dev>.service`, then the wait (about 46 s)
   - summary: `<dev> restart ok degraded … plugin_version_mismatch` (new archive mounted,
     not installed yet); the other member `none ok`
   - the other member is **not** restarted; check its unit's `ActiveEnterTimestamp`.
5. `uv run clawake sync-plugins -c <inv>`
   → `<dev>`: `install (digest_changed, version_mismatch)` on this first P2 run; the other
   member: `install (ledger_missing)` (migration). `Members to restart` lists both.
   - To avoid touching the other member on this first run, use `-m <dev>` in steps 5–6.
6. `uv run clawake sync-plugins -c <inv> -m <dev> --execute`
   → expected commands:
   - readiness probe (`/readyz`, `podman exec <ctr> openclaw plugins list --json`)
   - `podman exec <ctr> sha256sum /opt/clawake/plugins/<id>.tgz`
   - `podman exec <ctr> openclaw plugins install --force --accept-capabilities /opt/clawake/plugins/<id>.tgz`
   - ledger write
   - `systemctl --user restart <dev>.service`, then the wait
   - `plugins list` verification
   - summary row `install … <new version> sha256:<12 hex> ready <s>`; exit 0
7. Repeat step 6 → summary `skip`; no install, no restart; exit 0. This proves idempotence.
8. `uv run clawake status -c <inv> --wait --timeout 60`
   → both members `ready`, `<dev>` at the new version.

Optional, when operator time allows:

- Set `readiness.channels: true` on one member in a scratch copy of the inventory, then
  run `status`. Expect `ready` on a healthy member; this is the live check of the channel
  fix.
- Upgrade: `upgrade --execute` on a non-critical member, to observe
  `Health check: readiness=ready after <s>s`.

## Proposed `architecture.md` text (for the architect)

`#lifecycle-contracts`, replace step 6 and the paragraph after it:

> 6. Per selected member, in order: restart members whose rendered artifacts or managed
>    runtime configuration changed, start unchanged members whose unit is not active, and
>    leave unchanged active members alone. After each restart or start, wait until the
>    member is `ready` or `degraded` within `readiness.start_window_seconds`; stop at the
>    first failure and report the remaining members as not run.
>
> Setup converges artifact contents and restarts only what changed (ADR-003). Unchanged
> but stopped services are started, so a re-run still reconciles stopped services. It is
> not a continuous reconciliation loop. …

`#lifecycle-contracts`, last paragraph, add:

> `sync-plugins` waits for readiness before installing, skips plugins whose installed
> archive digest (recorded by Clawake in the plugin runtime ledger) and loaded version
> match the pin, restarts only on change, verifies the pinned version after the restart,
> and stops at the first failing member.

Upgrade paragraph: replace "checks health" with "waits for member readiness within the
start window".

## Open points and risks

- **OpenClaw facts.**
  - Verified: docs.openclaw.ai/cli/plugins/install says `--force` "does not bypass
    `security.installPolicy`" and does not approve capabilities; `--pin` is npm-only.
  - Verified: docs.openclaw.ai/cli/channels documents `lifecycle: "blocked"` with
    `running: false`, and the starting shape `{status: "starting", startupPhase}`.
  - Unverified in the docs: lifecycle `ready` and the field `connected`. Both rest on the
    live sample in `docs/evidence/2026-10-19-member-readiness-status.md`.
  - Unverified: whether a running gateway needs a restart to load a plugin installed
    through it. The install page says the install "applies the source through that
    Gateway". The restart after install is kept, as the prompt requires.
- **Same `artifact_path` with new content.** If an archive is replaced in place, the
  rendered Quadlet does not change. `setup` then reports `none`, and the container may
  still see the old bind-mounted inode. `sync-plugins` fails closed on the mounted digest
  check ("Run setup --execute before syncing"), but `setup` will not restart the member.
  The remedy is a new file name per version, as today's deployed pins do, or
  `restart -m`. A digest-aware Quadlet change would fix this, but it changes rendering
  and is out of scope.
- **Changed shared networks.** A changed shared-network Quadlet does not mark its members
  `changed`; only their own artifacts count. When a network is introduced, the member
  Quadlets change as well.
- **Ledger timing.** The ledger is written after the install succeeds and before
  restart and verification (prompt step order). If the restart or verification fails,
  the ledger says "installed". The probe condition of the skip rule still forces a
  reinstall, as `test_sync_reinstalls_when_ledger_matches_but_runtime_differs` shows. If
  `ensure_plugin_runtime_config` fails after a ledger write, the member reports `failed`
  and the next run re-evaluates.
- **Preview cost.** `sync-plugins` previews now make read-only probes (a `/readyz` GET and
  `podman exec … plugins list`). The `setup` preview reads `systemctl --user status`
  for unchanged members.
- **First-run migration.** The first P2 `sync-plugins --execute` without `-m` restarts
  every member that has archive plugins once (`ledger_missing`). Use `-m` for the live
  test.
- **Unit state `activating`.** An unchanged member whose unit is `activating` gets
  `start`. This is harmless: systemd joins the running start job. It is then waited on.

## Out-of-scope findings

- `src/clawake/services/plugins.py:64` (`_verify_mounted_artifact`), `:129`
  (`_verify_installed_npm_plugin`) and `:164` (`sync_plugin`) call `subprocess.run`
  directly, with no runner seam and no timeout. Tests patch
  `clawake.services.plugins.subprocess.run`. P2 injects `install` into `run_plugin_sync`,
  but the install step itself is still unbounded.
- `src/clawake/services/runtime_upgrade.py:78` and `:87` (`verify_runtime`) also call
  `subprocess.run` with no timeout.
- `tests/`: there is no shared `conftest.py` guard that forbids real `systemctl`/`podman`
  or loopback HTTP. The guard for this report was a scratch plugin; a committed autouse
  guard would turn silent host access into a failure.
