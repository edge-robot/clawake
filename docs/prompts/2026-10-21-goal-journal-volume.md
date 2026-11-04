---
target-repo: edge-robot/clawake
branch: feat/40-goal-journal
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#40
architecture-ref: clawake/docs/architecture.md
contract-version: 1.1.0-draft   # edge-robot/clawonshape docs/interface/onshape-mcp.md §12.1, §1
base-commit: 85079c8 (main)
status: done
---
# Persistent dispatch journal volume for the Onshape MCP service (#40)

## Goal

Let the operator enable goal-scoped mutation authorization for the isolated Onshape MCP service.
To support this, Clawake must:

- provision an optional, persistent, private, rw **dispatch journal** directory for the server;
- validate that the server configuration and the journal mount agree;
- never handle the grant file contents or any credential.

## Context

- Contract (edge-robot/clawonshape, `docs/interface/onshape-mcp.md`):
  - §12.1: server keys `goal_grant_file` and `dispatch_journal_dir` in `[external_token]`;
    `dispatch_journal_dir` is required whenever `goal_grant_file` is set;
  - §1: mounts; the journal is rw in the server only and persists across restarts and image upgrades;
  - §12.4: the journal is the server's only state, and losing it blocks goal admission.
- Server behaviour (edge-robot/onshape-mcp#5, accepted):
  - The journal directory must be owned by the server's runtime UID, mode `0700`, not a symlink.
    Otherwise the server still starts, but `/capabilities` omits `goal-grant-v1`.
  - The expected container path is `/var/lib/onshape-mcp/journal`.
  - The grant file lives next to the approval file inside the existing ro credential mount
    (`/run/onshape-credentials/…`). **No new ro mount is needed** for it.
- Code: `src/clawake/services/onshape_mcp.py`:
  - `OnshapeMcpSpec`;
  - `render_onshape_mcp`: the Quadlet has `ReadOnly=true`, `UserNS=keep-id:uid=1000,gid=1000` and
    the two ro volumes;
  - `validate_runtime_paths`;
  - `plan_onshape_mcp`.

  CLI: `clawake onshape-mcp` in `src/clawake/cli.py`. Tests: `tests/test_onshape_mcp.py`. Docs:
  `docs/inventory-v2.md` (MCP service paragraph).
- Related:
  - edge-robot/clawake#39 (`HealthCmd`, branch `fix/39-mcp-healthcmd`, not merged). It touches the
    same Quadlet text, so keep your diff away from the `Health*` lines.
  - edge-robot/clawake#16 (UID mapping G7).

## Binding constraints and prohibitions

- Clawake never reads, writes or validates the **contents** of the grant, approval or token files.
  It only checks ownership and mode of the directories it mounts.
- The new spec field is **optional**. Without it, the rendered Quadlet stays byte-identical to the
  current one, and an existing test proves that.
- The journal volume is mounted into the MCP container only, rw, at exactly
  `/var/lib/onshape-mcp/journal`.
- Validation reads the operator's `config.toml` only to check the two keys (TOML parse with
  `tomllib`, no other fields interpreted). The rules:
  - `goal_grant_file` set without a declared journal directory → validation error;
  - a declared journal directory without `dispatch_journal_dir = "/var/lib/onshape-mcp/journal"`
    → validation error;
  - error messages name the key, never values from other keys.
- Path rules as for the existing paths: absolute, disjoint from the other MCP paths and the Quadlet
  root, private owner permissions at `apply --execute` (`validate_runtime_paths`).
- Public repository: no host paths, IDs or deployment values in code, tests, docs or the report.
  Use placeholders.
- Do not touch `Containerfile`, `.devcontainer/`, `scripts/dev.sh` or `.claude/`. No deployment and no
  `--execute` against a real system.
- Work only on `feat/40-goal-journal` in this worktree. No new branches, worktrees or stashes. Never
  push.

## Steps

1. Verify the branch and base. Record the baseline: `uv run pytest -q` (count), `uv run ruff check`,
   `uv run ruff format --check`, plus the type check if the repo has one configured.
2. `OnshapeMcpSpec`: add the optional `dispatch_journal_directory: HostPath | None`. Include it in
   the disjointness check.
3. Render: if it is set, add `Volume=<dir>:/var/lib/onshape-mcp/journal:rw` after the existing
   volumes. Change nothing else.
4. Validation: add a pure function that checks the `config.toml` key consistency (see the
   constraints). Call it from `onshape-mcp` `plan` and `apply`, so that a preview already fails on
   inconsistency. Extend `validate_runtime_paths` to the journal directory (directory, owner = the
   current user, no group or other bits, not a symlink).
5. Tests:
   - byte-identical Quadlet without the field;
   - the rendered volume line with the field;
   - disjointness;
   - each consistency rule, both directions;
   - private-permission failures for the journal directory;
   - the error messages do not leak other config values.
6. Docs: extend the MCP service paragraph in `docs/inventory-v2.md`:
   - the new field;
   - the ownership and UID requirement with a link to #16;
   - that the journal is persistent state the operator must back up;
   - that a lost journal blocks goal admission rather than resetting budgets.
   Is the journal covered by `upgrade` backups? Investigate and **report** only, do not implement:
   the MCP service is operated separately from member upgrades.

## Acceptance criteria and tests

| # | Criterion | Evidence |
|---|---|---|
| A1 | Without the field, the Quadlet output is unchanged | test |
| A2 | With the field, exactly one rw volume line at the contract path | test |
| A3 | Inconsistent `config.toml` fails at `plan` (preview) and at `apply` | tests |
| A4 | Journal directory permissions are checked at `apply --execute` | tests |
| A5 | No file contents of grant, approval or token files are read anywhere | code review + test that only `config.toml` is parsed |
| A6 | All checks pass; the test count is ≥ the baseline | checks table |
| A7 | Backup coverage finding reported | report |

## Branch and worktree rules

Commit on `feat/40-goal-journal` only, with the trailers `Refs: edge-robot/clawake#40`,
`Prompt: docs/prompts/2026-10-21-goal-journal-volume.md`, `Agent: py-clawake-dev` and
`Co-Authored-By`. Never push. Hand back with the report.

## Expected report / handoff format

`docs/prompts/2026-10-21-goal-journal-volume.report.md` in the agent-definition format (frontmatter
`status`, `contract-version: 1.1.0-draft`, `e2e-request: required`). It must contain:

- the checks table;
- A1–A7 with evidence;
- the rendered Quadlet diff;
- the backup finding;
- the operator steps for the live E2E (with placeholders);
- the commit list with trailers.
