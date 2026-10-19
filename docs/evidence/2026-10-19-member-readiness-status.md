# Evidence: member readiness `status` against the live cluster (2026-10-19)

- Issue: edge-robot/clawake#23; ADR: `docs/ADR.md` ADR-003
- Prompt/report: `docs/prompts/2026-10-19-member-readiness{,.report}.md`
- Candidate: `feat/23-member-readiness` @ fbf9d72; reviewer: onshape-architekt

## Review re-checks (clawake-dev devcontainer)

ruff check / format --check green, `uv lock --check` green, pytest 359 passed, 2 skipped.
Commits b5c28f3..fbf9d72 carry the required trailers; no deployment values in the diff.

## Live, read-only

`clawake status -c <deployed inventory> --wait --timeout 30 --format json` against the
deployed two-member Onshape team (OpenClaw 2026.9.9), no restart:

| Member role | readiness | legacy `state` | reasons | pinned plugin version | installed version | plugin status |
|---|---|---|---|---|---|---|
| product_owner | ready | healthy | none | 0.2.10 | 0.2.10 | loaded, enabled |
| developer | ready | healthy | none | 0.2.10 | 0.2.10 | loaded, enabled |

Exit code 0.

## Live restart scenario (operator-approved, developer member)

Restart via `clawake restart -m <developer member>` (preview first, then `--execute`); the
following ran immediately after, from `feat/23-member-readiness`:

| Step | Command | readiness | reasons | exit |
|---|---|---|---|---|
| 1 | `status --format json` right after restart | starting | `readyz_unreachable` | 0 |
| 2 | `status --wait --timeout 1` | failed | `readyz_unreachable`, `start_window_exceeded` | 1 |
| 3 | `status --wait` (default 180 s window) | ready | none | 0 |

Unit active → gateway `[gateway] ready` (journal): **46 s**. The pre-P1 `status` would
have reported `healthy` during those 46 s (finding C1 in #5).

Consequence for P2: `upgrade` now waits on `/readyz` with 3 × 30 s retries, about 60 s;
46 s leaves only about 14 s of margin. P2 moves `upgrade` to `wait_members_ready` with the
start window.

## Channel status shape (read-only sample, 2026-10-19, robotics lead member)

`openclaw channels status --json` has a top-level `channelAccounts: {<channel>: [account]}`
(channels `discord`, `a2a`). Per account it has `lifecycle`, `running`, `connected`,
`configured`, `enabled`, `restartPending`, `reconnectAttempts`, `lastError` and other
fields. On a healthy member the value is **`lifecycle: "ready"`**, `running: true`,
`connected: true`. P1 accepts only `lifecycle == "running"`, so enabling
`readiness.channels` would wrongly report `degraded`. This is fixed in P2. Values such as
IDs were not recorded.

## Not yet covered

- `readiness.channels` stays off on all deployed members until the P2 fix is live.
