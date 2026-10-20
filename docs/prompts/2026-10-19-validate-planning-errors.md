---
target-repo: edge-robot/clawake
branch: feat/26-discord-channel
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#32
architecture-ref: clawake/docs/ADR.md#adr-005-discord-channel-provisioning-from-the-inventory-and-private-site-values
base-commit: 3ec4c60
status: issued
---
# Fix #32: `validate` must not crash on runtime planning refusals

## Goal

`clawake validate` prints a traceback when runtime planning refuses: a pending ADR-005
adoption, the ADR-001 drift guard, or another refusal to overwrite an unmanaged field.
After the fix:

- a pending named adoption is a **notice**, with exit 0;
- every other planning refusal is a clean `Error: <message>`, with exit 1;
- no traceback is ever printed.

## Context

- **Issue:** edge-robot/clawake#32. It was found live in the operator's adoption preview of
  a deployed team.
- **Code:**
  - `src/clawake/cli.py` `validate_inventory` calls `plan_deployment(inventory, selected,
    _template_root())` without error handling.
  - The setup command already converts planning `ValueError`s to `Error: <message>` with
    exit 1. Reuse that pattern, do not duplicate it.
  - `src/clawake/services/runtime_config.py` has `pending_adoptions(inventory, instance)`.
    It is read-only and returns e.g. `("discord-channel",)`.
  - `plan_deployment(..., adopt=...)` accepts the requested adoptions.
- **ADR-005 item 5:** adoption needs the explicit flag on `setup`. `validate` stays
  read-only and never adopts.

## Binding constraints and prohibitions

1. **Planning with pending adoptions.** For each selected v1 member, `validate` computes
   `pending_adoptions`.
   - It plans with `adopt=` the union of the pending names. That is safe, because
     `validate` never writes. The adoption preconditions still run: a plaintext string token
     stays a clean error with exit 1, and its message still never contains a value.
   - Per member with a pending adoption, print one notice line:
     `pending adoption: <member> discord-channel (review with: clawake setup -c <inventory> --adopt discord-channel)`.
2. **Errors.** Any other `ValueError` raised by planning (drift guard, unmanaged-field
   refusal, adoption precondition) prints `Error: <message>` and exits 1, without a
   traceback.
3. **Output.** The success output is unchanged apart from the notice lines (`Valid
   inventory: …`, `runtime pins: …`, `site values: …`, unused-name warning). Do not print
   IDs or values. The existing redaction rules apply.
4. **Scope.** No change to `setup`, `rollout`, `status`, `pins` or the planning semantics
   themselves. No new dependencies. Synthetic data only. Never run `--execute`, `podman`,
   `systemctl` or `openclaw`.

## Steps

1. Verify the preconditions: the branch is `feat/26-discord-channel`, HEAD is `3ec4c60`
   and the worktree is clean (this prompt file is the only untracked file). Record the base
   test count (590 passed, 2 skipped).
2. Write failing tests first, in `tests/test_discord_channel.py` or a new
   `tests/test_validate.py`:
   - pending adoption → exit 0, the notice line, `site values: …`, and no traceback;
   - pending adoption with a plaintext string token → exit 1 with the `Cannot adopt …`
     message, and the canary token value absent from the output;
   - drift guard after adoption → exit 1, `Error: Managed field changed outside Clawake:
     channels/discord`, no traceback;
   - an inventory without `discord` → output byte-identical to before;
   - `validate` writes nothing (byte snapshot of `tmp_path`).
3. Implement, then update `docs/manual.md` (the `validate` behaviour in the adoption
   walkthrough).
4. Run the checks, commit with `Refs: edge-robot/clawake#32`, and write the report.

## Acceptance criteria and tests

- [ ] The new tests failed first. Mutation checks: remove the error handling → the
      drift-guard test fails; plan without `adopt=` → the pending-adoption test fails.
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are all green.

## Branch and worktree rules

Commit only to `feat/26-discord-channel` in this checkout. No push, no rebase, no new
branches or worktrees. Commit this prompt unchanged with your report. Leave the worktree
clean.

## Expected report / handoff format

`docs/prompts/2026-10-19-validate-planning-errors.report.md` in the agent-definition
format, with:

- the checks table;
- the test names and their before/after results;
- the mutation results;
- the exact new `validate` outputs for the three cases (pending, drift, string token),
  using synthetic data.

Set `e2e-request: recommended`. The architect reruns `validate` against the deployed
team at the next rollout.
