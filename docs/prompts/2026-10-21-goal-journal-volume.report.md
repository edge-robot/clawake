---
prompt: docs/prompts/2026-10-21-goal-journal-volume.md
branch: feat/40-goal-journal
commits: c5c3d28..485e687
status: done
contract-version: 1.1.0-draft
e2e-request: required
---
# Report: Persistent dispatch journal volume for the Onshape MCP service (#40)

## Summary

`OnshapeMcpSpec` has a new optional field, `dispatch_journal_directory`. When it is set, the
Quadlet gets exactly one extra line after the existing volumes:
`Volume=<journal-dir>:/var/lib/onshape-mcp/journal:rw`. Without the field, the render is
byte-identical to the base commit. I proved this against a golden rendered by 85079c8 from a
scratch tree.

The journal path is part of the disjointness check and of `validate_runtime_paths` at
`apply --execute`: it must be a directory, owned by the current user, with no group or other
bits, and not a symlink.

A new pure function `check_journal_config(spec, text)` parses `config.toml` with `tomllib`. It
interprets only `goal_grant_file` and `dispatch_journal_dir` in `[external_token]`.
`validate_journal_config(spec)` reads only `spec.config_file` and calls it. `onshape-mcp`
`plan` and `apply` (preview and `--execute`) run this check before anything is printed or
written, so an inconsistent config already fails the preview.

`docs/inventory-v2.md` describes the field, the rules, the UID requirement (#16), and the
backup duty and lost-journal behaviour.

Clawake does not read grant, approval, token or journal file contents anywhere.

## Changed files

`git diff --stat 1bcacad..485e687` (1bcacad = prompt commit on top of base 85079c8):

| File | Lines | Purpose |
|---|---|---|
| `src/clawake/services/onshape_mcp.py` | +60/−2 | Adds the spec field, the disjointness entry, the render suffix, the journal permission check, `check_journal_config` and `validate_journal_config`, and the `DISPATCH_JOURNAL_CONTAINER_PATH` constant. |
| `src/clawake/cli.py` | +8 | `onshape-mcp` plan/apply runs the config check; an unreadable config or a rule violation exits 2 (`BadParameter`). |
| `tests/test_onshape_mcp_journal.py` | +391 | 46 new tests covering A1–A5. |
| `tests/fixtures/onshape-mcp-journal/without-journal.container` | +37 | Golden rendered by the base commit (85079c8). |
| `tests/fixtures/onshape-mcp-journal/with-journal.container` | +38 | Golden with the journal volume. |
| `docs/inventory-v2.md` | +28 | The MCP service paragraph: field, rules, UID/#16, backup, lost journal. |

## Checks

| Command | Baseline (1bcacad) | After (485e687) |
|---|---|---|
| `uv sync --locked` | pass | pass |
| `uv run ruff check .` | pass ("All checks passed!") | pass |
| `uv run ruff format --check .` | pass (52 files) | pass (53 files) |
| `uv run pytest -q` | 621 passed, 2 skipped | **667 passed, 2 skipped** (+46) |
| `uv lock --check` | pass | pass |
| Type check | not configured in `pyproject.toml` | n/a |

Additional verification:

- **Base golden:** I ran `git archive 85079c8 src templates` into a scratchpad and rendered with
  `PYTHONPATH=<scratch>/src`; the loaded module was confirmed through `clawake.__file__`. The
  output is byte-identical (`cmp`) to the new render without the field.
- **Mutation checks** on scratch copies of `src`:
  - reading `*.json` in the credential directory → 1 test fails (A5);
  - dropping the journal from `validate_runtime_paths` → 9 fail;
  - removing the CLI config check → 14 fail.
- No `# noqa` or `# type: ignore` was added.

## Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| A1 | Without the field, the Quadlet output is unchanged | met | `test_quadlet_without_journal_is_byte_identical_to_the_previous_render` compares against the golden rendered by 85079c8. |
| A2 | With the field, exactly one rw volume line at the contract path | met | `test_quadlet_with_journal_adds_exactly_one_rw_volume_line` checks: exactly one line, it follows the last `:ro` volume, removing it gives the no-journal render, and the network file is unchanged. Golden `with-journal.container`. |
| A3 | Inconsistent `config.toml` fails at `plan` (preview) and `apply` | met | `test_inconsistent_config_fails_plan_and_apply_before_any_change` covers 3 rules × plan/apply × preview/`--execute`. Each case exits 2, writes no Quadlet and makes no systemd call. Pure rule tests: `test_goal_grant_without_declared_journal_fails`, `test_declared_journal_without_dispatch_journal_dir_fails`, `test_declared_journal_with_other_dispatch_journal_dir_fails`, `test_dispatch_journal_dir_without_declared_journal_fails`, `test_consistent_configurations_pass`. |
| A4 | Journal permissions are checked at `apply --execute` | met | Tests cover a missing directory, modes 0750/0705/0770/0701/0710, a file instead of a directory, a symlink and a foreign owner. `test_apply_execute_checks_journal_permissions_before_writing` shows that no Quadlet is written and systemd is not called. |
| A5 | No grant/approval/token contents are read | met | Code review: the only new read is `Path(spec.config_file).read_text()`. `test_apply_execute_reads_only_config_toml` records `Path.open`, `builtins.open` and `io.open` during `apply --execute` with sentinel `goal-grant.json`, `approved-update.json` and `access-token.json` files present. Nothing under the credential or journal directory is opened, and `config.toml` is opened. The mutation check confirms the test bites. |
| A6 | All checks pass; test count ≥ baseline | met | 667 ≥ 621, see Checks. |
| A7 | Backup coverage finding reported | met | See "Backup finding" below. |

### Rendered Quadlet diff (golden fixtures, placeholder paths)

```diff
@@ -18,6 +18,7 @@
 DropCapability=all
 Volume=/srv/example-mcp/credential:/run/onshape-credentials:ro
 Volume=/srv/example-mcp/config.toml:/run/onshape-config/config.toml:ro
+Volume=/srv/example-mcp/journal:/var/lib/onshape-mcp/journal:rw
 Exec=--config /run/onshape-config/config.toml http
 HealthCmd=curl --fail --silent http://127.0.0.1:8080/health
 HealthInterval=30s
```

The `.network` file does not change, and no expectation of an existing test changed.

### Backup finding (A7; investigated, not implemented)

The journal is **not covered** by any Clawake backup:

- `backup.backup_sources(instance)` (`src/clawake/services/backup.py:94`) archives only the
  member's `workspace_path`, `team_definition_path`, `runtime_state_path` (if it is outside the
  workspace) and `backup_policy.paths`. `backup_config` copies only the inventory file.
