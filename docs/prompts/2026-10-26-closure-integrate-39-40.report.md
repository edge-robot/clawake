---
prompt: docs/prompts/2026-10-26-closure-integrate-39-40.md
branch: feat/40-goal-journal
commits: 8563c6a..1a98614
status: done
e2e-request: recommended
---
# Report: Closure K4 — integrate #39 into the goal-journal branch

## Summary

`fix/39-mcp-healthcmd` (`5bd8b64`) is merged into `feat/40-goal-journal` with the merge commit
`8563c6a` (`--no-ff`, no rebase, no history rewrite). Git merged `src/clawake/cli.py` and
`src/clawake/services/onshape_mcp.py` without textual conflicts. The two expected semantic test
conflicts appeared exactly as the #40 report predicted (10 health tests + 16 journal CLI tests,
26 failures) and are fixed in tests only. The goldens did not need regeneration. One production
file changed after the merge, and only in whitespace: the auto-merge left one blank line
instead of two between #39's health model and #40's journal constant. Both parents were
ruff-clean. One public-hygiene leak in the closure prompt itself is redacted at the tip.
The leak is still in the history; see *Open points*.

Baseline before the merge (HEAD `71e0315`, clean tree): `uv run pytest -q` → 667 passed,
2 skipped.

## Changed files

`git diff --stat 71e0315..HEAD` (everything the merge and the follow-up commits bring onto the
branch):

| File | Purpose |
|---|---|
| `docs/manual.md` (+57) | #39: health block, preflight and health wait for operators |
| `docs/prompts/2026-10-20-mcp-healthcmd.md` (+148) | #39 prompt (from the merge) |
| `docs/prompts/2026-10-20-mcp-healthcmd.report.md` (+287) | #39 report (from the merge) |
| `docs/prompts/2026-10-26-closure-integrate-39-40.md` (1 line) | redact the host worktree path (hygiene) |
| `src/clawake/cli.py` (+29/−2) | #39: preflight before plan, health wait after apply, health/restarts in status (merge, no manual edit) |
| `src/clawake/services/onshape_mcp.py` (+248) | #39: `McpHealth*`, preflight, `wait_healthy` (merge) + 1 blank line (`5f5687c`) |
| `tests/fixtures/onshape-mcp/clawonshape-mcp.container`, `…-private.network` | #39 goldens (from the merge) |
| `tests/test_onshape_mcp_health.py` (+543) | #39 tests (merge) + fix 1: the fixture writes an empty, valid TOML `config.toml` |
| `tests/test_onshape_mcp_journal.py` (+16) | fix 2: the `systemd` fixture also replaces `onshape_mcp.default_mcp_probes` |

Production code changes after the merge: **only** `5f5687c` (one blank line in
`src/clawake/services/onshape_mcp.py`, needed for `ruff format --check`). There are no
behavioural production changes outside what the two merged branches already contain.

## Known conflicts 1–3

1. **Non-TOML `config.toml` in #39 tests.** The `service_file` fixture in
   `tests/test_onshape_mcp_health.py` wrote `"non-secret config\n"`. It now writes `""`, with
   a comment. This fixed all 10 failing health tests.
2. **Journal CLI tests reach real podman.** The `systemd` fixture in
   `tests/test_onshape_mcp_journal.py` (used by exactly the 16 failing CLI tests) now also patches
   `clawake.services.onshape_mcp.default_mcp_probes` with `healthy_mcp_run`. This fake makes the
   preflight `podman run` return 0, `podman inspect` return `healthy` and
   `systemctl show NRestarts` return `0`. Any other command raises `AssertionError`. The
   `apply --execute` test therefore covers the #39 health wait too.
3. **Goldens.** The default `Health*` lines are the same in #39 and #40
   (`HealthCmd=curl --fail --silent http://127.0.0.1:8080/health`, 30s/5s/3,
   `HealthOnFailure=kill`). `tests/fixtures/onshape-mcp-journal/{without,with}-journal.container`
   are unchanged, and `test_quadlet_without_journal_is_byte_identical_to_the_previous_render` and
   `test_quadlet_with_journal_adds_exactly_one_rw_volume_line` pass. No expectation changed.

## Order decision

**Kept: #39's health preflight runs before #40's config.toml check.** No test needs the pure
config check to fail first. The 12 `test_inconsistent_config_fails_plan_and_apply_before_any_change`
cases still exit 2 with `dispatch_journal…` and write nothing. With the healthy fake, the
preflight passes and the config check then fails. Consequences for operators:

- If an image cannot run its health command **and** `config.toml` is inconsistent, the
  preflight error comes first (exit 1). The config error (exit 2) only shows after the image
  is fixed.
