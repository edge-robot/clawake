---
prompt: docs/prompts/2026-10-16-integrate-rollout-foundation.md
branch: integrate/rollout-foundation
commits: 55ea8df..de75347
status: done
e2e-request: required
---
# Report: Integrate demo/gateway-native-read and feat/agent-mcp-provisioning

## Summary

`feat/agent-mcp-provisioning` (e505969) is merged with `--no-ff` into
`integrate/rollout-foundation` (base 08f5a11). The merge commit is 55ea8df, with parents
08f5a11 and e505969. All three conflicts are resolved without a design decision:

- **MCP sources.** Both MCP models write into the desired state: the `onshape_mcp` /
  `onshape_mcp_enabled` instance flags and `openclaw.mcp_servers`.
- **Name conflict.** Validation rejects an instance that has `onshape_mcp: true` and
  `openclaw.mcp_servers.onshape`. Planning also refuses the conflict if validation was
  bypassed.
- **Drift guard vs. adoption.** The two rules apply to disjoint cases:
  - The drift guard `Managed field changed outside Clawake` (theirs) only checks paths
    that are already in the ownership ledger.
  - The one-time adoption of a disabled hosted Onshape connector (ours) only applies to
    paths that are not yet in the ledger.

  So both rules are kept unchanged. An unchanged re-run is a no-op. Tests prove this.

**Comparison against the deployed inventory.** For `deploy/team.json`, the rendered
Quadlets, the desired runtime config, the planned `openclaw.json` and ledger, the
plugin/grant runtime config, `validate`, and the `setup` / `sync-plugins` previews are
byte-identical between base and merge.

**The inventory changed while I worked.** The operator modified `deploy/team.json`
twice during this run (details under *Golden comparison*). The final comparison ran
against one stable version.

**Commits after the merge:**
- c1eab81: ruff format of files touched by the merge.
- 243b98e: regression and constraint tests with a synthetic fixture.
- de75347: one integration fix. Theirs' v2 setup created `workspace/.openclaw` instead
  of the `state_path`.
- The report commit follows de75347.

## Changed files

`git diff --stat 08f5a11 HEAD`: 36 files, +2358 / −75. Files from theirs that the merge
did not change further are marked *(theirs)*.

| File | Purpose |
|---|---|
| `docs/inventory-v2.md` | Conflict resolved: keeps the focused Onshape MCP section (ours) and the v2 operation examples (theirs). Adds the section "MCP server models and the conflict rule". |
| `docs/mcp-provisioning.md` | (theirs) generic OAuth MCP provisioning |
| `docs/runtime-foundation-review.md` | (theirs) review notes |
| `src/clawake/cli.py` | auto-merged: `--service`, v2 dispatch, `upgrade-service`, sync-plugins restart semantics (theirs); `stop`, `onshape-mcp`, global grants, `state_path` (ours). Ruff-formatted. |
| `src/clawake/config.py` | auto-merged `McpServerSpec`, `mcp_servers` and service fields (theirs) with `state_path`, `onshape_mcp*`, `global_tool_allow` (ours). **New:** validator against the `onshape` name conflict. |
| `src/clawake/services/deployment.py` | (theirs) plans covering services and instances |
| `src/clawake/services/gateway_config.py` | auto-merged: theirs owns only grants it added; ours keeps global grants and `state_path`. Ruff-formatted. |
| `src/clawake/services/render.py` | (theirs) service rendering |
| `src/clawake/services/resources.py` | (theirs) v2 lifecycle. **Fix:** prepares `runtime_state_path`. |
| `src/clawake/services/runtime_config.py` | conflict resolved: both MCP sources, a defensive override check, drift guard and adoption together |
| `src/clawake/services/service_upgrade.py`, `upgrade.py` | (theirs) pinned service upgrades |
| `templates/quadlet/openclaw.container.j2` | auto-merged: `depends_on` ordering (theirs, v2 only) and the `runtime_state_path` volume (ours) |
| `tests/fixtures/rollout-foundation/team.yml` | **New** synthetic two-member fixture with the deployed shape |
| `tests/fixtures/rollout-foundation/golden/**` | **New** goldens: 6 Quadlets, 6 runtime-plan files, 4 plugin-runtime files |
| `tests/test_rollout_foundation.py` | **New** 14 tests: golden, constraints 3/4/5, v2 `state_path` |
| `tests/test_gateway_config.py` | conflict resolved: both test sets kept. Ruff-formatted. |
| `tests/test_inventory_v2.py`, `test_mcp_provisioning.py`, `test_reusable_plugin_sync.py`, `test_service_runtime.py` | (theirs); `test_mcp_provisioning.py` ruff-formatted |

