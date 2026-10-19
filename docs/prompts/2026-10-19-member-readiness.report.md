---
prompt: docs/prompts/2026-10-19-member-readiness.md
branch: feat/23-member-readiness
commits: b5c28f3..57eb3be (plus the report commit that adds this file)
status: done
e2e-request: required
---
# Report: Member readiness model and `status` (ADR-003, part 1 of 3)

## Summary

- New module `src/clawake/services/readiness.py` holds the shared readiness model for v1
  members. It has five states (`stopped`, `starting`, `ready`, `degraded`, `failed`) with
  snake_case `reasons`.
- It offers `probe_member(instance, probes, *, deadline, unit_result) -> MemberReadiness`
  and `wait_members_ready(instances, timeout, probes) -> list[MemberReadiness]`, so P2 and
  P3 can call them.
- Every host interaction goes through `ReadinessProbes`, an injectable bundle of seams:
  `unit_status`, `run`, `http_get`, `clock` and `sleep`. The unit state comes from the
  existing `SystemdService.status`.
- `HealthSpec.path` now defaults to `/readyz`. A new inventory field
  `readiness: {channels: false, start_window_seconds: 180}` was added.
- `status` (also `status-quadlets`) now does the following:
  - adds `readiness`, `reasons` and `plugins` to every JSON row and keeps all old keys;
  - prints `readiness=<state>`, `reason:` lines, `plugin:` lines and a `WARNING: degraded`
    line;
  - has new options `--wait` and `--timeout <s>`;
  - uses the exit codes from item 6.
- v2 status is unchanged. `--wait` and `--timeout` are rejected for v2. `setup`,
  `restart`, `sync-plugins` and `upgrade` are untouched, except that `upgrade` now waits
  on `/readyz` (item 2).
- Base test count at `0924ac3`: 295 passed, 2 skipped. Now: 359 passed, 2 skipped.

## Changed files

```
 docs/inventory-v2.md              |  41 ++   readiness field, state table, probes
 docs/manual.md                    |  27 ++   new section "Member readiness and `status`"
 src/clawake/cli.py                |  82 +++- status: readiness, --wait/--timeout, exit codes, Architecture-Ref
 src/clawake/config.py             |  11 +-   ReadinessSpec, InstanceSpec.readiness, HealthSpec.path=/readyz
 src/clawake/services/readiness.py | 479 +++  shared readiness model, probes, bounded wait
 tests/test_cli.py                 |  23 +-   inject fake readiness seams into the 2 existing status tests
 tests/test_readiness.py           | 805 +++  state table, probes, secrets, fake-clock wait, CLI
```
The report commit also adds this report and the prompt file, unchanged.

## Checks

| Command | Result |
|---|---|
| `uv sync --locked` | pass |
| `uv run ruff check .` | pass (All checks passed!) |
| `uv run ruff format --check .` | pass (41 files already formatted) |
| `uv run pytest -q` | pass, 359 passed, 2 skipped (base: 295 passed, 2 skipped) |
| `uv lock --check` | pass |

- No `# noqa` or `# type: ignore` was added.
- `except` clauses in the new code are all narrow:
  - `run_command` turns `TimeoutExpired`/`OSError` into an explicit error result;
  - `http_get` turns `HTTPError`/`URLError`/`OSError` into an `HttpOutcome`;
  - `pinned_plugin_version` re-raises as `ValueError`.

  None of them sits on a mutating path.

**Test-first evidence.**
- The first run of `tests/test_readiness.py` failed at collection with
  `ModuleNotFoundError: clawake.services.readiness`.
- After the implementation, I ran mutation checks against scratch copies of `src`
  (`PYTHONPATH=<scratch>/src`). Each rule was broken separately, and each mutation turned
  the suite red:

| Mutation | Failed tests |
|---|---|
| inactive is no longer mapped to stopped | 4 |
| `failed` unit state is no longer mapped | 1 |
| `/readyz` non-200 counted as ready | 12 |
| version check removed | 2 |
| `enabled` check removed | 1 |
| `loaded` check removed | 1 |
| missing-plugin reason dropped | 3 |
| `running:false` check removed | 3 |
| lifecycle check removed | 2 |
| unparseable channels counted as healthy | 2 |
| `--probe` added to the channels argv | 14 |
| no expiry at the deadline | 4 |
| `starting` counted as settled | 5 |
| redaction removed | 1 |
| `channels` default set to `true` | 10 |
| `health.path` default reverted to `/health` | 4 |

