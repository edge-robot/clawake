# Evidence: runtime pin overlay on a private copy of the deployed inventory (2026-10-19)

- Issue: edge-robot/clawake#20; ADR: `docs/ADR.md` ADR-004
- Prompt/report: `docs/prompts/2026-10-19-runtime-pins{,.report}.md`
- Candidate: `feat/20-runtime-pins` @ 6f5ca3e; reviewer: onshape-architekt

## Review re-checks (clawake-dev devcontainer)

ruff check / format --check green, `uv lock --check` green, pytest 445 passed, 2 skipped.
Commits d5bc09a..6f5ca3e carry the required trailers; no deployment values in the diff.

## E2E (private scratch copy, mode 0700; deleted afterwards; live cluster read-only)

| Step | Result |
|---|---|
| 1 Baseline on the copy | `validate` ok; `setup` preview `0/2 member(s) changed`, both `none`; `sync-plugins` actions recorded; `pins show`: `runtime pins: none declared`, all `[inventory]` |
| 2 `pins init` preview | 12 removal entries (4 image + 2 plugin fields per member), `runtime_pins` line shown, DRY RUN |
| 3 `pins init --execute` | overlay written with mode 600; inventory copy byte-identical (sha256) |
| 4a `runtime_pins` added, fields still present | `validate` ok, `runtime pins: … (2 members, 12 fields)`; 12 fields `[overlay, also in inventory]` |
| 4b 12 fields removed from the copy | all 12 fields `[overlay]` |
| 5 Equivalence | `validate` ok; `setup` preview and `sync-plugins` actions **identical** to step 1 |
| 6 Negatives | a differing digest in the inventory → `runtime pins conflict`; overlay `chmod 664` → refused (`group- or world-writable`) |

The original deployed inventory and the live cluster were not modified.

## Follow-up

- The out-of-scope finding (legacy `upgrade --execute` rewrites a JSON inventory as YAML
  flow style) is tracked separately. Migrating the deployed inventory to `runtime_pins`
  avoids that code path.