## Conflict table

| File | Hunk | Ours (08f5a11) | Theirs (e505969) | Resolution | Test |
|---|---|---|---|---|---|
| `runtime_config.py` | `desired_fields()` MCP desired state | `desired.update(mcp)` from the `onshape_mcp*` flags | `desired[f"mcp/servers/{name}"]` from `settings.mcp_servers` | Both kept, flags first. If a name is already present, planning raises `ValueError` and never overrides. `InstanceSpec` rejects the conflict at validation time. | `test_onshape_flag_and_mcp_servers_onshape_are_rejected`, `..._conflict_without_enabled_flag`, `test_desired_fields_refuses_override_even_without_validation`, `test_onshape_flag_and_distinct_mcp_server_coexist` |
| `runtime_config.py` | `plan_runtime_files()` existing-field reconciliation | Adopts a not-yet-managed `mcp/servers/onshape` when it is the disabled hosted connector | Raises `Managed field changed outside Clawake` when a ledger-managed field differs from its ledger value | Both kept. The guard runs first and only for `path in old_fields`. Adoption only applies to `path not in old_fields`. | `test_hosted_connector_is_adopted_and_then_guarded`, `test_hosted_connector_reappearing_after_adoption_is_drift`, `test_enabled_or_unknown_connector_is_not_adopted`, `test_unchanged_rerun_of_fixture_is_a_noop`, `test_toggling_onshape_enabled_flag_is_not_drift`; theirs: `test_unmanaged_and_externally_modified_server_are_not_overwritten` |
| `tests/test_gateway_config.py` | tail after `test_preserves_unmanaged_agent_tool...` | 3 global-grant tests (9b152b6) | `test_preexisting_grant_is_not_claimed_by_inventory` (03445a7) | Both kept unchanged, side by side | the tests themselves, plus `test_global_and_agent_grants_preserve_operator_policy_together` |
| `docs/inventory-v2.md` | end of file | "Focused Onshape MCP exception" section starting with "Generic v2 deployment is still unsupported" | "Operation examples" plus service user/read-only notes | Both kept. The ours lead-in is reworded because theirs makes generic v2 deployment supported. New section on both MCP models and the conflict rule. | n/a (doc) |
| `cli.py`, `config.py`, `gateway_config.py`, `openclaw.container.j2` | auto-merged | see *Changed files* | see *Changed files* | Reviewed. No semantic conflict for v1. One cross-line gap found in `resources.py` (not a textual conflict) and fixed in de75347. | `test_v2_setup_prepares_state_path_not_workspace_openclaw` |

## Golden comparison (deployed inventory, scratch only)

**Method.** A throwaway script in the session scratchpad outside the repo
(`/tmp/claude-1000/…/scratchpad/golden.py`) loads `deploy/team.json` through
`clawake.config.load_inventory`. It writes per instance:

- the Quadlets, via `render_instance_assets` and `render_shared_network`;
- `desired_fields`;
- `plan_runtime_files` against a redirected empty state directory;
- `ensure_plugin_runtime_config` against a redirected empty state and a seeded state
  (synthetic `tools.allow: ["read"]`).

It also captures `clawake validate`. The base run uses an `git archive 08f5a11` tree
through `PYTHONPATH`, with that tree's templates. The merged run uses the checkout. The
host was never touched.

The `setup` and `sync-plugins` CLI previews were captured the same way.
`sync-plugins` exits 1 identically on both sides because the plugin archives exist only
on the host.

**`deploy/team.json` hash during the run (the file changed twice):**

