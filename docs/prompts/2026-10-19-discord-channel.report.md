---
prompt: docs/prompts/2026-10-19-discord-channel.md
branch: feat/26-discord-channel
commits: 80db7de..6ee5f0d (plus the commit that adds this report and the prompt); follow-up 28fab84 (plus the follow-up report commit)
status: done
e2e-request: required
---
# Report: P4 Discord channel provisioning and private site values (ADR-005, #26, #21)

## Summary

- **Site values.** A v1 inventory may declare `site_values: <path>`, an operator-written
  JSON file. Inventory strings that are exactly `${site:<name>}` are substituted from it.
  The loader runs `${VAR}` expansion → site substitution → runtime pin overlay → model
  validation. The `${VAR}` expander no longer looks up `${site:…}`.
- **Discord model.** `openclaw.discord` (`DiscordSpec`) enforces every rule in ADR-005
  item 3, including the #21 allowlist rule.
- **Rendering.** The block renders as the single ledger-owned field `channels/discord`,
  with the token as an env SecretRef.
- **Token check.** `setup` (preview and `--execute`) fails closed if `token_env` is not a
  key in any of the member's env files. The new helper `env_file_keys` parses key names
  only.
- **Adoption.** `setup --adopt discord-channel` takes over an unowned hand-written block
  once:
  - a plaintext string token is refused, with instructions;
  - the member backup is taken before any write;
  - ownership is recorded;
  - a second run is a no-op;
  - the drift guard applies afterwards.
- **Rollout** has no flag and fails with the same named-rule error while an adoption is
  pending.
- **Preview redaction.** Every `channels/discord` key path is printed with IDs as `<id>`
  and an `(added)`, `(removed)` or `(changed)` marker. Values are never printed.
- **Docs.** `docs/inventory-v2.md` has the reference documentation. `docs/manual.md` has
  the migration walkthrough and the `readiness.channels` acceptance check.

## Changed files

```
 docs/inventory-v2.md                               | 116 +++   site values + Discord sections (v1)
 docs/manual.md                                     | 105 +++   operator section, acceptance check, migration walkthrough
 src/clawake/cli.py                                 | 111 ++-   setup --adopt, token_env check, adoption backups, validate line, warnings
 src/clawake/config.py                              | 228 +++++- DiscordSpec, site_values field, loader stage, expander isolation
 src/clawake/services/deployment.py                 |  16 +-    plan_deployment(adopt=...) passthrough
 src/clawake/services/env_keys.py                   |  39 +     env-file key names only, never values
 src/clawake/services/runtime_config.py             | 110 ++-   channels/discord rendering, named adoption, redacted changed keys, pending_adoptions
 src/clawake/services/runtime_pins.py               |  39 +-    read_private_json (shared file-safety reader), read_overlay_file wraps it
 src/clawake/services/site_values.py                | 226 +++++ site values file validation, reference substitution, error hints (no write API)
 tests/fixtures/discord-channel/golden/*.json       |  36 +     golden channels.discord for the synthetic Lead and disabled member
 tests/test_discord_channel.py                      | 905 +++++ model table, rendering, token check, adoption, redaction, unchanged behaviour
 tests/test_site_values.py                          | 432 +++++ file format, permissions, references, ordering, validate output
```

## Checks

| Command | Result |
|---|---|
| `uv sync --locked` | pass |
| `uv run ruff check .` | pass (`All checks passed!`) |
| `uv run ruff format --check .` | pass (`51 files already formatted`) |
| `uv run pytest -q` | pass: **581 passed, 2 skipped** (base f74efd8: 471 passed, 2 skipped; +110) |
| `uv lock --check` | pass |

- **Failing first.** Before the implementation, both new test modules failed at
  collection (`ImportError`: `clawake.services.site_values`, `DiscordSpec`,
  `env_file_keys`, `pending_adoptions` did not exist).
- **Host guard.** I ran the new tests with a scratch pytest plugin that makes
  `subprocess.run` of `systemctl`/`podman`/`journalctl`/`openclaw` and `urlopen` raise:
  110 passed.
- **Mutation checks.** I mutated a scratch copy of `src/` (via `PYTHONPATH`) and ran the
  relevant test module:

