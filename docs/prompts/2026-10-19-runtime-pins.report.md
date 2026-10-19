---
prompt: docs/prompts/2026-10-19-runtime-pins.md
branch: feat/20-runtime-pins
commits: d5bc09a..fbd915c (implementation and docs; prompt and report follow in the next commit)
status: done
e2e-request: required
---
# Report: P3a runtime pin overlay (ADR-004)

## Summary

A v1 inventory can now declare `runtime_pins: <path>`. The path points to a private JSON
overlay with image tag/digest pins and archive plugin pins. `load_inventory()` expands
`${…}`, resolves the reference against the inventory directory, checks the file's mode,
validates the overlay against an allowlist, and merges it into the raw data **before**
`Inventory.model_validate`. Conflicting values fail closed and the error names both files;
equal values are allowed. `upgrade`/rollback write image fields to the overlay (atomic
write, 0600, `pins-backups/` with the newest 20 kept) and leave the inventory
byte-identical. Without `runtime_pins`, the legacy inventory edit is unchanged. A test
asserts the exact bytes, and the same assertion passes against base 92ed97b.

New pieces:
- library API for P3b: `set_plugin_pin()` and `read_pins()`;
- read-only `clawake pins show`;
- preview-first `clawake pins init` migration;
- a `runtime pins:` line in `validate`.

`rollout` (P3b) is not implemented. `setup` and `sync-plugins` code is not changed; they
see the effective data through the shared loader.

Preconditions: branch `feat/20-runtime-pins`, HEAD `92ed97b`. Base checks: 392 passed,
2 skipped. The sha256 of `/workspaces/clawonshape/deploy/team.json` was
`68b9b10f…cff3` at the start and at the end. It was used for its shape only; no value
from it was copied.

## Changed files

```
 docs/inventory-v2.md                 |  59 +++   new section "Runtime pin overlay (runtime_pins, v1)"
 docs/manual.md                       |  43 ++    pins show/init, upgrade output, migration walkthrough
 src/clawake/cli.py                   | 150 +++-   _load_layers (warnings → WARNING lines), pins show/init, validate line, upgrade preview/execute hooks
 src/clawake/config.py                |  68 ++-   Inventory.runtime_pins, InventoryLayers, load_inventory_layers(), v2 rejection
 src/clawake/services/runtime_pins.py | 697 +++   overlay read/validate/merge/write, backups, read_pins, set_plugin_pin, set_image_pins, plan_init
 src/clawake/services/upgrade.py      |  73 ++-   overlay branch in build_upgrade_plan/apply_upgrade/rollback_to_known_good, plan_overlay_upgrade
 tests/test_rollout_foundation.py     |  54 +++   golden: split inventory + overlay renders byte-identical to the fixture
 tests/test_runtime_pins.py           | 873 +++   52 tests: merge, validation, file safety, writers, CLI
```

## Checks

| Command | Result |
|---|---|
| `uv sync --locked` | pass |
| `uv run ruff check .` | pass ("All checks passed!") |
| `uv run ruff format --check .` | pass (45 files already formatted) |
| `uv run pytest -q` | pass: **445 passed, 2 skipped** (base: 392 passed, 2 skipped; +53 new) |
| `uv lock --check` | pass |

No new `# noqa` or `# type: ignore`. No new dependencies.

**Tests failed first.** Before the implementation, `tests/test_runtime_pins.py` failed at
collection (`ImportError: cannot import name 'load_inventory_layers'`). The new golden test
`test_split_inventory_and_overlay_render_identically_to_fixture` failed with a
`ValidationError` (14 other tests in that file passed).

**Mutation checks.** Each mutation was applied to a scratch copy of `src`
(`PYTHONPATH=<scratch>/src`, `clawake.__file__` confirmed), and
`tests/test_runtime_pins.py` plus `tests/test_rollout_foundation.py` were run. Every mutant
was killed:

| Mutation | Result |
|---|---|
| conflict rule disabled (`existing != value` → `False`) | 5 failed |
| image allowlist removed | 1 failed |
| top-level allowlist removed | 1 failed |
| npm guard removed | 2 failed |
| upgrade writer target: overlay branch disabled (writes inventory) | 2 failed |
| backup retention 20 → 21 | 1 failed |
| backup pruning removed | 1 failed |
| symlink refusal removed | 2 failed |
| group/world-writable refusal removed | 2 failed |

**Legacy unchanged.**
- `test_without_runtime_pins_upgrade_writes_inventory_byte_for_byte_as_before` (with the
  `pins_backup` line removed) also passes against base 92ed97b.
- `tests/test_upgrade.py` and `tests/test_cli.py` give 24 passed on base and on HEAD.
- No existing test was modified. Only an additive test was appended to
  `test_rollout_foundation.py`.