| When | sha256 |
|---|---|
| Precondition check | `125f7738f22d9f0b1e63136f5bc49b0483df6c2e3b4ad75ac46c68f2f95ab3da` |
| First base golden and first merged golden | `9e0182244234caf589c214d14b3add18dc3408058f3e51ac832fffce005daa2a` |
| Final run; same before and after the back-to-back base/merged comparison | `68b9b10f98dbc0295faba87e5555bf43639dd2621e425952faf50019adb3cff3` |

The operator changed the file between my runs. Between `9e01…` and `68b9…`, the only
visible effect is the Lead plugin `artifact_path` version. I did not write to
`/workspaces/clawonshape`.

**Diff results:**
- First pair, both at `9e01…`: `diff -r base merged` is empty.
- Final pair, both at `68b9…`, base with base templates: `diff -r base merged` is empty
  across 17 files (Quadlets, desired, plans, plugin runtime, validate). The CLI
  previews are also identical.
- `clawake validate` on both sides: `Valid inventory: <cluster>; 2 member(s), 10
  artifact change(s).`, exit 0.
- A first cross-version comparison mixed inventory `9e01…` with `68b9…`. It showed only
  that artifact path. This is an inventory difference, not a code difference, and I
  discarded it.

**Not covered.** No deployed `onshape-mcp` service YAML exists in `deploy/`, so
`onshape-mcp --action plan` could not be compared offline. The merge does not touch
`services/onshape_mcp.py`. `render_shared_network`, which it uses, is unchanged.

## Checks

| Command | Result |
|---|---|
| Base 08f5a11: `uv run pytest -q` | 259 passed, 2 skipped |
| Base 08f5a11: `uv run ruff format --check .` | **failed**: 3 files would be reformatted (`cli.py`, `gateway_config.py`, `tests/test_gateway_config.py`) |
| `uv sync --locked` | pass |
| `uv run ruff check .` | pass ("All checks passed!") |
| `uv run ruff format --check .` | pass (39 files already formatted) |
| `uv run pytest -q` | pass: **295 passed, 2 skipped** |
| `uv lock --check` | pass |

**Test count.** Collected from `git archive` trees:
- cefbfe5 (merge base): 244 passed + 2 skipped.
- e505969 (theirs): 266 + 2, so theirs adds 22 tests.
- Base 259 + 22 = 281. That is exactly the count right after the merge.
- With 14 new tests: 295. The criterion "≥ 281" is met.

**Mutation check of the new tests** (scratch copy of `src/` via `PYTHONPATH`; repo not
modified):

| Mutation | Tests that failed |
|---|---|
| Remove the drift guard | 2 |
| Remove the adoption | 1 |
| Remove the conflict validator | 2 |
| Revert theirs' agent-grant ownership | 1 (combined grant test) |
| Revert ours' global-grant ownership | 2 (golden + combined test) |

No `# noqa` or `# type: ignore` were added.

## Acceptance criteria

- **Merge commit with both parents and a conflict list: met.** 55ea8df has parents
  08f5a11 and e505969. The message lists all three conflicts, the extra validator and
  the reviewed auto-merged files.
- **All checks green; test count ≥ base + theirs: met.** See *Checks*: 295 ≥ 281. The
  format check needed c1eab81 because base was already red.
- **`diff -r` golden base vs. merged empty: met.** Empty for a stable inventory version.
  The hash changes during the run are reported above.
