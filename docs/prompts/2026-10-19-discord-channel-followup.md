---
target-repo: edge-robot/clawake
branch: feat/26-discord-channel
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#26
architecture-ref: clawake/docs/ADR.md#adr-005-discord-channel-provisioning-from-the-inventory-and-private-site-values
base-commit: 8a5808f (P4 548aab9 + ADR-005 amendment)
status: issued
---
# P4 follow-up: align the Discord rendering with the OpenClaw config schema

## Goal

Review findings on P4 (`docs/prompts/2026-10-19-discord-channel.report.md`). The
architect read the live OpenClaw config JSON schema (`openclaw config schema`, deployed
OpenClaw 2026.9.9). Two rules have to change.

## Context

- **Schema facts** (architect, 2026-10-19). The schema is strict:
  `additionalProperties: false` on `channels.discord`, on `guilds.<id>` and on
  `guilds.<id>.channels.<id>`, so an unknown key fails config validation.
  - Top-level `channels.discord.applicationId` exists (string).
  - Per-channel `roles` exists.
  - `dmPolicy` is one of `pairing`, `allowlist`, `open`, `disabled`; `groupPolicy` is one
    of `open`, `disabled`, `allowlist`.
  - The env SecretRef is `{source:"env", provider:^[a-z][a-z0-9_-]{0,63}$,
    id:^[A-Z][A-Z0-9_]{0,127}$}`, with no other keys.
  - `channels.discord.configWrites` (boolean) exists. Per docs.openclaw.ai
    `/channels/discord/events` "Config writes": channel-initiated config writes are
    **enabled by default**. `/config set` and `/config unset` from Discord can change the
    OpenClaw config.
- ADR-005 amendment (committed by the architect before this prompt): read the
  "Amendment 2026-10-19" section of ADR-005.

## Binding constraints and prohibitions

1. **`token_env`** must match `^[A-Z][A-Z0-9_]{0,127}$`, the OpenClaw SecretRef id
   pattern. Do not change the shared `ENV_NAME_PATTERN` or the A2A token fields; give
   Discord its own constant, with a comment that references the schema.
2. **`configWrites: false`** is always rendered for an enabled member. It is not
   configurable in the inventory. A disabled member still renders only
   `{"enabled": false}`.
3. Update the golden fixtures and docs (`docs/inventory-v2.md`, `docs/manual.md`) to
   match. No other behaviour changes. The scope limits of the P4 prompt still apply.

## Steps

1. Verify the preconditions: the branch and HEAD match the base, and the worktree is
   clean.
2. Write failing tests first: a token name with a leading underscore and one longer than
   128 characters are rejected; an enabled render contains `configWrites: false`;
   `config_writes` / `configWrites` in the inventory is an unknown key.
3. Implement, update the goldens and docs, and run the checks.
4. Append a section "Follow-up 2026-10-19" to the existing P4 report. Commit with
   `Refs: edge-robot/clawake#26`.

## Acceptance criteria and tests

- [ ] New tests failed first; mutation check: drop `configWrites` from the render → a
      test fails.
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are all green.

## Branch and worktree rules

Commit only to `feat/26-discord-channel` in this checkout. No push, no rebase, no new
branches or worktrees. Commit this prompt unchanged with the report update. Leave the
worktree clean.

## Expected report / handoff format

The "Follow-up 2026-10-19" section in `docs/prompts/2026-10-19-discord-channel.report.md`
contains: the checks table, the new test names, the mutation result, and the updated
golden render of the synthetic Lead.
