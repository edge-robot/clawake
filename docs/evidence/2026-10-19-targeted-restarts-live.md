# Evidence: targeted restarts and readiness-gated plugin sync, live (2026-10-19)

- Issue: edge-robot/clawake#5 (C1); ADR: `docs/ADR.md` ADR-003
- Prompt/report: `docs/prompts/2026-10-19-targeted-restarts{,.report}.md`
- Candidate: `feat/5-targeted-restarts` @ 59410dc; reviewer: onshape-architekt
- Target: deployed two-member Onshape team (OpenClaw 2026.9.9), private inventory in
  edge-robot/clawonshape, content hash unchanged during the run. Operator consent given
  for `--execute` (part A, no inventory change).

## Review re-checks (clawake-dev devcontainer)

ruff check / format --check green, `uv lock --check` green, pytest 392 passed, 2 skipped.
Commits 33b9475..59410dc carry the required trailers; no deployment values in the diff.

## Previews (read-only)

- `setup`: `0/2 member(s) changed`; both members `none (unchanged, unit_active)`.
- `sync-plugins`: both members `readiness=ready`, plugin 0.2.10, `action: install
  (ledger_missing)`. This is the expected first-run migration.

## Part A, `--execute`

| Step | Command | Result |
|---|---|---|
| 1 | `setup --execute` (whole team) | only `systemctl --user daemon-reload`; summary `none ok` for both; exit 0; both units' `ActiveEnterTimestamp` unchanged, so **no restart** (C1 fixed) |
| 2 | `sync-plugins -m <developer member> --execute` | readiness gate `ready` → `plugins install` → ledger → restart of that member only → ready after **60 s** → summary `install 0.2.10 ready 60`; exit 0; the other member's unit not restarted |
| 3 | same command again | `action: skip`, `Members to restart: none`, summary `skip`; exit 0 in 6 s; no restart (timestamps unchanged) |
| 4 | `status --format json` | both members `ready`, plugin 0.2.10 `loaded` |

The 60 s start time (46 s in the morning) equals the former `upgrade` health budget
(3 × 30 s with two sleeps, about 60 s). This confirms the move to the 180 s start window.

## Not covered (part B)

A real pin change (new archive) for a single member: to be run by the operator session
with its next plugin rollout, following the command sequence in the P2 report.
