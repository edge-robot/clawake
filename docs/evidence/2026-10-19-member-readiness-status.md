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

## Not yet covered

- Restart scenario (`starting` → `ready`, `--timeout 1` → `failed`): needs the operator
  pane to release a member restart.
- Channel checking: the live shape of `openclaw channels status --json` is not recorded
  yet; `readiness.channels` stays off on all members.
