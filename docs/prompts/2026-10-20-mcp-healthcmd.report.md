---
prompt: docs/prompts/2026-10-20-mcp-healthcmd.md
branch: fix/39-mcp-healthcmd
commits: b010ce4..85f5da3 (plus the commit adding the prompt and this report)
status: done
e2e-request: required
---
# Report: Onshape MCP per-image health command, image preflight, post-apply health wait (#39)

## Summary

`clawake onshape-mcp` no longer depends on `curl` in the image, refuses images that cannot
run their health check, and no longer reports success for a container that is killed in a
loop:

- **Spec + render.** `OnshapeMcpSpec.health` (optional): `command`, `interval_s`,
  `timeout_s`, `retries`, `start_period_s`. Without it, the quadlet is byte-identical to
  the base (golden fixture captured from `85079c8`). A custom command renders in Podman's
  exec form `HealthCmd=["CMD",...]`, so no shell is needed.
- **Preflight.** `plan` and `apply` (preview and `--execute`) first run
  `podman run --rm --pull=never --network=none --entrypoint <exe> <image@digest> --help`
  for each executable the health check needs. A missing executable stops with exit 1
  before anything is written.
- **Health wait.** After `apply --execute` (daemon-reload, restart), Clawake polls the
  Podman health state and systemd `NRestarts` within a bounded window. Success is `healthy`
  with an unchanged restart counter; anything else exits 1. There is no automatic rollback.
- **Status.** `--action status --execute` also prints `health:` and `restarts:`.

Base: `85079c8`, 621 passed, 2 skipped. Now 662 passed, 2 skipped (41 new tests).

### OpenClaw / Podman facts used

No OpenClaw behaviour is involved. The Podman facts come from Podman's docs and source:

- `podman run` exit status 125 = Podman error, 126 = contained command cannot be invoked,
  127 = contained command not found; any other code is the command's own
  (https://docs.podman.io/en/latest/markdown/podman-run.1.html, "Exit Status").
- `--pull=never` uses only local storage (same page, `--pull`).
- `--health-cmd` parsing (`pkg/specgenutil/specgen.go`, `makeHealthCheckFromCli`): a
  non-JSON string becomes `CMD-SHELL` (run through `/bin/sh -c`). A JSON array whose
  first element is `CMD` is kept as exec form. A single-element JSON array without a
  keyword is turned into `CMD-SHELL`, which is why Clawake always writes the explicit
  `"CMD"`.
- Quadlet (`pkg/systemd/quadlet/quadlet.go`, `handleHealth`) passes the `HealthCmd=`
  value as one `--health-cmd` argument. `unitfile.Lookup` only strips one surrounding pair
  of `"` (a JSON array starts with `[`). `escapeWords` (`pkg/systemd/parser/split.go`)
  quotes whitespace, quotes and backslashes in `ExecStart`, but **not** `%` or `$`. systemd
  would expand those, so they are rejected.

These are read from upstream `main`. The behaviour of the Podman version installed on the
live host is **unverified**.

## Changed files

`git diff --stat 85079c8..85f5da3`:

| file | purpose |
|---|---|
| `src/clawake/services/onshape_mcp.py` (+247/−) | `OnshapeMcpHealth`, `DEFAULT_HEALTH_COMMAND`, exec-form rendering, `McpProbes`/`default_mcp_probes`, `preflight_health_command`, `wait_healthy`, `read_health_status`, `read_restart_count`, `health_wait_window` |
| `src/clawake/cli.py` (+29/−8, `onshape_mcp` only) | preflight before `plan_onshape_mcp`; health wait replaces the "configured and started" success line; status prints health and restarts |
| `tests/test_onshape_mcp_health.py` (+542) | render, validation, preflight, wait and status tests with `FakeMcpHost`/`FakeSystemd` |
| `tests/fixtures/onshape-mcp/clawonshape-mcp.container`, `clawonshape-mcp-private.network` | golden quadlet rendered by the base code from a synthetic service file with the deployed key set |
| `docs/manual.md` (+57) | new section "Onshape MCP service health (`onshape-mcp`)" |

`docs/architecture.md` has no MCP paragraph, so it was not changed.

## Commits

| sha | subject | trailers |
|---|---|---|
| b010ce4 | `feat: ✨ configurable exec-form health command for the Onshape MCP service` | Refs: edge-robot/clawake#39, Prompt: docs/prompts/2026-10-20-mcp-healthcmd.md, Agent: py-clawake-dev, Co-Authored-By |
| de9473b | `feat: ✨ preflight the Onshape MCP image for its health command` | same |
| 5953b6b | `fix: 🐛 wait for a healthy Onshape MCP container after apply` | same |
| 85f5da3 | `docs: 📖 document the Onshape MCP health block, preflight and health wait` | same |
| (next) | `docs: 📖 add prompt and report for the MCP health command` | same |

## Checks

| command | result |
|---|---|
| `uv sync --locked` | pass (`Checked 21 packages`) |
| `uv run ruff check .` | pass (`All checks passed!`) |
| `uv run ruff format --check .` | pass (`53 files already formatted`) |
| `uv run pytest -q` | pass, 662 passed, 2 skipped (base 621 + 41 new) |
| `uv lock --check` | pass (`Resolved 29 packages`) |
| `git diff 85079c8 \| grep '^+' \| grep -E '[0-9]{17,20}\|/home/'` | empty (rc 1) |
| MCP tests with a scratch plugin that makes `subprocess.run` of podman/systemctl/journalctl/openclaw raise | 47 passed |

## Tests before and after

"Before" is the base for the render tests and the previous commit's sources (scratch tree
via `git archive` + `PYTHONPATH`) for the later tests.

