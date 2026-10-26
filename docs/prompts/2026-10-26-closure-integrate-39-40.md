---
target-repo: edge-robot/clawake
branch: feat/40-goal-journal
worktree: <host worktree, redacted> (container: /workspaces/clawake)
issue: edge-robot/clawake#40, edge-robot/clawake#39
architecture-ref: clawake/docs/architecture.md
base-commit: the commit that adds this prompt
status: done   # accepted 2026-10-26 (report 122cb00); host path in 71e0315 history pending user decision before push
---
# Closure K4: integrate #39 into the goal-journal branch

## Goal

The project is closing. Make `feat/40-goal-journal` the single branch that carries both #40
(goal grant file and persistent dispatch journal) and #39 (onshape-mcp healthcheck without a
hard-coded curl `HealthCmd`), green and ready for one pull request to `main`.

## Context

- `fix/39-mcp-healthcmd` @ `5bd8b64` is pushed but not merged; it is accepted
  (`docs/prompts/2026-10-20-*mcp-healthcmd*`).
- `feat/40-goal-journal` is accepted
  (`docs/prompts/2026-10-21-goal-journal-volume.report.md`). Its section "Merge with #39" lists the
  known semantic conflicts:
  1. 10 #39 health tests write a non-TOML `config.toml`. Write `""` or `"[external_token]\n"`.
  2. 16 CLI tests in `tests/test_onshape_mcp_journal.py` must also patch
     `clawake.services.onshape_mcp.default_mcp_probes`, because #39's preflight runs during `plan`.
  3. Regenerate the `without-journal`/`with-journal` goldens only if the default `Health*` lines
     changed.

  The order question: #39's preflight runs before #40's config check. Keep that order unless a test
  shows that the pure config check must fail first. Report your choice.
- This repository is **public**. Never commit host paths, user names, Discord or Onshape IDs,
  digests of private images or other deploy values.

## Binding constraints and prohibitions

- **No deployment.** Do not run `clawake` against any real inventory, and no `--execute`. The
  running cluster stays on `4b59c83` until after the final presentation.
- Merge `fix/39-mcp-healthcmd` into `feat/40-goal-journal` with a merge commit; no rebase or
  history rewrite. Then add the minimal test fixes on top. Production code changes only where a
  conflict requires them; name every one.
- Do not touch `Containerfile`, `.devcontainer/`, `scripts/dev.sh` or `.claude/`. No new
  dependencies.
- Work only on `feat/40-goal-journal` in this worktree. No new branches, worktrees or stashes.
  Never push.

## Steps

1. Verify branch and HEAD, and that the tree is clean. Record a baseline test run.
2. Run `git merge --no-ff fix/39-mcp-healthcmd`. Resolve conflicts if there are any.
3. Apply the known test fixes 1–3. Run the full suite plus `ruff check` and `ruff format --check`.
4. Public hygiene: `git diff origin/main...HEAD` must contain no host paths, user names, IDs or
   private digests. Fix only test data or docs that leak; report anything else.
5. Draft the PR description (not created) into the report: summary, `Closes #39`, `Closes #40`,
   test evidence, operator notes. The operator notes are: switching the service to the healthcheck
   subcommand requires a distroless image with the subcommand, and `config.toml` must be valid
   TOML.

## Acceptance criteria and tests

| # | Criterion | Evidence |
|---|---|---|
| C1 | Merge commit present; both issue scopes intact | `git log --graph` excerpt |
| C2 | Full suite, ruff check and ruff format pass | checks table |
| C3 | Public hygiene clean | grep commands and results |
| C4 | PR description drafted | report |

## Branch and worktree rules

Commit on `feat/40-goal-journal` only. Each commit has:

- author `Albert Kos <kickban3000@gmail.com>`;
- the trailers `Refs: edge-robot/clawake#39`, `Refs: edge-robot/clawake#40`,
  `Prompt: docs/prompts/2026-10-26-closure-integrate-39-40.md`, `Agent: py-clawake-dev` and
  `Co-Authored-By`.

Never push.

## Expected report / handoff format

Write `docs/prompts/2026-10-26-closure-integrate-39-40.report.md` with frontmatter `status` and
`e2e-request: recommended`; the live check comes with the next rollout after the project. Include:

- the checks table;
- C1–C4;
- the order decision;
- changed files;
- the PR description draft;
- the commit list.