- `upgrade` and `rollout` call these only for members. `OnshapeMcpSpec` is a separate service
  spec, and nothing in `upgrade.py`, `rollout.py`, `service_upgrade.py`, `runtime_upgrade.py` or
  `backup.py` references it.
- An MCP image upgrade means changing `image.digest` and running `onshape-mcp --action apply
  --execute`. This rewrites only the Quadlet and restarts the unit. The host journal directory is
  left untouched, so it persists across upgrades as §1 requires, but no backup is taken.
- Workaround without code: an operator could list `<journal-dir>` in some member's
  `backup_policy.paths`. That ties MCP state to a member's backup lifecycle and retention, so it
  is a design decision for the architect.
- Option for a follow-up: an `onshape-mcp --action backup [--execute]` that archives only the
  journal directory into a private archive, reusing `ensure_backup_dir` /
  `write_new_private_file` semantics.
- Note: the journal holds request hashes and Onshape IDs, not tokens (§12.4). Whether that
  counts as sensitive for archive placement is unverified.

## Behaviour changes for operators

- New optional service field `dispatch_journal_directory: <journal-dir>`: an absolute host
  path, disjoint from `credential_directory`, `config_file` and `quadlet_root`.
- **`onshape-mcp --action plan|apply` now reads `config.toml`, even without the journal field.**
  An unreadable file fails with "Onshape MCP config.toml is not readable" (exit 2). Invalid TOML
  fails with "MCP config.toml is not valid TOML" (exit 2), and the decoder message is not shown
  because it can quote config text. Before, `plan` worked with a missing `config.toml`. This
  follows fail-closed: without the file, the consistency rule cannot be checked.
- New exit-2 messages, which name keys only:
  - `goal_grant_file` set while the service declares no `dispatch_journal_directory`;
  - `dispatch_journal_directory` declared while `external_token.dispatch_journal_dir` is not
    `/var/lib/onshape-mcp/journal`;
  - `dispatch_journal_dir` set while the service declares no `dispatch_journal_directory`
    (**third rule, my addition**, see Open points).
- `apply --execute` with a non-private journal directory fails like the existing paths, with
  `ValueError("MCP runtime path requires private owner permissions")`, before any write.
