---
target-repo: edge-robot/clawake
branch: fix/39-mcp-healthcmd
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#39
architecture-ref: clawake/docs/architecture.md#lifecycle-contracts
base-commit: 85079c8 (main after PR #35)
status: issued
---
# Onshape MCP service: per-image health command, image preflight, post-apply health wait (#39)

## Goal

Fix edge-robot/clawake#39 so that `clawake onshape-mcp` can never again start an image whose
health check cannot run, or report success for one.

What happened live on 2026-10-20:
- The quadlet hard-coded `HealthCmd=curl …` with `HealthOnFailure=kill`.
- A distroless onshape-mcp image without `curl` was killed about every 60 s (97 restarts).
- Even so, `--action apply --execute` printed "configured and started".

onshape-mcp now ships `/usr/local/bin/onshape-mcp healthcheck` (edge-robot/onshape-mcp#4):
- exit 0 on HTTP 200, otherwise 1, and 2 on invalid arguments;
- loopback only;
- no shell needed.

## Context

- Read issue #39. It is the specification.
- `src/clawake/services/onshape_mcp.py`:
  - `OnshapeMcpSpec` (pydantic, `extra` forbidden through `clawake.config.BaseModel`);
  - `render_onshape_mcp`, with the quadlet text and the hard-coded `HealthCmd` and `Health*`
    lines;
  - `plan_onshape_mcp`, `validate_runtime_paths`, `probe_native`.
- `src/clawake/cli.py` `onshape_mcp` (`--action plan|apply|status|restart|stop|probe`).
  `apply --execute` runs daemon-reload, then restart, then prints success.
- `tests/test_onshape_mcp.py`, `tests/test_mcp_provisioning.py`.
- Reference for readiness waits with bounded windows: the member readiness model (ADR-003,
  `services/readiness.py`) and how `setup` waits per member.
- The deployed `service.json` (private, not in the repo) has these keys: `name`, `network`,
  `image{repository,tag,digest}`, `credential_directory`, `config_file`, `quadlet_root`,
  `client_container`. It must keep validating unchanged.

## Binding constraints and prohibitions

1. **Spec.** Add an optional `health` block to `OnshapeMcpSpec`:
   ```
   health: {command: [str, ...] (1..16 args, no shell), interval_s: 30, timeout_s: 5,
            retries: 3, start_period_s: 0}
   ```
   - **The default must reproduce today's quadlet byte for byte:**
     `curl --fail --silent http://127.0.0.1:8080/health`, interval 30 s, timeout 5 s,
     3 retries, `HealthOnFailure=kill`.
   - Existing `service.json` files without `health` therefore render identically. Prove this
     with a golden test.
   - `command[0]` must be an absolute path or a bare executable name. Reject empty strings,
     newlines and `%` (systemd specifier injection).
2. **Rendering.**
   - Render `HealthCmd=` from `command` with correct quadlet/systemd quoting.
   - Render `HealthStartPeriod=` only when it is > 0.
   - `HealthOnFailure=kill` stays as it is.
3. **Preflight (`plan` and `apply`).** Check that the image can execute `command[0]`.
   - Run `podman run --rm --network=none --entrypoint <command[0]> <image@digest> --help`.
     If that is not suitable, run `podman image inspect` plus a check that the path exists;
     pick the most robust variant and justify it in the report.
   - A missing executable is a **fail-closed** error before any artifact is written:
     exit 1 and a one-line message that names the image and the command.
   - In `plan`, this is the only podman call. It is read-only and creates no container
     state: use `--rm` and no volumes.
   - Make the check injectable so that tests never call podman.
4. **Post-apply wait.** After `apply --execute`, poll the container health until it is
   `healthy`, or until `start_period_s + interval_s × (retries + 1) + 30 s` has passed.
   - Use `podman inspect --format {{.State.Health.Status}}` and the systemd `NRestarts` value.
   - **Success:** `healthy` with no increase of `NRestarts` during the wait. Print
     `healthy after <n>s`.
   - **Failure:** `unhealthy`, the timeout, or a growing `NRestarts`. Exit 1 with
     `Onshape MCP service not healthy: <state>, restarts +<n>`. Do not roll back automatically.
     The operator decides.
   - `status` also prints the health state and `NRestarts`.
5. **Unchanged:**
   - Clawake never reads credentials or tokens;
   - no change to the member or inventory paths;
   - no change to `probe`;
   - no new dependencies.
6. **Scope.**
   - Allowed: `src/clawake/services/onshape_mcp.py`, `src/clawake/cli.py` (only the
     `onshape_mcp` command), tests, and `docs/manual.md` / `docs/architecture.md` (the MCP
     paragraph only).
   - Synthetic data only.
   - Never run `--execute`, a real `podman` or `systemctl`, or `openclaw`.
7. **Public repo.** No host paths, IDs or deployment values in code, tests, docs or commits.

## Steps

1. Check the preconditions:
   - the branch is `fix/39-mcp-healthcmd` and HEAD is `85079c8`;
   - the worktree is clean apart from this prompt;
   - record the base test count.
2. Write the failing tests first:
   - golden render: no `health` gives today's exact quadlet;
   - a custom `command` renders correctly, including quoting;
   - injection attempts (`%`, a newline, an empty argument) are rejected;
   - the preflight refuses a missing executable before anything is written;
   - post-apply: `healthy` gives success; `unhealthy`, a timeout or growing restarts give
     exit 1 and the exact message;
   - `status` shows the health state and restarts.
3. Implement. Make one commit per concern: spec + render, preflight, post-apply wait + status.
   Each commit carries `Refs: edge-robot/clawake#39`.
4. Update `docs/manual.md`: the `service.json` `health` block and the example for
   `onshape-mcp healthcheck`.
5. Run the checks and write the report.

## Acceptance criteria and tests

- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are green.
- [ ] Mutation checks. Each mutation must make a test fail:
  - drop the preflight;
  - treat `unhealthy` as success;
  - ignore `NRestarts`;
  - change the default `HealthCmd` (the golden test).
- [ ] No deployment data in the diff (`grep -E '[0-9]{17,20}|/home/'` is empty).

## Branch and worktree rules

Commit only to `fix/39-mcp-healthcmd` in this checkout. No push, no rebase, and no other
branches or worktrees. Commit this prompt unchanged together with your report. Leave the
worktree clean.

## Expected report / handoff format

`docs/prompts/2026-10-20-mcp-healthcmd.report.md` in the agent-definition format, with:

- the checks table;
- tests before and after;
- the mutation results;
- the exact operator messages;
- the rendered quadlet health lines for the default and for
  `["/usr/local/bin/onshape-mcp","healthcheck"]`;
- the preflight variant and its justification;
- the commit list with trailers.

Set `e2e-request: required`. The architect will:
1. switch the deployed `service.json` to the binary health command;
2. roll out the distroless image;
3. watch it stay `healthy`.

The rollout is held until clawonshape#17 is done.