| test(s) | before | after |
|---|---|---|
| all of `test_onshape_mcp_health.py` on base `85079c8` | collection error (`ImportError`: `DEFAULT_HEALTH_COMMAND`, `McpProbes`) | pass |
| `test_service_json_without_health_renders_todays_quadlet_byte_for_byte` | golden captured from the base renderer; equal by construction | pass |
| `test_default_health_block_reproduces_the_legacy_health_lines`, `test_binary_health_command_renders_exec_form_without_shell`, `test_custom_health_timing_and_quoting`, `test_unsafe_health_commands_are_rejected` (14 cases), `test_health_timing_bounds_and_unknown_keys` (5) | import error | pass |
| preflight tests (`test_plan_preflight_is_the_only_podman_call_and_is_read_only`, `test_default_shell_form_preflights_the_shell_and_curl`, `test_missing_health_executable_fails_closed_before_any_write` ×4, `test_preflight_podman_error_fails_closed`, `test_help_exit_code_of_the_command_itself_counts_as_present`) on commit `b010ce4` | import error (`McpProbes`) | pass |
| on commit `de9473b`: `test_apply_waits_until_healthy`, `test_apply_reports_unhealthy`, `test_apply_reports_growing_restarts_even_when_healthy`, `test_apply_reports_timeout`, `test_apply_fails_closed_when_the_restart_count_is_unreadable`, `test_status_prints_health_and_restarts`, `test_status_preview_runs_nothing`, `test_status_of_a_failed_unit_still_shows_health_and_exits_1` | 8 failed | pass |
| on commit `de9473b`: `test_apply_tolerates_a_container_that_is_not_there_yet`, `test_apply_preview_writes_nothing_and_waits_for_nothing` | pass (guards: old code printed success / preview unchanged) | pass |
| existing `tests/test_onshape_mcp.py`, `tests/test_mcp_provisioning.py` | pass | pass, unchanged |

## Mutation results

Run with a scratch script (`mutate.py`) that applies one replacement, runs
`tests/test_onshape_mcp_health.py` and restores the file:

| mutation | result |
|---|---|
| drop the preflight (`preflight_health_command(...)` → `pass`) | 8 failed: all preflight tests and `test_apply_preview_writes_nothing_and_waits_for_nothing` |
| treat `unhealthy` as success | 1 failed: `test_apply_reports_unhealthy` |
| ignore `NRestarts` (`if restarts != 0` → `if False`) | 1 failed: `test_apply_reports_growing_restarts_even_when_healthy` |
| change the default `HealthCmd` (`--fail` → `-f`) | 2 failed: the golden test and `test_default_health_block_reproduces_the_legacy_health_lines` |

## Rendered quadlet health lines

Default (no `health` block; byte-identical to the base):

```text
HealthCmd=curl --fail --silent http://127.0.0.1:8080/health
HealthInterval=30s
HealthTimeout=5s
HealthRetries=3
HealthOnFailure=kill
```

`"health": {"command": ["/usr/local/bin/onshape-mcp", "healthcheck"]}`:

```text
HealthCmd=["CMD","/usr/local/bin/onshape-mcp","healthcheck"]
HealthInterval=30s
HealthTimeout=5s
HealthRetries=3
HealthOnFailure=kill
```

With `start_period_s > 0`, `HealthStartPeriod=<n>s` comes before `HealthOnFailure=kill`.

## Preflight variant and justification

Chosen: `podman run --rm --pull=never --network=none --entrypoint <exe> <image@digest> --help`,
60 s timeout, once per required executable (`command[0]`; for the legacy shell-form
default also `/bin/sh`, since Podman runs that check through `/bin/sh -c`).

