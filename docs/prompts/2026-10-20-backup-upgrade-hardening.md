---
target-repo: edge-robot/clawake
branch: fix/28-backup-and-upgrade-hardening
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#28 (also #29, #30, #31)
architecture-ref: clawake/docs/architecture.md#lifecycle-contracts
base-commit: 5d0bf09 (main after PR #33)
status: issued
---
# P5: Backup and upgrade hardening (#28, #29, #30, #31) and stale drift markers

## Goal

Fix four small, independent defects found in the P3b review, and remove the
`Architecture-Ref` drift comments that `docs/architecture.md` (synced in 4d5d76f) has
made obsolete. Each issue gets its own commit and its own regression tests.

## Context

- **Issues** (read each one; they are the specification):
  - edge-robot/clawake#28: `prune_backups` (`services/backup.py`) matches files by the
    prefix `f"{name}-"`. Member `a` therefore also prunes backups of member `a-b`.
    Inventory-config backups (`<stem>-<stamp>.<suffix>`) share the directory and can
    collide the same way. Callers are in `cli.py` (`upgrade`, after success).
  - edge-robot/clawake#29: `backup_config` (`services/upgrade.py`) uses one-second
    timestamps and `write_text`. Two backups in the same second overwrite each other.
  - edge-robot/clawake#30: the YAML paths of `_update_image_fields` (flow and block style)
    use `Path.write_text`. JSON writes already use `_write_text_atomic` (#27).
  - edge-robot/clawake#31: read the architect's correction comment. The CLI already
    refuses a tag change without `--digest`. The remaining scope is a library-level
    guard in `_plan_from_image`, so that every caller of `build_upgrade_plan`,
    `apply_upgrade` and `plan_overlay_upgrade` fails closed.
- **Backup naming today:**
  - `backup_instance` writes `<member>-<UTC %Y%m%dT%H%M%SZ>.tar.gz`;
  - `backup_config` writes `<stem>-<stamp><suffix>`;
  - `runtime_pins` backups already use sub-second stamps in `pins-backups/`. Use them as
    the reference.
- **Stale drift markers.** These four comments say "the doc still says …". Since
  4d5d76f the doc says what the code does:
  - `src/clawake/cli.py` (setup, "The doc still says setup restarts unchanged members…");
  - `src/clawake/cli.py` (status, "The doc still says status represents systemd state
    only…");
  - `src/clawake/services/rollout.py` ("The doc says plugin installation remains an
    explicit `sync-plugins` action…");
  - `src/clawake/services/runtime_config.py` ("The doc says unmanaged channels survive
    reconciliation…").

## Binding constraints and prohibitions

1. **#28.** Pruning selects exactly one member's or config's backups.
   - Match the full name `<name>-<stamp>.<ext>` against a strict stamp pattern that
     accepts the existing second-resolution names **and** the new sub-second names from
     #29.
   - Never delete a file that does not parse.
   - Keep the directory layout. No migration of existing backups.
2. **#29.** Backups never overwrite each other.
   - Use sub-second UTC stamps in `backup_config` and `backup_instance`, in the same
     format as the pins backups.
   - Create files exclusively (`"x"` mode or `O_EXCL`), and fail closed with a clear error
     on a collision.
   - Mode 0600 for config backups: the inventory may reference private paths.
3. **#30.** Every inventory write in `services/upgrade.py` goes through
   `_write_text_atomic`. YAML formatting stays byte-identical to today; the existing YAML
   tests prove that. Add one test in which the replace step fails and the original file
   stays intact.
4. **#31.** `_plan_from_image` raises `ValueError` when `next_tag` differs from the
   current tag and `next_digest` is `None`.
   - The CLI message and behaviour stay unchanged, because the CLI checks first.
   - Rollback paths that pass both tag and digest are unaffected.
5. **Drift markers.** Delete the four stale comment blocks, including their
   `Architecture-Ref` lines. Keep the substantive comments next to them (e.g. "ADR-005
   item 4: one owned field…", "Restart only changed members…"). No other code change in
   that commit.
6. **Scope.** No behaviour change beyond the four issues. No new dependencies. Synthetic
   data only. Never run `--execute`, `podman`, `systemctl` or `openclaw`.

## Steps

1. Verify the preconditions: the branch is `fix/28-backup-and-upgrade-hardening`, HEAD is
   `5d0bf09`, and the worktree is clean apart from this prompt. Record the base test count
   (595 passed, 2 skipped expected).
2. For each issue, in the order #31, #30, #29, #28 (#28 depends on the #29 stamp format):
   - write failing tests first;
   - implement;
   - commit with `Refs: edge-robot/clawake#<n>`.

   Required tests:
   - #28: members `a` and `a-b`; prune `a` keeps every `a-b` backup. Old second-resolution
     names and new sub-second names are both pruned correctly. Unparseable files are kept.
   - #29: two backups in the same second produce two files; a pre-existing target fails
     closed; the config backup is 0600.
   - #30: YAML byte-identity (existing tests); an interrupted replace leaves the original.
   - #31: a service-level tag change without a digest raises; the CLI message is
     unchanged.
3. Remove the drift markers in a separate commit (`Refs: edge-robot/clawake#24`).
4. Update `docs/manual.md` where backup names are described, if they are.
5. Run the checks and write the report.

## Acceptance criteria and tests

- [ ] Every new test failed first. Mutation checks:
  - #28 revert to the prefix glob → the `a`/`a-b` test fails;
  - #29 drop exclusive creation → the collision test fails;
  - #30 revert the YAML write to `write_text` → the interruption test fails;
  - #31 drop the guard → the service test fails.
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are all green.
- [ ] No deployment data in the diff.

## Branch and worktree rules

Commit only to `fix/28-backup-and-upgrade-hardening` in this checkout. No push, no rebase,
no other branches or worktrees. Commit this prompt unchanged together with your report.
Leave the worktree clean.

## Expected report / handoff format

`docs/prompts/2026-10-20-backup-upgrade-hardening.report.md` in the agent-definition
format, with:

- one section per issue: the tests, before/after results, and the mutation result;
- the new backup file name examples;
- the operator-visible changes.

Set `e2e-request: recommended`. The architect checks the backup names at the next live
`upgrade` or `rollout`.
