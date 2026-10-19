---
target-repo: edge-robot/clawake
branch: feat/26-discord-channel
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#26
architecture-ref: clawake/docs/ADR.md#adr-005-discord-channel-provisioning-from-the-inventory-and-private-site-values
base-commit: f74efd8 (feat/24-rollout 60c784a + ADR-005 accepted)
status: issued
---
# P4: Discord channel provisioning and private site values (ADR-005, #26, #21)

## Goal

Clawake renders and owns `channels.discord` of each v1 member from the inventory. Private
IDs come from an operator-written `site_values` file through whole-string `${site:<name>}`
references. The bot token is only ever a SecretRef to an env variable. An existing
hand-written block is taken over only through the named adoption
`--adopt discord-channel`, which fails closed while the live token is a plaintext string.

## Context

- **Decision:** `docs/ADR.md` ADR-005 (accepted). It is binding and specifies the file
  format, the reference rule, the Discord model, rendering, adoption and acceptance. Also
  read ADR-001 (ledger, drift guard, named adoption) and ADR-004 (overlay loader and file
  safety, which you reuse).
- **Issues:** edge-robot/clawake#26 (this work), #21 (users allowlist; folds in).
- **Current code:**
  - `src/clawake/config.py`:
    - `load_inventory_layers()` runs `_expanded_inventory_data` (`${VAR}` expansion) →
      `apply_overlay` (runtime pins) → `Inventory.model_validate`;
    - `OpenClawSpec` (`lead_agent_id`, `subagents`, `a2a`, `mcp_servers`);
    - `ReadinessSpec.channels` (P1 opt-in channel probe).
  - `src/clawake/services/runtime_pins.py`: loader, permission checks and errors. Reuse
    these for the site file; do not copy them.
  - `src/clawake/services/runtime_config.py`:
    - `desired_fields()` returns ledger-owned paths (`channels/a2a/...`);
    - `plan_runtime_files()` applies the drift guard, the refusal to overwrite unmanaged
      fields and the only existing named adoption (`mcp/servers/onshape`);
    - `_changed_keys` feeds the preview.
  - `src/clawake/cli.py`: `_load_env_file_values` reads env **values**. Do not use it for
    the token check.
- **Live shape** (key paths only; the values are private and you never see them): both
  deployed members have `channels.discord` with `enabled`, `groupPolicy`,
  `guilds.<id>.channels.<id>.enabled` and a plaintext string `token`. On one member
  `enabled` is false.

## Binding constraints and prohibitions

1. **`site_values`** exactly as in ADR-005 item 1:
   - optional top-level v1 field; path resolution like `runtime_pins`;
   - JSON `{"version":1,"cluster":...,"values":{...}}`; values are strings or lists of
     strings; names match `^[a-z0-9][a-z0-9_.-]*$`;
   - unknown keys and a cluster mismatch are rejected with path-naming errors;
   - same permission rules as the overlay (refuse group/world-writable, warn if readable);
   - **Clawake never writes the file.** There is no write API and no `init` command;
   - v2 rejects it; a declared but missing file is an error.
2. **References** per ADR-005 item 2:
   - Only an entire string `${site:<name>}` is a reference. Substitute after `${VAR}`
     expansion and before `apply_overlay`. A string that contains `${site:` anywhere else
     is an error.
   - A list value may replace a whole field. A list value in a string-only position is a
     type error from model validation.
   - An unknown name fails loading. `validate` warns about unused names (names only).
   - The `${VAR}` expander must not swallow `${site:…}`. Add a test that the env lookup
     never sees `site:…`.
   - `validate` prints `site values: <path> (N values, M references)`.
   - Error messages name the inventory path of the reference and the site value
     **name**, never the value.
3. **Discord model** `OpenClawSpec.discord` per ADR-005 item 3, with every validation rule
   listed there:
   - snowflake pattern `^[0-9]{17,20}$` after substitution;
   - no `open`, no names, no `*`, no `dangerouslyAllowNameMatching`;
   - `allow_from` required iff `dm_policy: allowlist`, forbidden otherwise;
   - every enabled channel under `group_policy: allowlist` has non-empty `users` or `roles`
     (own or guild);
   - `enabled: false` allows no other key;
   - duplicate guild or channel IDs are rejected;
   - `token_env` matches the env-name pattern and is required when enabled;
   - defaults: `dm_policy: disabled`, `group_policy: allowlist`, `require_mention: true`.
4. **Rendering** per ADR-005 item 4:
   - **one** ledger-owned field `channels/discord` holding the whole object, with OpenClaw
     key names (`enabled`, `token`, `applicationId`, `dmPolicy`, `allowFrom`,
     `groupPolicy`, `guilds.<id>.{requireMention,users,roles,channels.<id>.{enabled,requireMention,users,roles}}`);
   - `token = {"source":"env","provider":"default","id":<token_env>}`;
   - omit empty lists and unset optionals; render deterministically;
   - members without `discord` behave exactly as today.