| Mutation | Result |
|---|---|
| M1: partial reference returns the string instead of raising | 4 `test_partial_reference_is_an_error[...]` fail |
| M2: allowlist-required channel rule disabled | `rejects[channel_without_allowlist]`, `rejects[channel_empty_allowlist]`, `test_missing_allowlist_error_text` fail |
| M3: string-token refusal disabled | `test_adoption_refuses_string_token_with_instructions`, `test_disabled_member_string_token_instructions` fail |
| M4: redaction returns the raw path | 5 tests fail, incl. `test_no_site_value_appears_in_any_preview`, `test_plan_keys_are_redacted_for_ids_outside_site_values` |
| M5: `env_file_keys` returns whole lines (values) | 11 tests fail, incl. `test_env_file_keys_returns_names_only` and `test_token_canary_never_leaves_the_env_file` |
| M6 (extra): expander lookup of `site:` restored | `test_env_expander_never_looks_up_site_references`, `test_env_variable_named_like_a_reference_is_ignored` fail |

- No `# type: ignore` or `# noqa` was added.

## Acceptance criteria

- **Every step-2 item has a named test that failed first** → met.
  - Site file: `test_malformed_site_files_are_rejected` (12 cases),
    `test_invalid_json_is_rejected_without_content`, `test_missing_site_file_is_an_error`,
    `test_group_or_world_writable_site_file_is_refused`, `test_readable_site_file_warns`,
    `test_v2_rejects_site_values`, `test_site_values_module_has_no_write_api`,
    `test_loading_never_writes_the_site_file`.
  - References: `test_partial_reference_is_an_error`, `test_unknown_site_name_fails_loading`,
    `test_validate_reports_counts_and_unused_names_only`,
    `test_list_value_in_string_position_is_a_model_type_error`,
    `test_reference_replaces_whole_fields_in_typed_positions`,
    `test_env_expander_never_looks_up_site_references`,
    `test_plugin_config_substitution_is_generic`,
    `test_substitution_runs_before_the_runtime_pin_overlay`.
  - Discord model: `test_discord_model_rejects` (34 rows), `test_discord_model_accepts`,
    `test_discord_model_defaults`.
  - Rendering: `test_rendered_discord_objects_match_golden` (Lead: enabled, one guild,
    one channel, approvers; disabled engineer).
  - Token env and canary: `test_env_file_keys_returns_names_only`,
    `test_setup_preview_fails_closed_without_token_env_key`,
    `test_token_canary_never_leaves_the_env_file`. The last one scans the output, the
    exception and every written file, including the ledger.
  - Adoption: `test_setup_without_flag_refuses_unowned_block`,
    `test_adoption_refuses_string_token_with_instructions`,
    `test_adoption_execute_backs_up_first_then_replaces_and_owns` (incl. the second-run
    no-op), `test_drift_after_adoption_trips_the_guard`,
    `test_rollout_refuses_pending_adoption`,
    `test_backup_failure_stops_adoption_before_any_write`.
  - Redaction: `test_no_site_value_appears_in_any_preview`.
  - Unchanged behaviour: `test_site_values_without_discord_render_byte_identical`,
    `test_member_without_discord_leaves_channels_discord_unowned`,
    `test_inventory_without_site_values_is_unchanged`.
- **Mutation checks on the five named rules** → met (table above).
- **Golden renders unchanged for inventories without `discord`/`site_values`** → met.
  - `tests/fixtures/rollout-foundation/golden/` is unchanged and its test passes.
  - Adding `site_values` and a reference to that fixture renders byte-identically.
  - No existing expectation changed.
- **All five checks green** → met.
- **No real IDs, host paths, container names or deploy values** → met. Only synthetic
  `1000000000000000xx` snowflakes, `canary-…` tokens and `example-*` names are used. I
  did not read `deploy/team.json`, `env/` or `/workspaces/clawonshape/state`.

## Final inventory keys