- `status`, `restart`, `stop` and `probe` do not read `config.toml`.

## Live E2E request

Preconditions (operator, placeholders only):

1. `mkdir -m 0700 <journal-dir>` as the user that runs the MCP service, and check with
   `stat -c '%U %a' <journal-dir>`, which should show the operator and `700`.
2. In `<service.yml>`, add `dispatch_journal_directory: <journal-dir>`.
3. In `<config.toml>` under `[external_token]`, add
   `dispatch_journal_dir = "/var/lib/onshape-mcp/journal"`. Add `goal_grant_file =
   "/run/onshape-credentials/<grant-file>"` once the plugin publishes grants.

Commands:

```
clawake onshape-mcp -c <service.yml> --action plan
clawake onshape-mcp -c <service.yml> --action apply
clawake onshape-mcp -c <service.yml> --action apply --execute
```

Expected results:

- The preview lists `<quadlet-root>/clawonshape-mcp.container`. The deployed file differs from
  the previous one only by the one `Volume=<journal-dir>:/var/lib/onshape-mcp/journal:rw` line.
- `--execute` restarts the unit.
- `GET /capabilities` from a client container on the private network lists `goal-grant-v1`
  (when `goal_grant_file` is configured).
- A re-run of `plan` lists nothing (idempotent).
- After a restart and after an image digest change plus `apply --execute`, the `*.jsonl` files
  in `<journal-dir>` are still there.

Negative checks (preview only):

- Remove `dispatch_journal_dir` from `config.toml` → `plan` exits 2 and names the key.
- `chmod 0750 <journal-dir>` → `apply --execute` fails before writing. Restore with `0700`.

Unverified on a live host:

- that the server sees the directory as owned by its UID 1000. Podman documents
  `--userns=keep-id:uid=UID` as "override the UID inside the container that is used to map
  the current user to" (docs.podman.io, podman-run, `--userns`), but the live host's Podman
  version was not checked;
- the server's `/capabilities` behaviour (onshape-mcp#5).

## Open points and risks

- **Third consistency rule (beyond the two listed):** `dispatch_journal_dir` set without a
  declared journal now fails. Without the mount, the path would be on the `ReadOnly=true` rootfs
  and the server would silently omit `goal-grant-v1`. Drop it if the architect wants exactly two
  rules.
- **`goal_grant_file` counts as "set" when the key is present**, even as an empty string. Keys
  outside `[external_token]` are ignored, as §12.1 places them there.
- **Plan now requires a readable, valid-TOML `config.toml`** (see Behaviour changes). This is
  intended fail-closed behaviour, but it changes the preview for setups without a config file.
- **Merge with #39 (`fix/39-mcp-healthcmd`):**
  - Textually, `git merge-tree` is clean after 485e687. I moved my hunks away from the imports,
    the `image =` line, the `Exec`/`Health*` lines and the preflight block.
  - Semantically, a test run of the merged tree in a scratchpad showed 26 failures that need
    small fixes during integration:
    1. The #39 health tests write `config.toml` as `"non-secret config\n"`, which is not valid
       TOML, so the new check rejects it. 10 tests are affected. Fix: write `""` or
       `"[external_token]\n"`.
    2. The CLI tests in `test_onshape_mcp_journal.py` (16 cases) must also patch
       `clawake.services.onshape_mcp.default_mcp_probes`, because #39's preflight runs during
       `plan`. In the merged tree they otherwise attempt a real `podman run`; in the container
       this failed as "podman is not available".
    3. The `without-journal`/`with-journal` goldens must be regenerated if #39 changes the
       default `Health*` lines. In the merged run they still passed.
  - Merge order: preflight (#39) runs before the config check (#40). Swap them if the pure check
    should fail first.
- The UID requirement depends on #16 (UID mapping G7). If the mapping changes away from
  `keep-id:uid=1000`, the owner rule must follow.

## Out-of-scope findings

- `src/clawake/cli.py` (`onshape_mcp`, the `validate_runtime_paths(spec)` call under
  `action == "apply" and execute`): `ValueError` and `FileNotFoundError` are not wrapped in
  `BadParameter`. A permission failure therefore shows up as an uncaught exception (exit 1)
  instead of a clean CLI error. This applies to the existing paths too, and #39 does not change
  it. I did not fix it.
- `tests/test_onshape_mcp.py:70` writes a non-TOML `config.toml` ("non-secret config"). It is
  harmless today because that test calls only `validate_runtime_paths`.
