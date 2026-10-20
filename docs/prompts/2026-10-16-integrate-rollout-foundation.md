---
target-repo: edge-robot/clawake
branch: integrate/rollout-foundation
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#25
architecture-ref: clawake/docs/architecture.md
base-commit: 08f5a11 (4da3957 demo/gateway-native-read-20261014 + devcontainer commit)
merge-source: feat/agent-mcp-provisioning @ e505969
status: done
---
# Integrate demo/gateway-native-read and feat/agent-mcp-provisioning

## Goal

Produce one integration branch that contains both development lines of Clawake, with all
checks green and **no behaviour change for the deployed Onshape cluster**. This branch
becomes the base for the operator-ergonomics work (#24 one-step rollout, #23 readiness in
`status`, #5 no restarts of unchanged members).

## Context

- Issue: edge-robot/clawake#25 (full description of both lines and the overlap).
- Both lines branch from `feat/robotics-team` (cefbfe5).
  - **Ours / base** `demo/gateway-native-read-20261014` (4da3957). The live cluster is
    operated from this line. Unique commits:
    - 68679f3: isolated self-hosted Onshape MCP runtime, `services/onshape_mcp.py`;
    - 9b152b6: explicit global tool grants without widening the host policy (#14);
    - 4da3957: optional `state_path`; native MCP lifecycle via the instance flags
      `onshape_mcp` / `onshape_mcp_enabled`.
  - **Theirs** `feat/agent-mcp-provisioning` (e505969), which contains
    `feat/reusable-runtime-foundation`. Unique commits:
    - 843e3fd: generic services and shared deployment plans;
    - c8dbf38: service dependency and readiness checks;
    - aac0304: service readiness and pinned upgrades;
    - 95c7601: private service mounts and pinned v2 plugin sync;
    - 03445a7: unmanaged grants and capability removal;
    - e505969: generic `mcp_servers` provisioning (`McpServerSpec`) and a drift guard
      `Managed field changed outside Clawake`.
- A trial merge (`git merge-tree`) gives content conflicts in exactly three files:
  - `src/clawake/services/runtime_config.py`
  - `tests/test_gateway_config.py`
  - `docs/inventory-v2.md`

  `cli.py`, `config.py`, `gateway_config.py` and `templates/quadlet/openclaw.container.j2`
  auto-merge, but they touch the same topics, so review them as carefully as the conflicts.
- Two conflicts in `runtime_config.py` matter most:
  1. **MCP desired state.**
     - Ours: `desired.update(mcp)` from the `onshape_mcp` flags.
     - Theirs: `desired[f"mcp/servers/{name}"]` from `settings.mcp_servers`.
  2. **Existing-field reconciliation.**
     - Ours: adopts a disabled hosted Onshape connector
       (`https://fs-mcp.labs.onshape.app/mcp`, `enabled: false`).
     - Theirs: raises `Managed field changed outside Clawake` when a previously managed
       field was changed externally.
- The deployed inventory is `/workspaces/clawonshape/deploy/team.json`. It is read-only and
  uses `onshape_mcp: true` and `onshape_mcp_enabled: true` on the Engineer instance.
- Architecture: `docs/architecture.md`, `docs/project-overview.md`, `docs/inventory-v2.md`.
- Ownership boundary (ADR: `clawonshape/docs/ADR.md`, review 2026-10-15; mirrored at
  `/workspaces/clawonshape/docs/ADR.md`):
  - The OpenClaw native MCP client is the transport.
  - Clawake provisions its configuration.
  - Clawake never handles OAuth tokens.

## Binding constraints and prohibitions

1. **Merge, do not rebase or squash.** Run `git merge --no-ff feat/agent-mcp-provisioning`
   on `integrate/rollout-foundation`. The merge itself prompts for permission; that is
   expected. The merge commit message lists every conflict and how it was resolved.
2. **The deployed inventory must keep its exact behaviour.**
   - For `/workspaces/clawonshape/deploy/team.json`, the rendered Quadlet units and the
     desired OpenClaw runtime configuration (including `mcp/servers/*`, grants and
     mounts) after the merge must equal the output at base 08f5a11.
   - Any difference is a finding you list in the report. Never silently accept a
     difference.
3. **The instance flags stay supported.** `onshape_mcp` / `onshape_mcp_enabled` keep
   working unchanged. `mcp_servers` may coexist with them.
   - If both configure the same server name (e.g. `onshape`), validation must reject it
     with a clear error. Never let one silently override the other.
   - Do not remove or deprecate either model; that is an architecture decision you only
     propose in the report.
4. **The drift guard must not break the live cluster.** Keep the `Managed field changed
   outside Clawake` guard from theirs only if the hosted-connector adoption from ours still
   works, and do not let it fire on an unchanged re-run.
   - Prove both with tests.
   - If the two cannot be reconciled without a design decision, set `status: blocked` for
     that point, describe the options and continue with the rest.
5. **Grant handling.** Both 9b152b6 (explicit global tool grants, no widening of the host
   policy) and 03445a7 (preserve unmanaged grants, safe capability removal) must hold after
   the merge, each proven by its existing tests plus one combined test.
6. **Public repository.** `edge-robot/clawake` is public.
   - Never copy `deploy/team.json`, Discord IDs, host paths, hostnames or anything from
     `/workspaces/clawonshape` into the repo.
   - Regression fixtures are synthetic: same structure, fake IDs such as `000000000000000001`,
     paths under `/srv/example`.
   - Do the before/after comparison against the real deployed inventory only in a
     scratch directory outside the repo (e.g. `/tmp/golden/`) and only describe the result
     in the report.
7. Do not change any other behaviour, do not start on #24/#23/#5, and do not reformat
   untouched files. No new dependencies.
8. Never run `--execute`, `podman`, `systemctl`, `openclaw` or `gh` (not available anyway).

## Steps

1. Verify the preconditions:
   - `git branch --show-current` is `integrate/rollout-foundation`;
   - `git log -1 --format=%h` is `08f5a11`;
   - `git rev-parse feat/agent-mcp-provisioning` starts with `e505969`.

   Then run the checks at the base and record the test count.
2. **Golden at base.** Write a small throwaway script in `/tmp`, not in the repo. It loads
   `/workspaces/clawonshape/deploy/team.json` through Clawake's own loader and writes the
   following to `/tmp/golden/base/`:
   - the rendered Quadlet files;
   - the desired runtime configuration per instance;
   - the result of `uv run clawake validate -c /workspaces/clawonshape/deploy/team.json`.

   Use the existing render and runtime-config functions, never the host. Also record
   `sha256sum /workspaces/clawonshape/deploy/team.json`.
3. Merge `feat/agent-mcp-provisioning` and resolve the three conflicts according to the
   constraints. Review the auto-merged files listed above.
4. Add tests:
   - a synthetic inventory fixture that mirrors the deployed shape (two instances, one with
     `onshape_mcp` + `onshape_mcp_enabled`, one plugin with `localMcp`, a `state_path`);
   - a regression test: the rendered output for that fixture is stable (golden files under
     `tests/fixtures/`);
   - conflict tests: instance flags and `mcp_servers` naming the same server → validation
     error;
   - drift guard vs. hosted-connector adoption, and an unchanged re-run, which must be a
     no-op;
   - combined grants test (constraint 5).
5. **Golden after merge.** Regenerate into `/tmp/golden/merged/` and `diff -r` against
   base. Also confirm that the hash of `deploy/team.json` did not change during your run.
   If it did, say so; the file belongs to a live operator.
6. Run all checks; commit in logical steps (merge commit, then tests, then fixes if any).
7. Write the report.

## Acceptance criteria and tests

- [ ] Merge commit on `integrate/rollout-foundation` with both parents (08f5a11, e505969)
      and a conflict-resolution list in the message.
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .` (changed
      files), `uv run pytest -q` and `uv lock --check` all green. Test count is at least
      base count + the tests unique to theirs.
- [ ] `diff -r /tmp/golden/base /tmp/golden/merged` is empty, or every difference is
      listed and justified in the report.
- [ ] Tests for constraints 3, 4 and 5 exist and pass, each named in the report.
- [ ] No real deployment data in the repo: no value from `/workspaces/clawonshape/deploy/team.json`
      (Discord IDs, host paths, container or host names) appears in `git diff 08f5a11`.
      Check this explicitly and state how in the report.
- [ ] `docs/inventory-v2.md` describes both MCP models and the conflict rule.
      Architecture docs are read-only; propose changes in the report.

## Branch and worktree rules

- Commit only to `integrate/rollout-foundation` in this checkout. No other branches, no
  worktrees, no push. Do not touch `feat/*` or `demo/*` branches; you only read them.
- The worktree `<workspace>/clawake-integrate` is temporary. The
  architect removes it after review and push; leave it clean (no untracked files
  except ignored ones). Commit this prompt file unchanged together with your report.

## Expected report / handoff format

`docs/prompts/2026-10-16-integrate-rollout-foundation.report.md`, using the format from
your agent definition. Additionally include:

- the golden comparison: the `deploy/team.json` hash before and after, and the diff
  result;
- a conflict table: file, hunk, ours, theirs, resolution, test;
- proposals for `docs/architecture.md` / ADR, e.g. whether `onshape_mcp` flags should
  later be expressed via `mcp_servers`.

Set `e2e-request: required`. Before the operator checkout switches to this branch, the
architect has to run `setup`, `sync-plugins` and `onshape-mcp` previews against the live
cluster.
