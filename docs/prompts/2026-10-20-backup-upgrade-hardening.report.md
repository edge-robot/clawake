---
prompt: docs/prompts/2026-10-20-backup-upgrade-hardening.md
branch: fix/28-backup-and-upgrade-hardening
commits: d5fd424..010db20 (plus the commit adding this report)
status: done
e2e-request: recommended
---
# Report: P5 backup and upgrade hardening (#28, #29, #30, #31) and stale drift markers

## Summary

The four defects from the P3b review are fixed, each in its own commit with regression
tests written first:

- **#31:** `_plan_from_image` refuses a tag change without a digest. `build_upgrade_plan`,
  `apply_upgrade` and `plan_overlay_upgrade` now fail closed for every caller. The CLI
  message is unchanged.
- **#30:** both YAML write paths of `_update_image_fields` (flow and block style) go
  through `_write_text_atomic`. The output bytes and the file mode are unchanged.
- **#29:** `backup_config` and `backup_instance` use sub-second UTC stamps
  (`%Y%m%dT%H%M%S.%fZ`, the same as the pins backups) and create their files exclusively.
  A collision fails closed. Config backups are mode 0600.
- **#28:** `prune_backups` only deletes regular files named exactly
  `<name>-<stamp><suffix>`, with an old or new stamp. Member `a` no longer prunes
  `a-b`, and files that do not parse are never deleted.

The four stale `Architecture-Ref` drift comments are removed in a separate commit
(comments only). `docs/manual.md` now describes backup names, collisions and pruning.

Commits (base `5d0bf09`):

