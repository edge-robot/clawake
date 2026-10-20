---
target-repo: edge-robot/clawake
branch: fix/28-backup-and-upgrade-hardening
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#34
architecture-ref: clawake/docs/architecture.md#lifecycle-contracts
base-commit: 08fd64c (P5 accepted)
status: issued
---
# P5b: Private runtime backup archives and clean backup failures (#34)

## Goal

Fix the three findings from the P5 report (*Out-of-scope findings*), filed as
edge-robot/clawake#34:

1. **Security.** `backup_instance` creates runtime archives with the umask mode
   (usually 0644), but they contain the member's workspace and OpenClaw state.
2. `backup_instance` creates the output directory even with `execute=False`.
3. `setup --adopt` handles only `OSError` around the member backup. A
   `tarfile.TarError` surfaces as a traceback.

## Context

- `src/clawake/services/backup.py`:
  - `backup_instance` (archive created with `tarfile.open(..., "x:gz")` since #29);
  - `write_new_private_file` and `PRIVATE_FILE_MODE` (0600), which you reuse.
- Callers, all with `execute=True`:
  - `src/clawake/cli.py` `upgrade_member`;
  - `src/clawake/cli.py` `setup_quadlets` (adoption backups);
  - `src/clawake/services/rollout.py`, which already catches `tarfile.TarError`. Use it as
    the reference for item 3.
- The P5 report: `docs/prompts/2026-10-20-backup-upgrade-hardening.report.md`.

## Binding constraints and prohibitions

1. **Archive mode.** The runtime archive is created exclusively with mode 0600 from the
   first byte, never by a chmod after the content is written:
   - open the file with `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)`;
   - hand the file object to `tarfile.open(fileobj=..., mode="w:gz")`;
   - keep the #29 collision error text `Backup <path> already exists; refusing to
     overwrite it`;
   - on any failure while writing, remove the partial archive and re-raise.
2. **Backup directory.** When Clawake creates the backup directory, it creates it with
   mode 0700. An existing directory's mode is left unchanged, but when it is group- or
   world-accessible, Clawake prints a one-line warning to stderr (path only).
3. **Preview.** `backup_instance(..., execute=False)` creates nothing: no directory and
   no file. It still returns the planned path.
4. **Adoption backup errors.** Around the adoption backup, `setup` catches
   `tarfile.TarError` the same way as `OSError`: a clean error, exit 1, nothing deployed.
   Match the `rollout` wording if one exists.
5. **Scope.** No change to backup contents, names (#29) or pruning (#28). No new
   dependencies. Synthetic data only. Never run `--execute`, `podman`, `systemctl` or
   `openclaw`.

## Steps

1. Verify the preconditions: the branch is `fix/28-backup-and-upgrade-hardening`, HEAD is
   `08fd64c`, and the worktree is clean apart from this prompt. Record the base test count
   (612 passed, 2 skipped).
2. Write failing tests first:
   - the archive mode is 0600 with a permissive umask (`os.umask(0o022)` in the test);
   - a new backup directory is 0700, and a pre-existing 0755 directory triggers the
     warning;
   - a preview creates no directory;
   - an injected `TarError` during the adoption backup gives a clean error, exit 1, no
     traceback, and no deploy or runtime write;
   - a failure while writing the archive leaves no partial file;
   - the existing #29 collision test still passes.
3. Implement. Update `docs/manual.md` where backups are described (mode, directory
   warning).
4. Run the checks. Commit with `Refs: edge-robot/clawake#34`. Append a section
   "P5b 2026-10-20" to the existing P5 report.

## Acceptance criteria and tests

- [ ] The new tests failed first. Mutation checks:
  - create the archive with `tarfile.open(path, "x:gz")` again → the mode test fails;
  - remove the `TarError` catch → the adoption test fails.
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are all green.

## Branch and worktree rules

Commit only to `fix/28-backup-and-upgrade-hardening` in this checkout. No push, no rebase,
no other branches or worktrees. Commit this prompt unchanged together with the report
update. Leave the worktree clean.

## Expected report / handoff format

The section "P5b 2026-10-20" in `docs/prompts/2026-10-20-backup-upgrade-hardening.report.md`
contains:

- the checks table;
- the tests, with their results before and after the fix;
- the mutation results;
- the exact warning and error texts;
- the operator-visible changes.

Keep `e2e-request: recommended`. The architect checks `ls -l .backups/` at the next live
run.