- **Tests for constraints 3, 4 and 5 exist and pass: met.** All in
  `tests/test_rollout_foundation.py`:
  - **Constraint 3** (flags and `mcp_servers`):
    - `test_onshape_flag_and_mcp_servers_onshape_are_rejected`
    - `test_onshape_flag_and_mcp_servers_onshape_conflict_without_enabled_flag`
    - `test_desired_fields_refuses_override_even_without_validation`
    - `test_onshape_flag_and_distinct_mcp_server_coexist`
    - `test_fixture_keeps_onshape_flags_and_state_path`
  - **Constraint 4** (drift guard vs. adoption, no-op re-run):
    - `test_hosted_connector_is_adopted_and_then_guarded`
    - `test_hosted_connector_reappearing_after_adoption_is_drift`
    - `test_enabled_or_unknown_connector_is_not_adopted`
    - `test_unchanged_rerun_of_fixture_is_a_noop`
    - `test_toggling_onshape_enabled_flag_is_not_drift`
    - theirs' `test_unmanaged_and_externally_modified_server_are_not_overwritten` still
      passes.
    - Ours had no adoption test before; these are the first.
  - **Constraint 5** (grants):
    - Existing 9b152b6 tests in `tests/test_gateway_config.py`, all pass:
      `test_explicit_global_grants_preserve_policy_and_withdraw_only_owned_names`,
      `test_global_grants_refuse_default_policy_and_empty_withdrawal`,
      `test_global_grants_reject_wildcards_groups_and_duplicate_names`.
    - Existing 03445a7 tests, all pass: `test_preexisting_grant_is_not_claimed_by_inventory`
      (×2), plus `test_sync_without_plugins_revokes_only_managed_grants_and_preview_is_inert`,
      `test_failed_plugin_install_does_not_restart_or_reconcile_member` and
      `test_v2_setup_plan_preserves_openclaw_tool_grants` in `tests/test_reusable_plugin_sync.py`.
    - Combined: `test_global_and_agent_grants_preserve_operator_policy_together` and
      `test_global_grants_never_create_a_host_allowlist`.
  - **Regression:** `test_fixture_render_and_runtime_plan_match_golden`. The goldens were
    generated with the 08f5a11 code. A second cross-check rendered the fixture Quadlets
    with the 08f5a11 templates: 6/6 identical.
- **No real deployment data in the repo: met.** Method: a scratch script collected every
  string leaf of `deploy/team.json` (74 values) and every path/host segment of length
  ≥ 4 (111 tokens in total). It searched the added lines of
  `git diff 08f5a11 HEAD -- . ':(exclude)uv.lock'` plus the prompt file.
  - Every hit is a generic schema word or public value: `state`, `workspace`, `lan`,
    `developer`, `product_owner`, `discord`, `message`, `127.0.0.1`, `18789`, public tool
    names such as `onshape__onshape_api_call`, and the OpenClaw version in theirs' doc.
  - `clawonshape` appears only inside the MCP URL `http://clawonshape-mcp:8080/mcp`, which
    has been in public code since 4da3957.
  - `localhost` appears only in Control UI origins derived from the fixture's fake ports.
  - No Discord IDs, host paths, host user names, instance or container names were found.
  - The fixture uses `/srv/example/...`, IDs `0000…0001`-style and digests `sha256:1111…`.
- **`docs/inventory-v2.md` describes both MCP models and the conflict rule: met.**
  Section "MCP server models and the conflict rule". Architecture-doc proposals are below.

## Behaviour changes for operators

**For the deployed v1 inventory:**
- The rendered Quadlets, the runtime config and the `setup` / `sync-plugins` previews
  are unchanged; the golden comparison proves this.
- Behaviour that can still differ on the live host:
  1. **Drift guard (theirs).** `setup` (preview and `--execute`) now **fails** with
     `Managed field changed outside Clawake: <path>` when a ledger-managed field in the
     live `openclaw.json` was edited outside Clawake. Before, it was silently
     overwritten. This affects `mcp/servers/onshape`, A2A, the workspace and subagent
     fields. It does not fire on an unchanged re-run.
  2. **`sync-plugins --execute` (theirs, 95c7601/03445a7):**
     - A member is restarted only if one of its plugins was installed or its runtime
       config changed. Members that failed are never restarted.
     - Without declared plugins, it no longer exits early; it still reconciles grants.
     - For the deployed inventory, every member has a plugin, so every member is still
       restarted after a successful install, as before.
  3. **Agent grant ledger (03445a7).** Clawake now owns only tool names it added. Names
     that the operator already had are kept when they are withdrawn from the inventory.
     The golden shows identical ledgers for the deployed inventory.

**New for all inventories:**
- Validation error when `onshape_mcp: true` is combined with `openclaw.mcp_servers.onshape`.
- From theirs:
  - `--service/-s` on `setup`, `restart`, `status`, `teardown` and `logs`;
  - v2 inventories use the generic lifecycle;
  - the `upgrade-service` command;
  - `depends_on` renders `After=`/`Requires=` (v2 only).