## Acceptance criteria

- **Every step-2 item has a named test that failed first — met.**
  - Merge:
    - `test_merge_overlay_supplies_plugin_pins_and_image_digests`
    - `test_merge_equal_duplicates_are_allowed`
    - `test_merge_different_duplicates_fail_naming_both_sources`
    - `test_merge_plugin_conflict_fails`
  - Validation:
    - `test_unknown_keys_are_rejected_at_every_level` (4 levels)
    - `test_cluster_mismatch_is_rejected`
    - `test_missing_version_or_cluster_is_rejected`
    - `test_unknown_member_is_rejected`
    - `test_undeclared_plugin_is_rejected`
    - `test_npm_plugin_pins_are_rejected`
    - `test_malformed_digests_are_rejected` (3 cases)
    - `test_non_string_values_are_rejected`
    - `test_invalid_json_is_rejected`
  - v2 and a missing file:
    - `test_v2_rejects_runtime_pins`
    - `test_missing_overlay_file_fails`
    - `test_missing_overlay_file_is_allowed_for_migration`
  - File safety:
    - `test_write_is_atomic_private_and_creates_backup` (temp file in the same directory,
      0600 before the rename, target unchanged until the rename, fsync, backup 0600)
    - `test_failed_write_leaves_previous_overlay`
    - `test_backup_retention_keeps_newest_twenty`
    - `test_group_writable_overlay_is_refused`
    - `test_world_writable_overlay_is_refused`
    - `test_readable_overlay_warns`
    - `test_symlink_target_is_refused_for_writes`
    - `test_symlinked_backup_directory_is_refused`
    - `test_create_only_refuses_existing_file`
  - Writers:
    - `test_upgrade_and_rollback_write_overlay_and_leave_inventory_identical`
    - `test_upgrade_refuses_when_inventory_duplicates_the_image_field`
    - `test_without_runtime_pins_upgrade_writes_inventory_byte_for_byte_as_before`
  - `set_plugin_pin`:
    - `test_set_plugin_pin_round_trip`
    - `test_set_plugin_pin_validates_before_writing`
    - `test_set_plugin_pin_requires_runtime_pins`
  - `pins show`:
    - `test_pins_show_reports_sources`
    - `test_pins_show_without_overlay_reports_inventory`
  - `pins init`:
    - `test_pins_init_preview_writes_nothing`
    - `test_pins_init_execute_writes_overlay_and_split_loads_identically`
    - `test_pins_init_refuses_to_overwrite` (preview and `--execute`)
    - `test_pins_init_with_declared_but_missing_overlay`
  - Golden: `test_split_inventory_and_overlay_render_identically_to_fixture`.
  - Mutation checks: see *Checks*.
- **Split and unsplit inventories render identically — met.** The test compares Quadlets,
  `desired_fields` and `plan_runtime_files` for the fixture and for its split form
  (inventory without any pin field + overlay). They are byte-identical, and the Quadlets
  also equal the committed golden files. No golden expectation changed.
- **Without `runtime_pins`, every existing test passes unchanged — met.** See *Legacy
  unchanged* in *Checks*.
- **All five checks green, no deployment data in the diff — met.** All fixtures are
  synthetic (`example-*`, `/srv/example`, repeated-hex digests).

## Behaviour changes for operators

- **New inventory field.** Top-level `runtime_pins: <path>` (v1 only; v2 → `runtime_pins
  is only supported in inventory version 1; v2 rollout does not exist yet`). When it is
  set, every command that loads the inventory needs the overlay file: `setup`, `restart`,
  `status`, `sync-plugins`, `upgrade`, `teardown`, `validate`, … A missing file gives
  exit 2: `runtime pins file <path> does not exist`.
- **Overlay mode.**
  - A group- or world-writable overlay is refused with exit 2.
  - A group- or world-readable overlay prints `WARNING: runtime pins file <p> is group- or
    world-readable (mode 0644); run: chmod 600 <p>` to stderr and continues.
- **`validate`** prints an extra line `runtime pins: <path> (N members, M fields)`.
- **`clawake pins show -c <inv> [--format json]`** (new, read-only):
  - Each line is `  <field> = <value> [inventory|overlay|overlay, also in inventory]`.
  - The JSON form is
    `{"cluster", "runtime_pins", "members": {<m>: {<field>: {"value", "source",
    "also_in_inventory"}}}}`.
- **`clawake pins init -c <inv> --output <path> [--execute]`** (new):
  - The preview prints the exact file content, the removal list and the `runtime_pins`
    line. For `.json` inventories the line is `"runtime_pins": "<path>"`.
  - It exits 2 if the output already exists or its directory is missing.
  - `--execute` creates the file 0600 via `link()`, so it never overwrites. It never edits
    the inventory and makes no backup, because there is no previous overlay.