- **Robust classification.** Podman reports the cases with dedicated exit codes: 126
  (not invokable) and 127 (not found) are the failure Clawake looks for; 125 is a Podman
  error. Any other exit code comes from the executable itself. A `--help` that returns 2
  still proves that the binary exists and runs. The check does not depend on what
  `--help` prints.
- **It tests what the health check will do.** It uses the same image, the same resolution
  of a bare name through the image's `PATH`, and the same loader and libc. `podman image
  inspect` plus a path check cannot resolve a bare name, cannot see a broken ELF
  interpreter, and needs `podman image mount` (rootless: `podman unshare`) to look into
  the layers. That is more mutation-prone and less accurate.
- **Read-only.** `--rm` removes the container. There are no volumes, so the credential
  directory and config are never mounted. `--network=none` gives no network.
  `--pull=never` prevents a registry pull in `plan`: the MCP images are built locally
  (`localhost/...`). An image that is not in local storage fails closed with exit 125 and
  the message below.
- The default user and the image's writable layer are discarded with the container.
- The check runs through the injectable `McpProbes.run` (default
  `readiness.run_command`), so tests never call podman.

## Exact operator messages

Preflight (stderr, exit 1, before any artifact write or systemd call):

- `Onshape MCP image <repo>:<tag>@<digest> cannot run health command <exe>: executable missing or not runnable (podman exit 126|127)`
- `Onshape MCP preflight could not run image <repo>:<tag>@<digest> for health command <exe>: podman exit 125 (the image must be in local storage)`
- `Onshape MCP preflight for image <ref> and health command <exe> timed out after 60s`
- `Onshape MCP preflight failed: podman is not available`

Apply `--execute` (after daemon-reload and restart):

- stdout: `Waiting up to <w>s for Onshape MCP health` (`<w>` = `start_period_s + interval_s × (retries + 1) + 30`; 150 with the defaults)
- success (stdout, exit 0): `Onshape MCP service healthy after <n>s; credential readiness unverified`
- failure (stderr, exit 1): `Onshape MCP service not healthy: <state>, restarts <±n>`, where `<state>` is:
  - `unhealthy`;
  - `timeout after <w>s (last state <starting|unknown|...>)`;
  - the current health state when the restart counter changed (e.g. `healthy, restarts +2`, which is the live #39 pattern);
  - `restart count unavailable`, rendered as `restarts +?`.

The old line `Onshape MCP service configured and started; credential readiness unverified`
is gone.

Status `--execute` (after the systemd status lines): `health: <state|unknown>` and
`restarts: <n|unknown>`. The status preview additionally lists
`podman inspect --format {{.State.Health.Status}} <name>` and
`systemctl --user show --property=NRestarts --value <name>.service`.

## Acceptance criteria

| criterion | status | evidence |
|---|---|---|
| Optional `health` block with the given fields and bounds | met | `OnshapeMcpHealth`; bounds and unknown-key tests |
| Default reproduces today's quadlet byte for byte; old `service.json` validates unchanged | met | golden test with the deployed key set (synthetic values) |
| `command[0]` absolute or bare; empty, newline, `%` rejected | met | 14 rejection cases (also `$`, tab, relative, leading `-`, 17 args) |
| Render `HealthCmd=` with correct quoting; `HealthStartPeriod` only > 0; `HealthOnFailure=kill` kept | met | `test_custom_health_timing_and_quoting` (JSON round-trip of quotes, backslash, `&`, `?`) |
| Preflight in `plan` and `apply`, fail closed before any write, exit 1, one line naming image and command | met | preflight tests; quadlet root absent, no systemd call |
| In `plan`, the preflight is the only podman call, read-only, `--rm`, no volumes | met | `test_plan_preflight_is_the_only_podman_call_and_is_read_only` asserts the full call list |
| Injectable check; tests never call podman | met | `McpProbes`; guard plugin run |
| Post-apply wait with the given window, `healthy` + no `NRestarts` increase, exact messages, no rollback | met | wait tests; `test_apply_reports_unhealthy` asserts no stop after failure |
| `status` prints health state and `NRestarts` | met | status tests |
| Unchanged: no credential reads, member/inventory paths, `probe`, no new dependencies | met | `probe_native` untouched; `uv.lock` unchanged |
| All five checks green | met | Checks table |
| Mutation checks | met | table above |
| No deployment data in the diff | met | grep empty |

## Behaviour changes for operators

- New optional `health` block in the service file. Old files without it render
  identically. Note that an invalid `health` block is reported like every other spec error,
  with the existing generic `Invalid Onshape MCP service specification` (exit 2); the
  pydantic detail is not shown.
- `--action plan` and `--action apply` (also without `--execute`) now run one or two
  `podman run --rm` preflights. The image must be in local storage; otherwise exit 1.
- `--action apply --execute` blocks for up to the wait window (150 s by default) and exits
  1 unless the container becomes `healthy` without restarts. A failure leaves the new
  quadlet deployed and the service running or restarting. Nothing is stopped or rolled back.
- `--action status` preview prints two additional command lines. `--execute` prints
  `health:` and `restarts:`; its exit code is still that of `systemctl status`.
- `restart`, `stop` and `probe` are unchanged. The `apply --execute` systemd-failure path
  is unchanged (exit 2).

## Live E2E request

Held until clawonshape#17 is done (per prompt). Use the deployed service file in its
private location (`<service.json>` below):

1. Preview with the current (curl) image, no `health` block:
   `clawake onshape-mcp -c <service.json> --action plan`
   Expected: two preflight runs (`/bin/sh`, `curl`) succeed, then no changed artifacts
   are listed (the quadlet is byte-identical).
2. Add `"health": {"command": ["/usr/local/bin/onshape-mcp", "healthcheck"]}` and switch
   `image` to the distroless digest (image built and in local storage):
   `clawake onshape-mcp -c <service.json> --action plan`
   Expected: one preflight run, exit 0. The `.container` artifact is listed as changed.
   For a negative check, the old curl block with the distroless image must give
   `Onshape MCP image ... cannot run health command curl: executable missing or not runnable (podman exit 127)`
   and exit 1.
3. `clawake onshape-mcp -c <service.json> --action apply --execute`
   Expected: `Waiting up to 150s for Onshape MCP health`, then
   `Onshape MCP service healthy after <n>s; credential readiness unverified`, exit 0.
   Check that the deployed quadlet contains
   `HealthCmd=["CMD","/usr/local/bin/onshape-mcp","healthcheck"]`.
4. After at least 5 minutes: `clawake onshape-mcp -c <service.json> --action status --execute`
   Expected: `health: healthy` and the same `restarts:` value as directly after step 3.
   Also check `podman inspect --format '{{json .State.Health}}' clawonshape-mcp` for exit-0
   probe log entries.
5. Re-run step 3 unchanged. Expected: no artifact listed, restart, healthy again (apply
   always restarts; idempotence of the restart is out of this prompt's scope, see below).

## Open points and risks

- **Podman version.** The exec-form JSON parsing and the 125/126/127 exit codes are
  verified against upstream `main` and the current docs, not against the host's Podman
  version.
- **Exit code 127 from `--help`.** If an executable itself returned 126/127 for
  `--help`, it would be classified as missing (fail closed, false negative). This is
  unlikely for `curl`/`onshape-mcp`.
- **The preflight needs a local image.** `--pull=never` means a digest that is not yet in
  local storage blocks `plan`/`apply` (exit 1, 125 message). The operator runs
  `podman pull` or builds the image first. This is intentional, so that `plan` stays
  read-only and offline.
- **Default shell form.** The default keeps running through `/bin/sh`, for byte-identity.
  Writing the default list explicitly in `health.command` also keeps the shell form.
- **Wait overshoot.** Each probe has its own timeout (≤ 10 s, at least 1 s), so the wait
  can exceed the window by at most two probe timeouts if Podman or systemd hang.
- **`apply --execute` always restarts**, even with no changed artifact (pre-existing
  behaviour, unchanged). The idempotence rule would ask for no restart; not in scope.
- `restarts` is compared with the value read right after the restart. A manual start by
  someone else during the wait (counter reset) is also reported as a failure (negative
  delta, e.g. `restarts -3`).

## Out-of-scope findings

- `src/clawake/services/onshape_mcp.py:372` and `:381`: `probe_native` calls
  `subprocess.run` directly instead of through the runner abstraction (tests patch
  `subprocess.run` globally). Not changed (prompt: no change to `probe`).
- `src/clawake/cli.py:1358`: spec errors hide the pydantic reason (exit 2, generic text),
  so an operator does not see which `health` field is wrong. Not changed.
- `src/clawake/cli.py:1408`: a failing daemon-reload/restart in `apply --execute` raises
  `typer.BadParameter` (exit 2, usage-error framing) instead of exit 1. Not changed.
- `docs/inventory-v2.md:113`: the "Focused Onshape MCP exception" paragraph lists
  `plan|apply|status|restart` (no `stop`/`probe`) and does not mention the health block,
  preflight or wait. That file is outside the allowed doc scope. Not changed.