| Key | Type | Default | Rule |
|---|---|---|---|
| `site_values` (top level, v1) | path string | unset | resolved like `runtime_pins`; file must exist; v2 rejects it; cannot itself be a reference |
| site file `version` | int | — | must be `1` |
| site file `cluster` | string | — | must equal `cluster.name` |
| site file `values` | object | — | required; names `^[a-z0-9][a-z0-9_.-]*$`; value string or list of strings; other top-level keys rejected |
| `openclaw.discord.enabled` | bool | `true` | `false` renders `{"enabled": false}` and allows no other key |
| `…discord.token_env` | `^[A-Z_][A-Z0-9_]*$` | unset | required when enabled; must be a key in one of the member's `env_files` (setup) |
| `…discord.application_id` | snowflake | unset | optional |
| `…discord.dm_policy` | `disabled`\|`allowlist`\|`pairing` | `disabled` | no `open` |
| `…discord.allow_from` | snowflake list | `[]` | required non-empty iff `dm_policy: allowlist`, forbidden otherwise |
| `…discord.group_policy` | `allowlist`\|`disabled` | `allowlist` | no `open` |
| `…discord.guilds[].id` | snowflake | — | unique |
| `…guilds[].require_mention` | bool | `true` | |
| `…guilds[].users` / `.roles` | snowflake list | `[]` | unique entries; guild defaults |
| `…guilds[].channels[].id` | snowflake | — | unique across all guilds |
| `…channels[].enabled` | bool | `true` | |
| `…channels[].require_mention` | bool | unset | rendered only when set |
| `…channels[].users` / `.roles` | snowflake list | `[]` | unique entries |
| rule (#21) | — | — | `group_policy: allowlist`: every enabled channel needs own or guild `users`/`roles`; a guild without `channels` needs guild `users`/`roles` |

Snowflake: `^[0-9]{17,20}$` after substitution, YAML string. Names, `*`,
`dangerouslyAllowNameMatching`, `token`, `accounts` and other unknown keys are rejected.

## Setup preview for the synthetic adoption

Two members: `lead` (enabled) and `engineer` (disabled). Both start with a hand-written
block. Paths are shown as `<tmp>`.

**Without the flag** (exit 1):
```
Error: Refusing to overwrite unmanaged OpenClaw field: channels/discord (named adoption 'discord-channel': review the preview, then run setup with --adopt discord-channel)
```

**With the flag, string tokens** (exit 1; the first member that fails is reported):
```
Error: Cannot adopt channels/discord of member lead: the live token in <tmp>/lead/state/openclaw.json is a plaintext string. Move it into the member env file as DISCORD_BOT_TOKEN, replace channels.discord.token with {"source": "env", "provider": "default", "id": "DISCORD_BOT_TOKEN"}, then re-run setup --adopt discord-channel. Clawake never reads, moves or deletes token values.
Error: Cannot adopt channels/discord of member engineer: the live token in <tmp>/engineer/state/openclaw.json is a plaintext string. Discord is disabled for this member: remove channels.discord.token (or replace it with an env SecretRef), then re-run setup --adopt discord-channel. Clawake never reads, moves or deletes token values.
```

**With the flag, Lead SecretRef and engineer token removed** (exit 0, nothing written):
```
 - lead [product_owner] runtime: write <tmp>/lead/state/openclaw.json
   changed keys: agents, channels/a2a, channels/discord/allowFrom (added), channels/discord/applicationId (added), channels/discord/dmPolicy (added), channels/discord/guilds/<id>/channels/<id>/requireMention (added), channels/discord/guilds/<id>/channels/<id>/users (added), channels/discord/guilds/<id>/requireMention (added), channels/discord/guilds/<id>/roles (added), gateway, plugins
 - engineer [developer] runtime: write <tmp>/engineer/state/openclaw.json
   changed keys: agents, channels/a2a, channels/discord/groupPolicy (removed), channels/discord/guilds (removed), gateway, plugins
Adoptions:
 - lead: adopt discord-channel (backup first: .backups/lead-<UTC timestamp>.tar.gz)
 - engineer: adopt discord-channel (backup first: .backups/engineer-<UTC timestamp>.tar.gz)
```
(`agents`, `channels/a2a`, `gateway` and `plugins` appear only because the synthetic
state is fresh.) With `--execute`, `Created runtime backup for <member>: …` is printed
before the first `Deployed …`. The next preview prints `0/2 member(s) changed` and no
`Adoptions:` block.

## Exact error texts

- **Partial reference:**
  `instances[lead].plugins[example-cad].config.channel: '${site:' is only allowed as a whole-string reference '${site:<name>}'; partial references are not substituted`
- **Unknown site name:**
  `instances[lead].plugins[example-cad].config.approvers: unknown site value 'discord.approver' (not defined in site values file <path>)`
- **Missing allowlist** (pydantic text plus a names-only hint):
  ```
  1 validation error for Inventory
  instances.0.openclaw.discord
    Value error, discord.guilds[0].channels[0]: group_policy allowlist requires non-empty users or roles on the channel or its guild (no channel open to the whole guild) [type=value_error]
  Involved site values (names only): site value 'discord.guild' at instances[lead].openclaw.discord.guilds[0].id; ...
  ```
- **String token:** see the two `Cannot adopt …` lines above.

## Behaviour changes for operators

- `setup` and `setup-quadlets` have a new option `--adopt <name>`, which can be repeated.
  The only name is `discord-channel`. An unknown name exits 2, and so does `--adopt`
  with a v2 inventory.
- `setup` exits 2 before planning if an enabled `discord.token_env` is not a key in any of
  the member's env files. The message names the env file paths and any missing files.
- Planning errors in `setup` (drift guard, refused unmanaged field, adoption precondition)
  now print `Error: <message>` and exit 1. Before, they raised an uncaught `ValueError`;
  the exit code was also 1.
- `setup --execute` with a pending adoption takes a `backup_instance` archive into
  `.backups/` (relative to the working directory) before any write, regardless of
  `backup_policy`. It does not prune `.backups/`.
- Key paths below `channels/discord` are printed redacted, with an
  `(added)`/`(removed)`/`(changed)` marker. All other keys print as before.
- `validate` prints `site values: <path> (N values, M references)` and
  `WARNING: site values file <path>: unused values: …` (names only).
- The overlay and site-file JSON errors no longer chain the `JSONDecodeError`, so its
  `.doc` with the file content stays out of tracebacks. The message text is unchanged.
- Inventories without `discord` and without `site_values` behave exactly as before.

## Live E2E request

The operator session must first do step 1 of the manual walkthrough by hand (token to the
env file and SecretRef; on the disabled member, remove the token). It must also write the
site file. Then, read-only:

```
clawake validate -c deploy/team.json            # expect: site values: <path> (N values, M references); no unused warning
clawake setup -c deploy/team.json               # expect exit 1: Refusing to overwrite unmanaged OpenClaw field: channels/discord (named adoption 'discord-channel' ...)
clawake setup -c deploy/team.json --adopt discord-channel   # expect: redacted channels/discord key paths, Adoptions: block for both members, no IDs in output
clawake rollout -c deploy/team.json             # expect exit 1 with the same Refusing ... error
```

Then mutating:

```
clawake setup -c deploy/team.json --adopt discord-channel --execute
clawake status -c deploy/team.json -m <enabled member> --wait   # with readiness.channels: true on that member; expect ready
clawake setup -c deploy/team.json --adopt discord-channel       # expect 0/2 member(s) changed, no Adoptions: block
clawake setup -c deploy/team.json                                # expect no-op
```

Expected result:
- Both `openclaw.json` files contain the rendered `channels.discord` and no string token.
- The ledgers contain `channels/discord`.
- One backup per member is in `.backups/`.
- The enabled member's Discord account is `ready`.

Please also check the open ADR question: after a pairing approval and a gateway restart,
`channels.discord` must still be byte-identical (re-run the `setup` preview → no-op).

## Open points and risks

- **Ordering of the token step.** Because of the `token_env` check, the env key must
  exist before even the preview works. That is intended (fail closed).
- **OpenClaw facts (docs.openclaw.ai, fetched 2026-10-19).**
  - `/channels/discord/access-control` confirms `dmPolicy`, `allowFrom`, `groupPolicy`,
    `guilds.<id>.{requireMention,users,roles,channels}` and per-channel
    `enabled`/`requireMention`/`users`. It also says a guild without `channels` allows
    all its channels; the extra guild rule above is based on that.
  - `/channels/discord/setup` confirms the env SecretRef shape and that a config token
    wins over `DISCORD_BOT_TOKEN`.
  - **Unverified:** per-channel `roles` (rendered as ADR-005 specifies, but not listed
    among the per-channel keys on the fetched page); top-level
    `channels.discord.applicationId` (the docs show it in the per-account example); and
    whether a disabled account makes `readiness.channels` report `degraded`. The manual
    therefore advises enabling `readiness.channels` only on enabled members.
- **Withdrawing a block.** Removing `discord` from the inventory after adoption deletes
  the owned `channels.discord` object, as for every owned field under ADR-001. The
  operator should know this before withdrawing a block.
- **Duplicate IDs.** Duplicate IDs inside `users`/`roles`/`allow_from` are rejected. This
  is stricter than the ADR text, which only requires unique guild and channel IDs.
- **Backups.** The adoption backup ignores `backup_policy.enabled`. ADR-005 requires a
  backup, so this is deliberate.
- **Proposed architecture.md text**, to replace the sentence "Unmanaged models, channels
  and settings survive reconciliation" in *Shared networks and leadership teams*:
  > Unmanaged models, channels and settings survive reconciliation. A member that declares
  > `openclaw.discord` makes `channels.discord` one Clawake-owned field, rendered from the
  > inventory and private site values (ADR-005). An existing hand-written block is taken
  > over only by `setup --adopt discord-channel`, which refuses plaintext tokens and backs
  > up the member first. Any later change made outside Clawake trips the drift guard.

  The matching `Architecture-Ref` comment is in
  `src/clawake/services/runtime_config.py` (`desired_fields`).

## Out-of-scope findings

- `docs/ADR.md:304-305`: the ADR-005 example combines `dm_policy: disabled` with
  `allow_from`. Its own rule forbids that combination. The example in
  `docs/inventory-v2.md` uses `dm_policy: allowlist` instead. ADR not changed.
- ADR-005 does not mention YAML quoting: references inside YAML flow lists must be quoted
  (`["${site:x}"]`), because unquoted `[${site:x}]` is a YAML parse error. This is now
  documented in `docs/inventory-v2.md`.
- `tests/test_cli.py:250`: `test_setup_quadlets_dry_run_succeeds` does not patch
  `cli.default_probes`, so it may probe 127.0.0.1. Pre-existing; not changed.

## Follow-up 2026-10-19

Prompt: `docs/prompts/2026-10-19-discord-channel-followup.md`, base `8a5808f`
(ADR-005 amendment, items 7 and 8). Implementation commit `28fab84`.

### Summary

- `token_env` now matches the OpenClaw env SecretRef id pattern
  `^[A-Z][A-Z0-9_]{0,127}$`. It uses its own constant `DISCORD_TOKEN_ENV_PATTERN` in
  `src/clawake/config.py`, with a comment that references the schema. The shared
  `ENV_NAME_PATTERN` and the A2A `inbound_token_env` / `outbound_token_env` fields are
  unchanged.
- `DiscordSpec.runtime_config()` always renders `"configWrites": false` for an enabled
  member, after `groupPolicy` and before `guilds`. It cannot be set in the inventory:
  `config_writes` / `configWrites` are unknown keys, and the model already forbids those
  (`extra="forbid"`). A disabled member still renders only `{"enabled": false}`.
- Docs: `docs/inventory-v2.md` (the `token_env` pattern, the unknown-key list, the
  rendered key list and a `configWrites` rule) and `docs/manual.md` (the `token_env`
  pattern, `configWrites: false`, and the one-time `configWrites (added)` preview line plus
  restart for an already-owned member).
- OpenClaw fact checked: https://docs.openclaw.ai/channels/discord/events, section
  "Config writes": "Channel-initiated config writes are enabled by default". It covers
  `/config set|unset` (when command features are enabled), and setting
  `channels.discord.configWrites: false` disables them. The schema facts
  (`additionalProperties: false`, the SecretRef id pattern) come from the architect's
  reading of `openclaw config schema`. I did not verify them myself, because there is no
  OpenClaw in the devcontainer.

### Changed files (vs. 8a5808f)

```
 docs/inventory-v2.md                               | 13 +++++++----
 docs/manual.md                                     |  7 ++++++
 src/clawake/config.py                              |  9 +++++++-
 .../golden/lead.channels-discord.json              |  1 +
 tests/test_discord_channel.py                      | 27 +++++++++++++++++++++-
```

### Checks

| Command | Result |
|---|---|
| `uv sync --locked` | pass |
| `uv run ruff check .` | pass (`All checks passed!`) |
| `uv run ruff format --check .` | pass (`51 files already formatted`) |
| `uv run pytest -q` | pass (590 passed, 2 skipped) |
| `uv lock --check` | pass |

No new `# noqa` or `# type: ignore`.

### New tests and tests that failed first

All in `tests/test_discord_channel.py`. I ran them before the implementation:
`5 failed, 74 passed`.

| Test | Before fix |
|---|---|
| `test_discord_model_rejects[token_env_leading_underscore]` (`_DISCORD_BOT_TOKEN`) | failed |
| `test_discord_model_rejects[token_env_129_chars]` (`"D" * 129`) | failed |
| `test_enabled_render_always_disables_config_writes` (new) | failed (`KeyError: 'configWrites'`) |
| `test_rendering_is_deterministic_and_uses_openclaw_names` (key order now includes `configWrites`) | failed |
| `test_rendering_omits_empty_lists_and_unset_optionals` (expects `"configWrites": False`) | failed |
| `test_discord_model_rejects[token_env_leading_digit]` (`1DISCORD`) | passed (the old pattern already rejected it); regression guard |
| `test_discord_model_rejects[config_writes_snake]`, `[config_writes_camel]`, `[disabled_config_writes]` | passed (`extra="forbid"` already rejected them); regression guards |
| `test_discord_model_accepts[token_env_128_chars]`, `[token_env_single_letter]` | passed; boundary guards |

Changed expectations:
- Golden `tests/fixtures/discord-channel/golden/lead.channels-discord.json`: adds
  `"configWrites": false`. `engineer.channels-discord.json` is unchanged
  (`{"enabled": false}`).
- `test_adoption_preview_redacts_ids_and_mutates_nothing`: the adoption preview now also
  lists `channels/discord/configWrites (added)`. The section "Setup preview for the
  synthetic adoption" above shows the state before this follow-up.

### Mutation check

I ran this in a scratch copy, with `PYTHONPATH` pointing at the mutated `src`. Dropping
the line `config["configWrites"] = False` made 5 tests fail:
`test_rendered_discord_objects_match_golden`,
`test_rendering_is_deterministic_and_uses_openclaw_names`,
`test_rendering_omits_empty_lists_and_unset_optionals`,
`test_enabled_render_always_disables_config_writes` and
`test_adoption_preview_redacts_ids_and_mutates_nothing`.

### Updated golden render of the synthetic Lead

`tests/fixtures/discord-channel/golden/lead.channels-discord.json`:

```json
{
  "enabled": true,
  "token": {
    "source": "env",
    "provider": "default",
    "id": "DISCORD_BOT_TOKEN"
  },
  "applicationId": "100000000000000010",
  "dmPolicy": "allowlist",
  "allowFrom": [
    "100000000000000003",
    "100000000000000004"
  ],
  "groupPolicy": "allowlist",
  "configWrites": false,
  "guilds": {
    "100000000000000001": {
      "requireMention": true,
      "roles": [
        "100000000000000005"
      ],
      "channels": {
        "100000000000000002": {
          "enabled": true,
          "requireMention": true,
          "users": [
            "100000000000000003",
            "100000000000000004"
          ]
        }
      }
    }
  }
}
```

### Acceptance criteria

- New tests failed first; mutation check finds a dropped `configWrites`: **met** (see
  above).
- All five mandatory checks green: **met**.

### Behaviour changes for operators

- `validate` / `setup` reject a `token_env` with a leading underscore or more than 128
  characters. The error names the `token_env` field.
- The rendered `channels.discord` of an enabled member gains `configWrites: false`. A
  member whose `channels/discord` is already owned shows
  `channels/discord/configWrites (added)` in its next `setup` preview and is restarted
  once.

### Live E2E request (addition)

`e2e-request: required` still applies, because the rendered OpenClaw config changed. Run
the P4 E2E request above, plus:
- In the adoption preview
  (`clawake setup -c deploy/team.yml --adopt discord-channel`), expect
  `channels/discord/configWrites (added)` for each enabled member.
- After `--execute`, OpenClaw accepts the config. Check that the gateway starts without a
  config schema error (I did not verify that a dedicated validate subcommand exists), and that
  `status --wait` reports `ready`.
- Optional: in the allowed lead channel, `/config set` from Discord is refused.

### Open points and risks

- The schema facts are unverified in the devcontainer (no OpenClaw). The live E2E above
  is the verification.
- A member that is already owned restarts once to pick up `configWrites: false`.
