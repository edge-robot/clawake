---
target-repo: edge-robot/clawake
branch: feat/20-runtime-pins
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#20
architecture-ref: clawake/docs/ADR.md#adr-004-runtime-pins-live-in-a-private-overlay-referenced-by-the-inventory
base-commit: 92ed97b (feat/5-targeted-restarts 6db6fed + ADR-004)
status: issued
---
# P3a: Runtime pin overlay (ADR-004)

## Goal

Move per-rollout values out of the versioned v1 inventory into a private, Clawake-owned
overlay file. These values are plugin archive pins and image tag/digest pins. The
inventory references the overlay. `upgrade` / rollback write it. P3b (`clawake rollout`)
will write plugin pins through the API you add here.

## Context

- **Decision:** `docs/ADR.md`, ADR-004 (accepted). Read it completely; it is binding. Also
  read ADR-001 for the fail-closed style.
- **Issue:** edge-robot/clawake#20. Umbrella: #24.
- **Current code:**
  - **Loading.** `src/clawake/config.py` `load_inventory()` reads YAML/JSON, expands
    `${…}` (`_expand_string_values`) and validates into `Inventory`.
  - **Plugin pins.** `PluginSpec` requires exactly one source: `artifact_path` +
    `sha256`, or `npm_spec` + `integrity`.
  - **Image pins.** `ImageSpec` has `tag`, `digest`, `known_good_tag` and
    `known_good_digest`.
  - **Writers.** `src/clawake/services/upgrade.py` `apply_upgrade()` and
    `rollback_to_known_good()` rewrite image fields **in the inventory file**
    (`_update_image_fields`). `cli.upgrade_member()` calls them.
