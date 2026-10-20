---
target-repo: edge-robot/clawake
branch: feat/23-member-readiness
worktree: <workspace>/clawake-integrate (inside the devcontainer: /workspaces/clawake)
issue: edge-robot/clawake#23
architecture-ref: clawake/docs/ADR.md#adr-003-member-readiness-model-and-one-step-rollout
base-commit: 0924ac3 (integrate/rollout-foundation 71e8dc9 + ADR log)
status: issued
---
# P1: Member readiness model and `status` (ADR-003, part 1 of 3)

## Goal

Give every v1 member one shared readiness state (`stopped`, `starting`, `ready`,
`degraded` or `failed`) and make `clawake status` report it. Later commands (P2: `setup` /
`sync-plugins`, P3: `rollout`) will reuse it. Add a bounded `--wait` to `status`.

## Context

- **Decision:** `docs/ADR.md`, ADR-003 (accepted). Read it completely; this prompt
  implements decision items 1, 2 and 6.
- **Issue:** edge-robot/clawake#23. Umbrella: #24.
- **Current code:**
  - `src/clawake/cli.py` `status_quadlets()` (v1 path) maps `systemctl --user status`
    rc 0 to `state="healthy"` via `_status_state()`. A running unit therefore reads as
    healthy while the gateway is still starting.
  - `src/clawake/services/runtime_upgrade.py` `wait_for_health()` and `_gateway_host_url()`
    already probe the gateway over HTTP from the host, using `InstanceSpec.health`
    (`HealthSpec`, default path `/health`). Only `upgrade` uses them.
  - `src/clawake/services/resources.py` `readiness()` / `wait_ready()` cover v2
    resources via `podman inspect`. Do not change v2 behaviour in this prompt.
- **Facts verified by the architect on 2026-10-16/19** (OpenClaw 2026.9.9, read-only):
  - **Gateway readiness endpoint.** `GET <gateway>/readyz` → `200 {"ready":true}` on
    both deployed members. Per docs.openclaw.ai/cli/gateway/query, `/readyz` "stays red
    while startup plugin sidecars, channels, or configured hooks are still settling".
    `/healthz` and `/health` are liveness only.
  - **Installed plugins.** `openclaw plugins list --json` returns a top-level object with
    a `plugins` array. Per plugin it has `id` (str), `version` (str), `enabled` (bool) and
    `status` (str, e.g. `"loaded"`), among others. There is **no sha256 or integrity
    field**.
  - **Channel state.** `openclaw channels status --json` *without* `--probe` reads the
    gateway's recorded channel lifecycle and secret-free metadata; it runs no live
    checks (docs.openclaw.ai/cli/channels). The docs name the per-account fields
    `running` and `lifecycle` (e.g. `"blocked"`). While the gateway is starting it returns
    `{"status":"starting","startupPhase":"…"}`. The exact JSON shape is **unverified
    live**. Never use `--probe`.
- **Pinned plugins** are in the inventory (`instance.plugins`, archive path + sha256).
  The package version is inside the pinned `.tgz` (`package/package.json`).

## Binding constraints and prohibitions

1. **States and their rules.** Compute them per member, in this order:
   - `stopped`: the unit is inactive or dead.
   - `failed`: the unit failed, or `--wait` ran out while the member was not `ready` or
     `degraded`.
   - `starting`: the unit is active and `/readyz` is not 200 or not reachable.
   - `degraded`: `/readyz` is 200, but at least one of these holds:
     - a pinned plugin is missing from `plugins list`, is not `enabled`, its `status` is
       not `loaded`, or its `version` differs from the version in the pinned archive;
     - channel checking is enabled for the member and a channel account reports
       `running: false` or a `lifecycle` other than a running one;
     - any of these checks could not be parsed. Unknown never counts as healthy.
   - `ready`: none of the above.

   Every non-`ready` state carries machine-readable `reasons`, as snake_case codes with a
   short detail.
2. **Default readiness path is `/readyz`.** Change `HealthSpec.path` to `/readyz`, so that
   `upgrade` also becomes stricter. Rendering must not change: `HealthSpec` is not
   rendered for v1, which the golden test in `tests/test_rollout_foundation.py` proves.
3. **Channel checking is opt-in per member.** Add an inventory field, e.g.
   `readiness: {channels: false, start_window_seconds: 180}`, with those defaults and
   documented in `docs/inventory-v2.md`. Use `channels status --json` only, never
   `--probe`.