5. **Token env check.** `setup` (preview and `--execute`) fails closed if `token_env` is
   not a **key** in any of the member's `env_files`.
   - Write a new helper that parses key names only. It must never return, log or keep
     values.
   - Prove it with a test whose env value is a canary string that must not appear in any
     output, exception or ledger.
6. **Adoption `--adopt discord-channel`** on the setup command, per ADR-005 item 5:
   - Without the flag, an unowned existing `channels.discord` that differs from the
     rendered object is an error naming the rule and the flag. Keep the existing "Refusing
     to overwrite unmanaged OpenClaw field" style.
   - With the flag, a live `token` of type string → error with operator instructions
     (move it to `<token_env>` in the env file, replace it with the SecretRef). The
     message never contains the value.
   - **Preview redaction.** Changed key paths under `channels.discord` are printed with
     every snowflake segment replaced by `<id>`, and without values. This applies to all
     previews of `channels.discord`, not only adoption.
   - `--execute` takes the member backup before replacing, then records ownership.
   - After adoption, the drift guard applies as for every owned field. A second run with
     the flag is a no-op.
   - `rollout` does not get the flag. It must fail with the same named-rule error if
     adoption is pending.
7. **Readiness.** No new probe. Document in the manual that `readiness.channels: true` is
   the acceptance check (ADR-005 item 6).
8. **Scope limits.**
   - No changes to rollout, pins, plugin sync or readiness semantics beyond what is
     needed to pass the new loader stage.
   - No `accounts.<id>`, no v2, no dashboard work.
   - No ClawOnshape changes. Generic `${site:…}` substitution in plugin `config` comes for
     free from item 2; add one test for it and nothing ClawOnshape-specific.
   - No new dependencies. Public repository: synthetic snowflakes only, e.g.
     `100000000000000001`. Never run `--execute`, `podman`, `systemctl` or `openclaw`
     yourself. Never read `/workspaces/clawonshape/state`.

## Steps

1. Verify the preconditions: the branch is `feat/26-discord-channel` and HEAD is
   `f74efd8`. Run the checks and record the base test count (471 passed, 2 skipped
   expected).
2. Write failing tests first:
   - site file: format, rejections, permissions, missing file, v2;
   - references: whole-string only, partial is an error, unknown name, unused warning,
     lists, env expander isolation, plugin `config` substitution, ordering before
     `apply_overlay`;
   - Discord model: every validation rule in item 3, as a parametrized table;
   - rendering: golden JSON for a synthetic Lead (enabled, one guild, one channel,
     approvers) and a synthetic disabled member;
   - token env check incl. the canary;
   - adoption: no flag → named error; flag + string token → instructions without the
     value; flag + SecretRef token → backup then replace and own; second run no-op; drift
     after adoption → guard; `rollout` refuses pending adoption;
   - redaction: no snowflake from the synthetic site file appears in any preview output
     (scan the captured output for every site value);
   - unchanged behaviour: members without `discord` and inventories without
     `site_values` produce byte-identical renders (golden, as in P3a).
3. Implement:
   - e.g. `services/site_values.py`;
   - a `DiscordSpec` in `config.py`;
   - rendering in `runtime_config.desired_fields`;
   - the adoption flag through the setup CLI into `plan_runtime_files`.
4. Docs:
   - `docs/inventory-v2.md` (v1 section): site values, references, Discord model;
   - `docs/manual.md`: a migration walkthrough for an existing hand-written block with
     synthetic values (token to env file and SecretRef, write the site file, add the
     block, preview, `--adopt discord-channel`, `readiness.channels`).
   - Propose `architecture.md` text in the report.
5. Run the checks, commit (logical commits, `Refs: edge-robot/clawake#26`; the allowlist
   validation additionally `Refs: edge-robot/clawake#21`) and write the report.

## Acceptance criteria and tests

- [ ] Every item in step 2 has a named test that failed first. Run mutation checks on:
      partial-reference rejection, the allowlist-required rule, the string-token refusal,
      preview redaction, and the env-name-only helper (make it return values → the canary
      test fails).
- [ ] Golden renders are unchanged for inventories without `discord`/`site_values`.
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are all green.
- [ ] No real IDs, host paths, container names or deploy values in the diff, the tests or
      the report.

## Branch and worktree rules

- Commit only to `feat/26-discord-channel` in this checkout. No other branches, no
  worktrees, no push, no rebase. Commit this prompt unchanged together with your report.
- Leave the worktree clean.

## Expected report / handoff format

`docs/prompts/2026-10-19-discord-channel.report.md` in the agent-definition format.
Include:

- the final inventory keys, as a table (key, type, default, rule). The architect hands it
  to the operator session;
- the setup preview for the synthetic adoption: without the flag, with the flag and a
  string token, and with the flag and a SecretRef token;
- the exact operator migration sequence for a deployed two-member team (one enabled, one
  disabled member), with the read-only checks before `--execute`;
- the exact error texts for: partial reference, unknown site name, missing allowlist,
  string token.

Set `e2e-request: required`.