- `plan` makes read-only `podman run --rm --pull=never --network=none` calls before it reads
  `config.toml`. Both checks happen before anything is written, so fail-closed still holds.

Switching the order would be a production change that no conflict requires, so I did not
make it.

## Checks

Final state at `1a98614`:

| Command | Result |
|---|---|
| `uv sync --locked` | pass (rc 0) |
| `uv run ruff check .` | pass, "All checks passed!" |
| `uv run ruff format --check .` | pass, "54 files already formatted" |
| `uv run pytest -q` | pass, 708 passed, 2 skipped |
| `uv lock --check` | pass (rc 0) |

Intermediate results:

- Baseline at `71e0315`: 667 passed, 2 skipped.
- Right after the merge at `8563c6a`: 26 failed, 682 passed, 2 skipped. All 26 were in
  `test_onshape_mcp_health.py` (10) and `test_onshape_mcp_journal.py` (16), with
  "podman is not available" or "not valid TOML".
- After `d77152a`: 708 passed. `ruff format --check` failed on
  `src/clawake/services/onshape_mcp.py` (missing blank line). The parents `71e0315` and
  `5bd8b64` both passed that check; `5f5687c` fixed it.

No `# noqa` or `# type: ignore` was added.

## Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| C1 | Merge commit present; both issue scopes intact | met | graph below. #39: `preflight_health_command`, `wait_healthy`, health block, `tests/test_onshape_mcp_health.py` all present. #40: `check_journal_config`, `dispatch_journal_directory`, journal goldens all present. `pytest -q tests/test_onshape_mcp*.py` → 93 passed |
| C2 | Full suite, ruff check and ruff format pass | met | checks table |
| C3 | Public hygiene clean | met at the tip, partial in the history | see below |
| C4 | PR description drafted | met | see below |

```
* 1a98614 docs: 📖 redact the host worktree path in the closure K4 prompt
* 5f5687c style: 🎨 restore the blank line the merge dropped in onshape_mcp
* d77152a test: 🧪 align the #39 health and #40 journal tests after the merge
*   8563c6a merge: 🔀 integrate fix/39-mcp-healthcmd into feat/40-goal-journal
|\
| * 5bd8b64 docs: 📋 accept mcp-healthcmd report and mark prompt done
| * 0562ddf docs: 📖 add prompt and report for the MCP health command
| * 85f5da3 docs: 📖 document the Onshape MCP health block, preflight and health wait
| * 5953b6b fix: 🐛 wait for a healthy Onshape MCP container after apply
| * de9473b feat: ✨ preflight the Onshape MCP image for its health command
| * b010ce4 feat: ✨ configurable exec-form health command for the Onshape MCP service
* | 71e0315 docs: 🧾 prompt: integrate #39 into the goal-journal branch (closure K4)
* | a3d96b3 chore(dev): 🧹 remove stale dev containers whose process is gone
```

### C3: hygiene commands

`origin/main` = `85079c8`. Added lines: `git diff origin/main...HEAD | grep '^+'` (16 files,
+2372/−10).

| Check (on added lines) | Result |
|---|---|
| host paths `grep -nE '/home/\|/Users/\|/root/\|/run/user/[0-9]'` | before the fix: one real hit, the closure prompt line 4 (host worktree path with user name). The other hits are grep patterns in the #39 prompt/report and `/home/dev` (container home in `scripts/dev.sh`, already on `main`). After `1a98614`: `grep -cE '/home/<user>\|<user>/\|<project-dir>'` → 0 |
| Discord/numeric IDs `grep -E '\b[0-9]{17,20}\b'` (also 15–20) | 0 |
| digests `grep -oE 'sha256:[0-9a-f]{64}' \| sort -u` | only `sha256:aaaa…a` (fake) |
| Onshape-style 24-hex IDs | 0 |
| e-mails/tokens `grep -E '@…\|ghp_\|xox[bp]-\|Bearer '` | only decorators, `<image@digest>` placeholders and the author line in the closure prompt (line 73) |
| URLs | only `127.0.0.1:8080/health`, `fake-url-marker`, `docs.podman.io` |
| commit messages `git log origin/main..HEAD --format=%B \| grep -E '/home/\|<user>\|[0-9]{17,20}\|sha256:'` | 0 |

Not changed, reported here instead:

- Closure prompt line 73, `author Albert Kos <kickban3000@gmail.com>`. The same identity is
  already public as a commit author on `main` (`git log origin/main --format=%ae`), and the
  prompt requires it. It is not a new leak, and it is the architect's call.