| sha | purpose |
|---|---|
| d5fd424 | #31 library guard |
| 752f5c3 | #30 atomic YAML writes |
| ace63d6 | #29 exclusive sub-second backups |
| f7f6986 | #28 strict prune matching |
| e926054 | drift markers (#24) |
| 010db20 | manual |
| (this commit) | prompt and report |

The issues on GitHub were not readable in this container: there are no GitHub
credentials. The binding constraints in the prompt served as the specification.

## Changed files

`git diff --stat 5d0bf09..010db20`:

```
 docs/manual.md                         |  22 ++++-
 src/clawake/cli.py                     |  12 +--
 src/clawake/services/backup.py         | 100 ++++++++++++++++---
 src/clawake/services/rollout.py        |   3 -
 src/clawake/services/runtime_config.py |   3 -
 src/clawake/services/upgrade.py        |  23 +++--
 tests/test_backup.py                   | 157 +++++++++++++++++++++++++++++-
 tests/test_cli.py                      |  18 ++++
 tests/test_rollout.py                  |   2 +-
 tests/test_runtime_pins.py             |  25 ++++-
 tests/test_upgrade.py                  | 172 ++++++++++++++++++++++++++++++++-
 11 files changed, 497 insertions(+), 40 deletions(-)
```

- `src/clawake/services/backup.py`: stamp helper, exclusive 0600 file writer, `x:gz`
  archives, strict `prune_backups(output_dir, name, retention, *, suffix)`.
- `src/clawake/services/upgrade.py`: #31 guard, atomic YAML writes, `backup_config` uses
  the exclusive writer (optional `now` clock).
- `src/clawake/cli.py`: `upgrade` passes name and suffix to `prune_backups`; two drift
  comments removed.
- `src/clawake/services/rollout.py`, `src/clawake/services/runtime_config.py`: drift
  comments removed.
- `docs/manual.md`: backup names, collision behaviour and pruning in the `upgrade` section;
  the rollout failure example uses the new stamp.
- `tests/test_upgrade.py`, `tests/test_backup.py`, `tests/test_runtime_pins.py`,
  `tests/test_cli.py`: new regression tests (listed below).
- `tests/test_rollout.py`: the backup-name regex expects the new stamp.

## Checks

| command | result |
|---|---|
| `uv sync --locked` | pass (`Checked 21 packages`) |
| `uv run ruff check .` | pass (`All checks passed!`) |
| `uv run ruff format --check .` | pass (`52 files already formatted`) |
| `uv run pytest -q` | pass: **612 passed, 2 skipped** (base: 595 passed, 2 skipped) |
| `uv lock --check` | pass (`Resolved 29 packages`) |

New `# noqa` / `# type: ignore`: none.

## Per issue

Mutation checks ran against scratch copies of HEAD (`git archive` + `PYTHONPATH`;
`clawake.__file__` confirmed the scratch tree) with
`tests/test_backup.py tests/test_upgrade.py tests/test_runtime_pins.py tests/test_cli.py`.

### #31: a tag change without a digest fails closed in the library

- Tests:
  - `test_upgrade.py::test_build_upgrade_plan_refuses_tag_change_without_digest`
  - `test_upgrade.py::test_apply_upgrade_refuses_tag_change_without_digest_and_writes_nothing`
  - `test_upgrade.py::test_same_tag_without_digest_keeps_the_current_digest`
  - `test_runtime_pins.py::test_overlay_upgrade_refuses_tag_change_without_digest`
    (`plan_overlay_upgrade`, `build_upgrade_plan` and `apply_upgrade` with an overlay;
    inventory and overlay unchanged, no pins backup)
- CLI message: `test_cli.py::test_upgrade_rejects_unpinned_new_tag` is unchanged and still
  passes ("immutable --digest is required", exit 2).
- Rollback: the existing rollback tests in `test_upgrade.py` and `test_runtime_pins.py`
  pass unchanged.
- Before: the three refusal tests failed (`DID NOT RAISE`). After: pass.
- Mutation (`if False:` instead of the guard): the 3 refusal tests fail.
- **Changed expectations:** `test_apply_upgrade_never_writes_literal_none_in_yaml` and
  `test_apply_upgrade_never_writes_null_in_json` (from #27) changed the tag *and* passed no
  digest, which the guard now forbids. They now keep the tag (`"old"` / `"2026.9.9"`) and
  pass no digest. That is the remaining library path where the planned digest is `None`, so
  the tests still prove that no `None`/`null` is written.

### #30: YAML inventory writes are atomic

- Tests:
  - `test_upgrade.py::test_interrupted_yaml_replace_leaves_the_original_inventory[block|flow]`:
    `os.replace` raises; the original bytes stay and no temporary file is left.
  - `test_upgrade.py::test_flow_style_yaml_upgrade_output_is_unchanged`: flow-style
    snapshot plus file mode 0640. It passed on the old code before the change, so it pins
    today's bytes.
- Byte identity: the existing YAML tests pass unchanged
  (`test_apply_upgrade_preserves_unrelated_yaml_formatting`,
  `test_runtime_pins.py::test_without_runtime_pins_upgrade_writes_inventory_byte_for_byte_as_before`
  and the rollback golden tests).
- Before: both interruption cases failed. After: pass.
- Mutation (both YAML paths back to `write_text`): both interruption cases fail.

### #29: backups never overwrite each other

- Tests:
  - `test_upgrade.py::test_two_config_backups_in_the_same_second_produce_two_files`
  - `test_upgrade.py::test_config_backup_fails_closed_when_the_target_exists`
  - `test_upgrade.py::test_config_backup_is_private` (0600, name format)
  - `test_backup.py::test_two_runtime_backups_in_the_same_second_produce_two_archives`
  - `test_backup.py::test_runtime_backup_fails_closed_when_the_archive_exists`
- Before: all five failed. After: pass.
- Mutation (`O_EXCL` → `O_TRUNC` and `x:gz` → `w:gz`): both fail-closed tests fail.
  Without exclusive creation the earlier backup is overwritten.
- **Changed expectation:** `test_rollout.py::test_readiness_failure_after_restart_stops_without_commit`.
  The regex `alpha-\d{8}T\d{6}Z\.tar\.gz` became `alpha-\d{8}T\d{6}\.\d{6}Z\.tar\.gz`.
- New name examples:
  - runtime archive: `.backups/example-lead-20261020T093015.123456Z.tar.gz`
  - inventory copy: `.backups/team-20261020T093015.123987Z.json` (mode 0600)
  - unchanged: `pins-backups/20261020T093015.123456Z.json`,
    `.backups/pins-20261020T093015.123456Z.json`
- On a collision there is no `-N` retry (unlike the pins helpers). The prompt asks to fail
  closed. The error is `Backup <path> already exists; refusing to overwrite it`
  (`FileExistsError`, so every caller's existing `OSError` handling applies).

### #28: pruning selects exactly one member's or config's backups

- API: `prune_backups(output_dir, name, retention, *, suffix)`. `upgrade` calls it with
  `(instance.name, suffix=".tar.gz")` and `(config.stem, suffix=config.suffix)`.
- Pattern: `<name>-(\d{8}T\d{6}(\.\d{6})?Z)<suffix>`, full match, and the stamp must parse
  as a date. Symlinks and non-regular files are skipped. Ordering is by the time in the
  stamp (was mtime), so old and new names sort correctly against each other. A missing
  directory removes nothing.
- Tests:
  - `test_backup.py::test_pruning_member_a_keeps_every_backup_of_member_a_b`
  - `test_backup.py::test_pruning_handles_second_and_sub_second_names_by_their_timestamp`
  - `test_backup.py::test_pruning_never_deletes_files_that_do_not_parse` (`latest`,
    `.partial`, 3-digit fraction, `-1` suffix, month 13, `.tgz`, foreign prefix, symlink)
  - `test_backup.py::test_pruning_a_config_keeps_member_archives_and_other_configs`
  - `test_backup.py::test_pruning_a_missing_directory_removes_nothing`
  - `test_cli.py::test_upgrade_execute_runs_backup_migration_and_health_checks` (extended):
    seeds 6 old backups each for `one`, `one-b`, `inventory`, `inventory-b`. After
    `upgrade --execute` with retention 5, every `one-b`/`inventory-b` file is kept, and
    exactly the 4 newest seeded `one`/`inventory` files plus the 2 new backups remain.
- **Changed expectation:** `test_backup.py::test_prune_backups_retains_newest_matching_files`
  used names like `one-0.tar.gz`. These no longer parse and are now kept by design, so the
  test uses real stamp names and the new signature.
- Before: all six failed (new signature / `one-b` pruned). After: pass.
- Mutation (prefix glob `f"{name}-*"` with mtime ordering): the `a`/`a-b` test, the
  unparseable test, the config test and the CLI test fail.

### Drift markers

All four comment blocks are removed with their `Architecture-Ref` lines: two in `cli.py`
(setup, status), one in `rollout.py` and one in `runtime_config.py`. The substantive
comments next to them stay ("Restart only changed members…", "ADR-005 item 4: one owned
field…", "ADR-004: pins are committed only…"). The commit only deletes 10 comment lines.
No `Architecture-Ref` remains in `src/`. `docs/architecture.md` lines 43–70 and 115–124
now describe the behaviour.

## Acceptance criteria

- Every new test failed first, and the mutation checks hold: **met** (see per issue; each
  of the four mutations fails the named test).
- `uv sync --locked`, `ruff check`, `ruff format --check`, `pytest -q`, `uv lock --check`
  green: **met**.
- No deployment data in the diff: **met**. Only synthetic names and digests. The single
  `deploy/team.json` match in the diff is pre-existing context in `docs/manual.md`.
- One commit per issue, drift markers separately with `Refs: edge-robot/clawake#24`:
  **met**.
- Manual updated: **met**.

## Behaviour changes for operators

- Backup file names in `.backups/` now have microseconds:
  `<member>-YYYYMMDDTHHMMSS.ffffffZ.tar.gz` and
  `<inventory stem>-YYYYMMDDTHHMMSS.ffffffZ<suffix>`. This affects `upgrade`, `rollout` and
  `setup --adopt`.
- Inventory copies made by `upgrade` are mode 0600 (before: umask default, usually 0644).
- A backup whose target name already exists fails with
  `Backup <path> already exists; refusing to overwrite it`. `upgrade` then takes its
  existing "config not changed" failure path and restarts the previous image. `rollout`
  and `setup --adopt` abort before deploying, as with any backup failure.
- `upgrade` pruning no longer deletes backups of members whose name starts with
  `<member>-`, or files that do not match the strict name. Existing second-resolution
  backups are still recognised and pruned by their timestamp.
- The library refuses a tag change without a digest. No CLI-visible change: the CLI
  already refused it with the same message and exit code 2.
- YAML inventory writes during `upgrade`/rollback are atomic. The bytes are unchanged.
- No new flags, no changed defaults, no changed preview output.

## Live E2E request

Recommended, not required. `upgrade`'s `--execute` steps and the preview are unchanged
apart from backup naming, file mode and prune selection. The architect checks the names
at the next live run.

1. At the next live `upgrade` (preview first, then `--execute` with `--digest`) or
   `rollout --execute`:
   - `ls -l .backups/` shows the new archive as
     `<member>-YYYYMMDDTHHMMSS.ffffffZ.tar.gz`, and after `upgrade` the inventory copy as
     `<stem>-YYYYMMDDTHHMMSS.ffffffZ.json` with mode `-rw-------`.
   - After a successful `upgrade`, older backups of that member beyond
     `backup_policy.retention` are gone. Backups of any member whose name starts with
     `<member>-`, and the rollout overlay copies `pins-*.json`, are untouched (unless the
     inventory stem is `pins`; see risks).
2. Optional: `clawake upgrade -c deploy/team.json -m <member> --to <new-tag>` without
   `--digest` still exits 2 with "An immutable --digest is required when changing tags".

## Open points and risks

- **Collision without retry.** Following the prompt, a name collision fails closed
  instead of picking `-1`. With microsecond stamps this needs two backups of the same name
  in the same microsecond, or a clock step backwards.
- **Overlay copies in `.backups/`.** `rollout` writes `pins-<stamp>.json` there. If an
  inventory file were itself named `pins.json`, `upgrade` would treat these copies as its
  config backups and prune them. The deployed inventory is `team.json`, so this does not
  happen today.
- **Ordering by stamp.** Pruning now orders by the time in the name, not by mtime. A file
  renamed by hand to a later stamp counts as newer.
- The GitHub issues could not be read in the container. If an issue has more scope than
  the prompt's constraints, it is not covered here.

## Out-of-scope findings

- `src/clawake/services/backup.py:84`: `backup_instance` creates `output_dir` even when
  `execute=False`. No caller in `src/` uses `execute=False` today, but the preview
  contract would require it to create nothing. Not changed.
- `src/clawake/services/backup.py:141`: runtime archives are created with the umask mode
  (usually 0644), though they contain workspace and state data. Only config backups were
  in scope for 0600. Not changed.
- `src/clawake/cli.py:862`: the `setup --adopt` backup only catches `OSError`, while
  `rollout` also catches `tarfile.TarError`. A tar error would surface as a traceback
  instead of "Nothing was changed". Not changed.