- **P1/P2** (accepted, on this branch's history):
  - `services/readiness.py` and `services/member_ops.py`;
  - the installed-plugin ledger in `clawake-managed-plugin-runtime.json`.

  Do not change their behaviour.
- **Shape of the deployed inventory** (private, reference only):
  `/workspaces/clawonshape/deploy/team.json`. It is v1 JSON with two instances, each
  with an `image` (repository, tag, digest, known_good_*) and one archive plugin. Use it
  to make sure your design fits; never copy values from it.

## Binding constraints and prohibitions

1. **Overlay reference.**
   - Add an optional top-level v1 field `runtime_pins: str`. It is a path, relative to
     the inventory file's directory unless absolute, with `${…}` expanded like other
     paths.
   - A missing file is an error **unless** the command is the migration command (item 6).
   - v2 inventories reject `runtime_pins` with a clear error.
2. **Overlay format (JSON).**
   ```json
   {
     "version": 1,
     "cluster": "<inventory cluster.name>",
     "members": {
       "<instance name>": {
         "image": {"tag": "...", "digest": "sha256:...", "known_good_tag": "...", "known_good_digest": "sha256:..."},
         "plugins": {"<plugin id>": {"artifact_path": "...", "sha256": "sha256:<64 hex>"}}
       }
     }
   }
   ```
   - Every key is optional except `version` and `cluster`.
   - Reject, with errors that name the path:
     - unknown keys at any level;
     - `cluster` mismatch;
     - unknown members;
     - plugin ids not declared on that member;
     - pins for npm plugins;
     - malformed digests.
3. **Merge semantics.**
   - Apply the overlay to the **raw** inventory data after `${…}` expansion and
     **before** `Inventory.model_validate`, so the existing validators see the effective
     data.
   - A plugin entry in the inventory may then omit `artifact_path` / `sha256` when the
     overlay supplies them.
   - If the inventory and the overlay set the same field to **different** values,
     loading fails with an error naming both sources. Equal values are fine.
4. **File safety.**
   - Writes are atomic: temporary file in the same directory, `fsync`, rename, mode
     0600.
   - Before every write, copy the previous overlay to `<overlay dir>/pins-backups/<UTC
     timestamp>.json` (mode 0600) and keep the newest 20.
   - Loading refuses a file that is group- or world-writable. It warns if the file is
     group- or world-readable.
   - Never follow a symlink for writes.
5. **Writers.**
   - With `runtime_pins` declared, `apply_upgrade()` and `rollback_to_known_good()`
     write image fields to the overlay and **never** touch the inventory file. Without
     it, behaviour is byte-for-byte unchanged; prove this with a test.
   - Add a library API for P3b: `set_plugin_pin(inventory_path, member, plugin_id,
     artifact_path, sha256) -> backup_path`, plus `read_pins(...)`. No CLI to set plugin
     pins in this prompt.
6. **CLI.**
   - **`clawake pins show -c <inventory> [--format json]`** (read-only). Prints the
     effective pins per member and, for each field, its source (`inventory` or
     `overlay`).
   - **`clawake pins init -c <inventory> --output <path> [--execute]`** (migration).
     - The preview prints the overlay that would be written: all image fields and archive
       plugin pins currently in the inventory, plus the exact list of inventory fields the
       operator should then remove and the `runtime_pins` line to add.
     - `--execute` writes the file with the safety rules above.
     - It refuses to overwrite an existing file.
     - It never edits the inventory.
   - `validate` reports `runtime pins: <path> (N members, M fields)` when an overlay is
     in use.
7. **Scope limits.**
   - Do not implement `rollout` (P3b).
   - Do not change `setup` / `sync-plugins` behaviour other than that they now see the
     effective data through `load_inventory`.
   - No new dependencies. Public repository: synthetic data only. Never run `--execute`,
     `podman`, `systemctl` or `openclaw` yourself.

## Steps

1. Verify the preconditions: the branch is `feat/20-runtime-pins` and HEAD is `92ed97b`.
   Run the checks and record the base test count.
2. Write failing tests first:
   - merge: an overlay supplies plugin pins and image digests; equal duplicates are
     allowed; different duplicates fail;
   - validation: every rejection in item 2;
   - v2 rejects `runtime_pins`; a missing overlay file fails;
   - file safety: atomic write, mode 0600, backup created, 20 kept, a group-writable file
     is refused, a symlink target is refused;
   - `upgrade` / rollback write the overlay and leave the inventory byte-identical; without
     an overlay, the legacy path is unchanged;
   - `set_plugin_pin` round trip;
   - `pins show` sources;
   - `pins init` preview, execute, refusal to overwrite, and that the inventory is
     untouched;
   - golden rendering: for a synthetic fixture, the inventory with full pins and the
     split inventory + overlay render byte-identical Quadlets and runtime plans
     (`tests/test_rollout_foundation.py` style).
3. Implement, e.g. `src/clawake/services/runtime_pins.py` plus the minimal hooks in
   `config.py`, `upgrade.py` and `cli.py`.
4. Document the overlay in `docs/inventory-v2.md` (v1 section) and `docs/manual.md`,
   including a migration walkthrough with synthetic paths. Propose `architecture.md` text
   in the report.
5. Run the checks, commit and write the report.

## Acceptance criteria and tests

- [ ] Every item in step 2 has a named test that failed first. Run mutation checks on the
      conflict rule, the allowlist, the upgrade writer target and backup retention.
- [ ] Split and unsplit inventories produce identical rendering (golden).
- [ ] Without `runtime_pins`, every existing test passes unchanged.
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are all green. No deployment data in the
      diff.

## Branch and worktree rules

- Commit only to `feat/20-runtime-pins` in this checkout. No other branches, no
  worktrees, no push, no rebase. Commit this prompt unchanged together with your report.
- Leave the worktree clean.

## Expected report / handoff format

`docs/prompts/2026-10-19-runtime-pins.report.md` in the agent-definition format.
Include:

- the `pins init` preview for a synthetic two-member inventory shaped like the deployed
  one;
- a step-by-step migration recipe the architect can hand to the operator session (which
  fields to remove from the inventory, where to put the overlay, which read-only checks
  prove equivalence: `validate`, the `setup` preview `0/N changed`, the `sync-plugins`
  preview, and `pins show`);
- the exact error texts for a conflict and for an unknown key.

Set `e2e-request: required`. The architect will run `pins init` / `pins show` and the
previews against a private copy of the deployed inventory before the operator migrates.