4. **Probes are read-only and secret-free.**
   - Allowed are HTTP GET from the host to the published gateway port (reuse
     `_gateway_host_url`) and `podman exec <container> openclaw plugins list --json` /
     `openclaw channels status --json`.
   - Never read container env, never print raw command output, and never include tokens
     or URLs with credentials in `reasons`.
   - Every subprocess and HTTP call goes through an injectable seam, so tests use fakes.
   - Use per-probe timeouts of at most 10 s.
5. **Bounded waiting.** `status --wait` polls until every selected member is `ready` or
   `degraded`, or until `start_window_seconds` has passed (optional override
   `--timeout <s>`). There is no unbounded loop.
6. **Compatibility of `status` output.**
   - JSON: keep the existing keys. Add `readiness` and `reasons` per instance, and
     `plugins: [{id, pinned_version, installed_version, status, enabled}]`.
   - The existing `state` key keeps its current meaning, documented as deprecated in
     favour of `readiness`.
   - Text output adds `readiness=<state>` and one `reason:` line per reason.
   - Exit code: non-zero if any member is `stopped` or `failed`. With `--wait`, also
     non-zero if any member is not `ready` or `degraded` at the end. `degraded` alone
     exits 0 but is clearly marked.
7. Put the shared model in a new module, e.g. `src/clawake/services/readiness.py`, with a
   function that later prompts can call (`probe_member(instance, …) -> MemberReadiness`,
   `wait_members_ready(instances, timeout) -> list[MemberReadiness]`). Do **not** change
   `setup`, `restart`, `sync-plugins` or `upgrade` behaviour beyond item 2; that is P2.
8. Public repository: synthetic data only (see `docs/ADR.md` preamble). No new
   dependencies. Never run `--execute`, `podman` or `openclaw` yourself.

## Steps

1. Verify the preconditions: the branch is `feat/23-member-readiness` and HEAD is
   `0924ac3`. Run the checks and record the base test count.
2. Write failing tests first for the state table (item 1). Use fakes for: the unit state;
   `/readyz` responses (200, 503, connection refused, timeout); `plugins list` JSON
   (matching, version mismatch, disabled, `status != loaded`, missing, malformed); and
   `channels status` JSON (running, `running:false`, `lifecycle:"blocked"`, the
   `{"status":"starting"}` form, malformed).
3. Implement `readiness.py`, the inventory field, the `HealthSpec` default and the
   `status` changes.
4. Add tests:
   - `status --format json` keys;
   - exit codes;
   - `--wait` with a fake clock: it reaches `ready`, it times out to `failed`, and it
     never exceeds the window;
   - no secrets in `reasons`;
   - the golden rendering is unchanged.
5. Document in `docs/inventory-v2.md` and `docs/manual.md` (the `status` section). Do not
   edit `docs/architecture.md`; propose text in the report.
6. Run the checks, commit and write the report.

## Acceptance criteria and tests

- [ ] All five states and every rule in item 1 are covered by named tests, each written to
      fail first.
- [ ] `HealthSpec.path` defaults to `/readyz`; the golden test is unchanged; `upgrade`
      tests are still green.
- [ ] `status` JSON is backward compatible (old keys present) and contains `readiness`,
      `reasons` and `plugins`.
- [ ] `--wait` is bounded (fake-clock test) and exits as specified.
- [ ] Channel checking is off by default and uses no `--probe` (asserted on the fake
      runner's argv).
- [ ] `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run pytest -q` and `uv lock --check` are all green.
- [ ] No deployment data in the diff.

## Branch and worktree rules

- Commit only to `feat/23-member-readiness` in this checkout. No other branches, no
  worktrees, no push, no rebase. Commit this prompt file unchanged together with your
  report.
- Leave the worktree clean afterwards. P2 continues on its own branch from this one.

## Expected report / handoff format

`docs/prompts/2026-10-19-member-readiness.report.md` in the agent-definition format.
Include:

- a table mapping each state rule to its test;
- example `status` text and JSON output from a synthetic fixture with fakes;
- the exact `podman exec` / HTTP probe argv the architect should expect live;
- any field of `plugins list` / `channels status` you had to assume.

Set `e2e-request: required`. The architect will run `status` and `status --wait` against
the live cluster, including a member restart if the operator allows it.