## Acceptance criteria

- **All five states and every rule in item 1 have named tests that failed first: met.**
  Evidence: the table below plus the mutation checks above.

  | Rule | Test(s) in `tests/test_readiness.py` |
  |---|---|
  | stopped: unit inactive or dead | `test_stopped_when_unit_inactive`, `test_stopped_when_unit_not_found` |
  | failed: unit failed | `test_failed_when_unit_failed`, `test_failed_when_unit_waits_for_auto_restart`, `test_failed_when_unit_state_unknown` |
  | failed: `--wait` ran out | `test_wait_times_out_to_failed_and_never_exceeds_window`, `test_wait_timeout_override_bounds_the_window`, `test_status_wait_times_out_to_failed_within_override` |
  | starting: `/readyz` 503, refused, timeout, transport error, 204 | `test_starting_when_readyz_not_200[*]` (5 cases), `test_starting_when_unit_is_activating` |
  | degraded: plugin version mismatch, disabled, `status != loaded`, missing, malformed (4 shapes) | `test_degraded_when_plugin_does_not_match_pin[*]` (9 cases), `test_degraded_when_plugins_list_command_fails`, `test_degraded_when_pinned_archive_unreadable` |
  | degraded: channel `running:false`, `lifecycle:"blocked"`, `{"status":"starting"}`, malformed, no accounts | `test_degraded_when_channel_is_not_running[*]` (8 cases), `test_channel_lifecycle_blocked_reports_both_running_and_lifecycle` |
  | ready | `test_ready_without_plugins_or_channels`, `test_ready_when_pinned_plugin_matches`, `test_ready_when_npm_plugin_matches_spec_version`, `test_ready_when_channels_enabled_and_running` |
  | reasons present on every non-ready state | asserted via `_codes(...)` in every test above |

- **`HealthSpec.path` defaults to `/readyz`, the golden test is unchanged and the upgrade
  tests are green: met.**
  - Evidence: `test_health_default_path_is_readyz`.
  - `tests/test_rollout_foundation.py::test_fixture_render_and_runtime_plan_match_golden`
    passes, and no golden file changed (`git diff --stat` touches no fixture).
  - `templates/` contains no `health` reference.
  - `tests/test_upgrade.py` and `tests/test_runtime_upgrade.py` are green.
- **`status` JSON is backward compatible and adds `readiness`, `reasons` and `plugins`:
  met.** Evidence: `test_status_json_keeps_legacy_keys_and_adds_readiness`, which checks the
  exact key set, and `test_status_json_starting_keeps_deprecated_state_healthy`.
- **`--wait` is bounded and exits as specified: met.**
  - Evidence: `test_wait_reaches_ready`, `test_wait_times_out_to_failed_and_never_exceeds_window`
    (7 s per probe against a 30 s window; the clock stays at or below the deadline),
    `test_wait_timeout_override_bounds_the_window`, `test_wait_accepts_degraded_as_settled`,
    `test_wait_keeps_stopped_member_stopped_after_window`.
  - CLI tests: `test_status_wait_reaches_ready_and_exits_zero`,
    `test_status_wait_times_out_to_failed_within_override`,
    `test_status_wait_degraded_exits_zero`, `test_status_exit_codes[*]`,
    `test_status_exit_non_zero_if_any_member_stopped`.