- **`upgrade`** with an overlay:
  - The preview adds `  pins: <path> (inventory file is not modified)`.
  - The preview exits 2 with `Cannot plan upgrade: runtime pins conflict: …` if the
    inventory still holds an image field that the upgrade would change.
  - `--execute` prints `Updated runtime pins; previous pins: <backup>`, and on failure
    `Runtime pins recovery file: <backup>`.
  - The inventory config backup in `.backups/` is still made, as before.
- **Unchanged without `runtime_pins`:** the `upgrade` output, the inventory edit and the
  exit codes.

**Error texts** (synthetic run, `clawake validate`, exit 2, inside the usual Typer error
box):

```
Invalid value for --config: Cannot load team.yml: runtime pins conflict: instances[example-lead].image.digest is 'sha256:cccc…cccc' in inventory team.yml but 'sha256:aaaa…aaaa' in runtime pins /…/private/pins.json; remove it from one of them
```
```
Invalid value for --config: Cannot load team.yml: runtime pins file /…/private/pins.json: members.example-lead.image.repository: unknown key (allowed: tag, digest, known_good_tag, known_good_digest)
```

(The digests are shortened here; the real message prints them in full.)

**`pins init` preview** for a synthetic two-member inventory shaped like the deployed one
(digests shortened here only):

```
Runtime pins overlay to write: /…/demo/private/pins.json (mode 0600)
{
  "version": 1,
  "cluster": "example-cad",
  "members": {
    "example-lead": {
      "image": {
        "tag": "2026.1.1",
        "digest": "sha256:aaaa…aaaa",
        "known_good_tag": "2026.1.0",
        "known_good_digest": "sha256:bbbb…bbbb"
      },
      "plugins": {
        "example-cad": {
          "artifact_path": "/srv/example/packages/example-cad-0.0.1.tgz",
          "sha256": "sha256:1111…1111"
        }
      }
    },
    "example-engineer": { … same shape … }
  }
}
Then remove these 12 field(s) from team.yml:
  - instances[example-lead].image.tag
  - instances[example-lead].image.digest
  - instances[example-lead].image.known_good_tag
  - instances[example-lead].image.known_good_digest
  - instances[example-lead].plugins[example-cad].artifact_path
  - instances[example-lead].plugins[example-cad].sha256
  - instances[example-engineer].image.tag
  - instances[example-engineer].image.digest
  - instances[example-engineer].image.known_good_tag
  - instances[example-engineer].image.known_good_digest
  - instances[example-engineer].plugins[example-cad].artifact_path
  - instances[example-engineer].plugins[example-cad].sha256
And add this top-level line:
  runtime_pins: /…/demo/private/pins.json
Verify with: clawake validate -c team.yml and clawake pins show -c team.yml
DRY RUN; the inventory is never edited. Re-run with --execute to write.
```

## Live E2E request

The architect works on a **private copy** of the deployed inventory. Paths are examples;
`<copy>` is a scratch directory, `<pins-dir>` is a private 0700 directory outside any
checkout.

1. Run the baseline before any change:
   - `clawake validate -c <copy>/team.json`
   - `clawake setup -c <copy>/team.json`, and note the `N/M member(s) changed` line and
     the member actions.
   - `clawake sync-plugins -c <copy>/team.json`, and note the `action:` lines.
   - `clawake pins show -c <copy>/team.json`. Expected: `runtime pins: none declared`, and
     every pin `[inventory]`.
2. Preview the overlay:
   `clawake pins init -c <copy>/team.json --output <pins-dir>/pins.json`. Expected: the
   JSON holds 4 image fields and 2 archive plugin fields per member (12 in total), the
   removal list has 12 entries, and the line `"runtime_pins": "<pins-dir>/pins.json"` is
   shown.
3. Write the overlay with the same command plus `--execute`. Expected: `Wrote …`, the file
   has mode 0600, and the copy of `team.json` is byte-identical (`sha256sum` before and
   after).
4. Edit `<copy>/team.json` by hand:
   1. Add the `runtime_pins` line, then run `clawake validate` and `clawake pins show`.
      Expected: valid, every field `[overlay, also in inventory]`.
   2. Remove the 12 listed fields, then run `clawake pins show`. Expected: every field
      `[overlay]`.
5. Prove equivalence. Expected: identical to step 1.
   - `clawake validate -c <copy>/team.json` adds `runtime pins: <pins-dir>/pins.json (2
     members, 12 fields)`.
   - The `clawake setup -c <copy>/team.json` preview shows the same `changed` count as in
     step 1 (expected `0/2 member(s) changed`) and the same actions.
   - The `clawake sync-plugins -c <copy>/team.json` preview shows the same `action:` lines.