- **History:** `71e0315` (the prompt commit) still has the host path with the user name, and so
  does the removed line in `1a98614` (`git log origin/main..HEAD -p | grep -c '/home/<user>'` → 2).
  A merge commit into `main` would publish it. I was not allowed to rewrite history.

## Behaviour changes for operators

Compared with `feat/40-goal-journal` before the merge, everything below comes from #39 and
nothing new was added here:

- `onshape-mcp --action plan|apply` runs a read-only image preflight per health executable
  first. If it fails, the command exits 1 before any write.
- `apply --execute` waits for `healthy` with a stable restart counter, bounded by
  `start_period + interval*(retries+1) + 30s`. If the wait fails, it exits 1. The old message
  "configured and started" is replaced by "healthy after Ns; credential readiness unverified".
- `--action status` also prints `health:` and `restarts:` (with `--execute`). Without
  `--execute` it prints the two read-only commands instead.
- From #40: `plan`/`apply` parse `config.toml` as TOML, so a non-TOML file now fails the preview
  (exit 2).

## Live E2E request

`recommended`, not `required`. The prompt forbids a deployment. The cluster stays on `4b59c83`
until after the final presentation, and the live check comes with the next rollout. For that
rollout:

1. `clawake onshape-mcp --config <service.json> --action plan`. Expected: preflight passes
   with no output, the planned artifacts are listed, and nothing is written.
2. `clawake onshape-mcp --config <service.json> --action apply --execute`. Expected:
   "Waiting up to …s", then "Onshape MCP service healthy after Ns; credential readiness
   unverified", exit 0.
3. `clawake onshape-mcp --config <service.json> --action status --execute`. Expected:
   `health: healthy`, `restarts: 0`.

## PR description draft (not created)

> **Onshape MCP: goal grant file + dispatch journal (#40) and image-aware healthcheck (#39)**
>
> ### Summary
> - **#40:** optional `dispatch_journal_directory` mounts a private, persistent journal at
>   `/var/lib/onshape-mcp/journal:rw`. `plan`/`apply` check that `config.toml`
>   (`external_token.goal_grant_file` / `dispatch_journal_dir`) and the declared mount agree,
>   and fail the preview otherwise. Only `config.toml` is read; grant, approval, token and
>   journal files stay unread. `apply --execute` checks private owner permissions of the
>   journal directory before writing.
> - **#39:** the health check is configurable (`health.command`, exec form or the legacy
>   shell form) and no longer hard-codes curl. `plan`/`apply` preflight the pinned image
>   (`podman run --rm --pull=never --network=none --entrypoint <exe> … --help`) and refuse an
>   image that cannot run its health command. `apply --execute` waits, with a bound, for a
>   healthy container with no new restarts. `status` shows health and restart count.
> - Without a `health` block and without a journal, the rendered Quadlet is byte-identical
>   to before.
>
> Closes #39
> Closes #40
>
> ### Tests
> `uv run pytest -q` → 708 passed, 2 skipped; `ruff check`, `ruff format --check`,
> `uv lock --check` pass. Unit tests use fake runners only; no live deployment in this PR.
>
> ### Operator notes
> - Switching the service to `"health": {"command": ["/usr/local/bin/onshape-mcp", "healthcheck"]}`
>   requires a (distroless) image that ships the `healthcheck` subcommand. Otherwise the
>   preflight refuses the plan (exit 1). Images without it keep the default curl health
>   command.
> - `config.toml` must be valid TOML. `plan` and `apply` parse it and fail closed (exit 2)
>   otherwise.
> - The preflight needs the pinned image in local Podman storage (`--pull=never`).
>
> 🤖 Generated with [Claude Code](https://claude.com/claude-code)

## Commit list

| SHA | Message |
|---|---|
| `8563c6a` | merge: 🔀 integrate fix/39-mcp-healthcmd into feat/40-goal-journal |
| `d77152a` | test: 🧪 align the #39 health and #40 journal tests after the merge |
| `5f5687c` | style: 🎨 restore the blank line the merge dropped in onshape_mcp |
| `1a98614` | docs: 📖 redact the host worktree path in the closure K4 prompt |
| (next) | docs: 📖 add report for closure K4 (this file) |

## Open points and risks

- **History leak:** to keep the host path out of `main`, merge the PR with **squash** or
  rewrite `71e0315` before pushing. That rewrite is the architect's decision and is not
  allowed for me.
- Order decision (preflight first) means `plan` needs Podman and the local image before
  the config check can run.
- `HealthOnFailure=kill` together with the post-apply wait is only verified with fakes until
  the next rollout.

## Out-of-scope findings

None.
