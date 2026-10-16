# Evidence: rollout-foundation integration, live preview comparison (2026-10-16)

- Issue: edge-robot/clawake#25
- Prompt/report: `docs/prompts/2026-10-16-integrate-rollout-foundation{,.report}.md`
- Reviewer: onshape-architekt
- Base: `demo/gateway-native-read-20261014` @ 4da3957 (operator checkout)
- Candidate: `integrate/rollout-foundation` @ 8bb966e (merge 55ea8df of e505969 into 08f5a11)

## Review re-checks (inside the clawake-dev devcontainer)

| Command | Result |
|---|---|
| `uv sync --locked` | pass |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 39 files already formatted |
| `uv run pytest -q` | 295 passed, 2 skipped |
| `uv lock --check` | pass |

Commits 55ea8df..8bb966e carry the `Refs`, `Prompt`, `Agent` and `Co-Authored-By`
trailers. The merge has parents 08f5a11 and e505969. No deployment values (Discord IDs,
host paths, container or host names) appear in `git diff 08f5a11 HEAD`.

## Live preview comparison (read-only, no `--execute`)

Against the deployed Onshape team inventory (private, kept in edge-robot/clawonshape
`deploy/team.json`; content hash unchanged before and after the run) and the deployed
onshape-mcp service config:

| Command (preview) | Base exit | Candidate exit |
|---|---|---|
| `clawake validate -c <deploy/team.json>` | 0 | 0 |
| `clawake setup -c <deploy/team.json>` | 0 | 0 |
| `clawake sync-plugins -c <deploy/team.json>` | 0 | 0 |
| `clawake onshape-mcp -c <service.json> --action plan` | 0 | 0 |

- `diff -r` of all four outputs between base and candidate: **identical**.
- `setup` preview: `0/2 member(s) changed`. The drift guard
  (`Managed field changed outside Clawake`) did not fire against the live `openclaw.json`.

## Result

Accepted for the operator checkout switch. Remaining live step: switching the operator
checkout to the candidate. That needs the operator pane to release the cluster; after the
switch, rerun the `setup` preview, which must still report 0 changed members.