6. Optional negative checks:
   - Change one digest in the copy only: `validate` exits 2 with `runtime pins conflict`.
   - `chmod 664 pins.json`: exits 2 with `group- or world-writable`.
   - `upgrade -c <copy>/team.json -m <member> --to <tag> --digest <digest>` preview shows
     `pins: …`. Do not use `--execute` here.

`upgrade --execute` against an overlay should be tested live only in a maintenance window.
Then, expected:
- only `pins.json` changes;
- a new `pins-backups/<UTC>.json` appears;
- `team.json` is byte-identical.

### Migration recipe for the operator session (deployed team)

1. Run `install -d -m 0700 ~/.config/clawake/<cluster>` (outside `clawonshape/`, never
   versioned).
2. Run `clawake pins init -c deploy/team.json --output ~/.config/clawake/<cluster>/pins.json`
   and review it. Then run the same command with `--execute`.
3. Add `"runtime_pins": "<absolute path from the preview>"` at the top level of
   `deploy/team.json`. Equal duplicates are allowed, so `validate` passes at this point.
4. Remove exactly the fields listed in the preview:
   - for each member, `image.tag`, `image.digest`, `image.known_good_tag`,
     `image.known_good_digest`;
   - for each archive plugin, `artifact_path` and `sha256`.

   Keep `image.repository` and all plugin `id`/`config`/`enabled`/`custom_ui`.
5. Run the read-only proof:
   - `clawake validate -c deploy/team.json` (shows the `runtime pins:` line);
   - `clawake pins show -c deploy/team.json` (all `[overlay]`);
   - the `clawake setup -c deploy/team.json` preview: `0/2 member(s) changed`, actions
     `none`;
   - the `clawake sync-plugins -c deploy/team.json` preview: the same actions as before
     the migration.
6. From now on, plugin rollouts change the overlay, not `team.json`: in P3b via
   `set_plugin_pin`, until then by hand with `chmod 600` kept. `upgrade` writes the image
   fields there itself.

## Open points and risks

- **Proposed `architecture.md` text** (architect-owned, not changed):
  > **Runtime pins (ADR-004).** A v1 inventory may reference a private overlay via
  > `runtime_pins`. `config.load_inventory()` applies it to the expanded raw data before
  > model validation, through `services/runtime_pins.py`. It allowlists image
  > tag/digest/known-good fields and archive plugin `artifact_path`/`sha256`, and fails
  > on conflicts. Clawake writes the overlay atomically (0600, no symlinks) after copying
  > the previous version to `pins-backups/` (newest 20). With an overlay, `upgrade` and
  > rollback write it instead of the inventory; `rollout` (P3b) uses `set_plugin_pin`.
  > `clawake pins show` shows the source of every effective value. `clawake pins init`
  > migrates preview-first and never edits the inventory.
- **Plugin pins by hand until P3b.** Clawake writes plugin pins only through
  `set_plugin_pin`. Until P3b, an operator who edits `pins.json` by hand must keep mode
  0600, or loading refuses or warns.
- **Reading follows symlinks.** Loading follows a symlinked overlay; only writes refuse
  symlinks. The prompt restricts symlink refusal to writes.
- **Overlay tag syntax.** Overlay tags must match the OCI tag grammar
  `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`. A v1 inventory with an unusual tag would make
  `pins init` fail validation instead of writing. This does not affect current
  inventories.
- **Duplicate warnings.** In `upgrade --execute` and `pins init`, overlay permission
  warnings are printed once by the CLI loader and suppressed in the library writers. The
  post-upgrade reload may print the warning a second time. This is cosmetic.
- **OpenClaw behaviour.** This change relies on no OpenClaw plugin, grant, MCP or config
  behaviour, so no docs.openclaw.ai page was needed.

## Out-of-scope findings

- `src/clawake/services/upgrade.py:48-120` (`_update_image_fields`, legacy path): on a
  **JSON** inventory, the flow-style branch rewrites the image object with
  `yaml.safe_dump(..., default_flow_style=True)`. The file stops being valid JSON:
  `{repository: ghcr.io/…, tag: 2026.2.0, …}` (verified in a scratch run). The deployed
  inventory is JSON, so a legacy `upgrade --execute` there would corrupt it. With
  `runtime_pins`, this path is no longer used.
- `src/clawake/services/upgrade.py:44` and `:109`: `_format_image_value` writes the
  literal `None` when `apply_upgrade` gets `next_digest=None` and the image has no digest.
  This is reachable only through the library; the CLI always requires a digest.
- `src/clawake/services/upgrade.py:260-263` (`backup_config`): JSON inventories are backed up
  with a `.yaml` suffix.