- **Channel checking is off by default and never uses `--probe` (asserted on the fake
  runner's argv): met.** Evidence: `test_channel_checking_is_off_by_default_and_never_probes`,
  `test_channel_probe_never_uses_probe_flag`, `test_status_cli_channel_probe_argv_is_read_only`.
- **No secrets in `reasons`: met.** Evidence: `test_reasons_never_contain_secret_looking_values`
  (fake token in version, status, accountId as a credentialed URL, lifecycle and
  lastError), plus `test_degraded_when_plugins_list_command_fails` (stderr is never echoed).
- **All five checks are green: met** (see Checks).
- **No deployment data in the diff: met.**
  - The tests use only the synthetic names `one`, `two`, `example-*` and `example-cad`,
    fake digests and the sanitized `<tmp>` paths.
  - The docs reuse only the `personal-team`/`pflegebetreuer` example that already exists in
    `docs/manual.md`.

## Behaviour changes for operators

- **`status` / `status-quadlets` (v1):**
  - **Additional reads per member.** One `GET http://127.0.0.1:<published gateway
    port>/readyz` is made when the unit is active. `podman exec` runs only when `/readyz`
    is 200: `plugins list --json` if the member has pinned plugins, and `channels status
    --json` if `readiness.channels: true`.
  - **Text output.** Each member line ends with `readiness=<state>`. Below it follow
    `reason:` lines, `plugin:` lines and, for degraded members, `WARNING: degraded; ...`.
  - **JSON output.** New per-instance keys are `readiness`, `reasons` and `plugins`. The
    `state` key is deprecated but unchanged.
  - **New options.**
    - `--wait` polls every 2 s up to `readiness.start_window_seconds` (default 180).
    - `--timeout <1..3600>` overrides the window. It requires `--wait`, otherwise exit 2.
    - v2 rejects both with exit 2.
  - **Exit codes.**
    - 1 if any member is `stopped` or `failed`. This is as before for rc≠0 units, since
      inactive/failed/not-found units had rc≠0 already.
    - With `--wait`, also 1 if any member is not `ready`/`degraded` at the end.
    - `starting` without `--wait` exits 0, as an active unit did before.
    - `degraded` exits 0.
- **`upgrade`:**
  - Its post-restart health wait now polls `/readyz` instead of `/health`, with the same
    retries (3 × 30 s) and the same per-request timeout.
  - The preview line `health:` now shows `.../readyz`.
  - If a gateway needs longer than about 60 s to become ready after the restart, `upgrade`
    now fails where it passed before. See *Open points*.
- **Inventory:** there is a new optional `readiness` block per instance. Unknown keys are
  rejected (`extra=forbid`).

## Live E2E request

Read-only commands, which are safe to run at any time:
```
uv run clawake status -c /workspaces/clawonshape/deploy/team.json
uv run clawake status -c /workspaces/clawonshape/deploy/team.json --format json
uv run clawake status -c /workspaces/clawonshape/deploy/team.json --wait
uv run clawake status -c /workspaces/clawonshape/deploy/team.json --wait --timeout 30 --format json
```
Expected on the running cluster:
- Both members show `readiness=ready`, or `degraded` with a concrete plugin reason.
- The exit code is 0.
- JSON `plugins[*].installed_version` equals `pinned_version` for every pinned plugin.

Probe argv and requests to expect per member, in this order and only while the previous
step succeeded:
1. `systemctl --user status <member>.service`, which is existing behaviour.
2. `GET http://127.0.0.1:<host_port of gateway_container_port>/readyz`, with no proxy and
   a timeout of `min(health.timeout_seconds, 10)`, default 5 s.
3. `podman exec <container_name> openclaw plugins list --json`, timeout 10 s, only if
   the member has pinned plugins.
4. `podman exec <container_name> openclaw channels status --json`, timeout 10 s, only
   with `readiness.channels: true`. It is never run with `--probe`.

Restart scenario, only if the operator allows a member restart:
```
uv run clawake restart -c /workspaces/clawonshape/deploy/team.json -m <member>            # preview
uv run clawake restart -c /workspaces/clawonshape/deploy/team.json -m <member> --execute
uv run clawake status  -c /workspaces/clawonshape/deploy/team.json -m <member> --wait
```
Expected:
- During the first seconds, a parallel `status` shows `readiness=starting` with reason
  `readyz_unreachable` or `readyz_not_ready`.
- `--wait` ends with `ready` (or `degraded`) and exit 0, in well under 180 s.
- After that, run `--wait --timeout 1` right after a restart. It should end `failed` with
  `start_window_exceeded` and exit 1.

Optional channel check: set `readiness: {channels: true}` on one member in a scratch copy
of the inventory, then run `status --format json` against it. Record the raw shape of
`openclaw channels status --json` in the private evidence; see the assumptions below.

## Open points and risks

- **Assumed JSON fields.** The live shapes are unverified. docs.openclaw.ai documents
  neither JSON schema.
  - **`plugins list --json`:** the architect verified a top-level object with a `plugins`
    array, and `id`/`version`/`enabled`/`status` per entry. The parser requires
    `version: str`, `status: str` and `enabled: bool`, and reports `plugins_unparseable`
    otherwise. `"loaded"` is the only accepted status for enabled pins. For pins with
    `enabled: false` in the inventory, `enabled` and `status` are not checked; the version
    is still compared.
  - **`channels status --json`:** the account container is **assumed**. I accept
    `channelAccounts: {<channel>: [account, ...]}` (my recollection of OpenClaw's gateway
    `channels.status` payload) or `channels: {<channel>: {accounts: [...]}}`. Anything
    else gives `channels_unparseable`, so the member is degraded and never silently
    healthy.
    - Per account, `running` (bool) and/or `lifecycle` (str) are used, and the label uses
      `accountId`.
    - Only `lifecycle == "running"` counts as up. The docs name only `"blocked"`, so other
      live values such as `"connected"` would show as `channel_lifecycle`. Please capture
      a live sample before you enable `channels` on a member.
  - An empty account list gives `channels_none_reported`, so it counts as degraded.
- **Drift between the ADR and this prompt.**
  - ADR-003 item 1 says `degraded` means "not installed at the pinned sha256" and uses
    `channels status --probe`. Per the prompt, the implementation compares **versions**,
    because there is no integrity field, and **never** uses `--probe`.
  - Please update the ADR text, or record that sha verification stays open for P2.
- **Architecture drift.** `docs/architecture.md#ownership-and-isolation` still says
  "Status represents systemd service state". I marked it in `cli.py` with
  `Architecture-Ref`. Proposed replacement text:
  > Status reports one readiness state per v1 member (`stopped`, `starting`, `ready`,
  > `degraded`, `failed`; ADR-003) from systemd state, the gateway's `/readyz`, pinned
  > plugin versions and, opt-in, recorded channel lifecycle. `status --wait` is bounded by
  > the member's start window. Probes are read-only and never use `channels --probe`.
- **`upgrade` is stricter.** `wait_for_health` keeps `health.retries` (3) ×
  `interval_seconds` (30). If `/readyz` needs longer than about 60 s after a restart
  (Discord was seen about 70 s after start), `upgrade --execute` would now fail and stop
  the member. Options are to raise `health.retries` per member, or to have P2 switch
  `upgrade` to `wait_members_ready`. Please check this during the E2E run.
- **Remaining unbounded call.** `systemctl --user status` is called through the existing
  `SystemdService` without a timeout, as before. A hanging systemd call could delay
  `--wait` past its window. The HTTP and `podman exec` probes are clamped to
  `min(10 s, remaining window)`.
- **`--wait` keeps polling stopped or failed members until their window ends.** It does
  not stop early, so systemd auto-restarts can still recover. Stopped members stay
  `stopped` and also get `start_window_exceeded`. P2 may want an early exit for members
  it did not start.
- **`activating (auto-restart)` maps to `failed`.** Under `--wait`, a member that recovers
  through systemd restart is still polled and can become ready.
- **`docs/manual.md` is in German.** The new section is in English, per the agent rules.
  The German sentences in §1 ("`status` zeigt den systemd-Zustand") and §5 are now
  incomplete, and I left them unchanged. Please align them.
- The two existing status tests in `tests/test_cli.py` previously made a real loopback
  GET to `127.0.0.1:18789` once `status` started probing. They now inject fake seams.
  Two expectations were extended: `readiness == "ready"`, and
  `state=failed rc=3 readiness=failed`.

## Out-of-scope findings

- `src/clawake/services/runtime_upgrade.py:80-95`: `wait_for_health` uses
  `urllib.request.urlopen` with environment proxies. With `http_proxy` set and no
  `no_proxy` for 127.0.0.1, the upgrade health check would go through the proxy. The new
  readiness probe uses an opener without a proxy.
- `src/clawake/cli.py:294-305` (`_dashboard_host_url`) duplicates
  `runtime_upgrade._gateway_host_url`.
- `src/clawake/cli.py` `status_quadlets`: the JSON `diagnostics` and text `cause:` lines
  show raw journal lines, which is existing behaviour. They can contain personal content,
  as the manual §1 says, and they are not redacted.

OpenClaw sources checked on 2026-10-19:
- docs.openclaw.ai/cli/gateway/query: `/readyz` vs `/healthz`, confirmed.
- docs.openclaw.ai/cli/channels: `channels status --json` without `--probe` reads recorded
  lifecycle; `running: false` / `lifecycle: "blocked"`; the `{status: "starting",
  startupPhase}` form. Confirmed; the full schema is not documented.
- docs.openclaw.ai/cli/plugins and docs.openclaw.ai/cli/plugins/list: the `--json` schema
  is not documented, so it is unverified there and relies on the architect's live
  observation.

`/workspaces/clawonshape/deploy/team.json` sha256 was `68b9b10f…cff3` both at the start
and at the end. No golden comparison depended on it.