- v2 `setup --execute` creates the instance `state_path` instead of `workspace/.openclaw`
  (de75347).

## Live E2E request

Run from the operator checkout. First on the current base checkout (4da3957 line), then
on `integrate/rollout-foundation`. Compare the output byte-for-byte. All of these are
previews.

```bash
uv run clawake validate     -c <clawonshape>/deploy/team.json
uv run clawake setup        -c <clawonshape>/deploy/team.json
uv run clawake sync-plugins -c <clawonshape>/deploy/team.json
uv run clawake status       -c <clawonshape>/deploy/team.json --format json
uv run clawake onshape-mcp  -c <onshape-mcp service.yml> --action plan
```

**Expected:**
- Identical output on both checkouts.
- `setup` does **not** raise `Managed field changed outside Clawake`. If it does, the
  live `openclaw.json` differs from the ledger (`clawake-managed-runtime.json`) at the
  named path. Resolve that before switching; do not bypass the guard.
- `onshape-mcp --action plan` lists no changes if it was already applied.

**Optional, only after identical previews:**
```bash
uv run clawake setup -c <clawonshape>/deploy/team.json --member <engineer> --execute
uv run clawake setup -c <clawonshape>/deploy/team.json --member <engineer>
```
The second command is a preview and must report the member as unchanged. Note that
`setup --execute` restarts the selected member (unchanged behaviour, #5).

## Open points and risks

- **OpenClaw write-back is unverified.** https://docs.openclaw.ai/cli/mcp confirms that
  `openclaw mcp set/unset` write `mcp.servers` entries. The page does not document
  whether `login`, `configure`, `tools`, `doctor` or config normalization rewrite an
  existing entry. If any of them do, the drift guard will stop `setup` on the next run.
  This is fail-closed, but it blocks the rollout. The E2E preview above detects it.
- **The inventory changed during the run.** Only the final, stable version was compared
  end to end. If the operator changes `team.json` again, the E2E preview comparison is
  the authoritative check.
- **`onshape-mcp` service spec not compared offline.** It is not in `deploy/`.
- **Base format failure.** The base was already failing `ruff format --check`. The
  formatting commit c1eab81 touches only files the merge changed.

## Proposals for `docs/architecture.md` / ADR (not applied)

1. **Express the `onshape_mcp` flags through `mcp_servers` later.**
   - Today `McpServerSpec` only allows HTTPS with `auth: oauth`. The local Onshape
     server is plain HTTP on a private Podman network, with an external token owned by
     ClawOnshape.
   - Unifying the two needs an ADR decision: allow `auth: none` or `external` plus
     `http` restricted to private network aliases, and add an explicit, one-time
     `adopt: {url, enabled: false}` rule.
   - Until then, keep the flags as a preset and keep the name-conflict rule.
2. **Document the ownership-ledger semantics in `architecture.md`:**
   - fail-closed drift guard on managed fields;
   - one-time, explicit adoption;
   - only Clawake-added grant names are owned;
   - an unchanged re-run is a no-op.
3. **Mention the optional `state_path` and the v2 resource lifecycle** in
   `architecture.md`. Both now exist on the same branch.
4. **ADR note.** Before #24 (one-step rollout), record which OpenClaw commands may
   rewrite `mcp.servers.*`, verified per OpenClaw release.

## Out-of-scope findings

- `src/clawake/services/resources.py:140-145`: the v2 `setup` does not call
  `ensure_browser_cache` for browser images; v1 does at `src/clawake/cli.py:634`. Not
  fixed.
- `docs/inventory-v2.md` (ours section): "`InstanceSpec.onshape_mcp: true` reconciles the
  known local native server in disabled state" is only true without
  `onshape_mcp_enabled`. The new MCP section states it correctly; the old sentence was
  left as is.
- `docs/prompts/2026-10-16-integrate-rollout-foundation.md` was edited on disk at 16:28
  (worktree path redacted) after I first read it; no instructions changed. It is
  committed as found.
