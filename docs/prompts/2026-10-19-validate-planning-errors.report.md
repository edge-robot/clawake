---
prompt: docs/prompts/2026-10-19-validate-planning-errors.md
branch: feat/26-discord-channel
commits: e9b9fe0..5c4511d
status: done
e2e-request: recommended
---
# Report: Fix #32: `validate` must not crash on runtime planning refusals

## Summary

`clawake validate` called `plan_deployment` without error handling, so every runtime
planning refusal ended in an uncaught `ValueError` (traceback). This fix changes that:

- `validate` computes `pending_adoptions` for each selected v1 member and plans with
  `adopt=` the union of the pending names. `validate` never writes, so this is safe. The
  adoption preconditions still run.
- For each member with a pending adoption, it prints one notice line and exits 0.
- Every other planning `ValueError` (drift guard, unmanaged-field refusal, adoption
  precondition such as a plaintext string token) prints `Error: <message>` and exits 1.
  No traceback is printed.
- The setup error pattern now lives in one helper, `_plan_or_exit`, which both `setup`
  and `validate` use. There is no copy. Setup behaviour is unchanged: it passes the
  requested adoptions, and pending adoptions are computed only when some were requested,
  as before.

The base was verified before starting: branch `feat/26-discord-channel`, HEAD `3ec4c60`,
and the prompt file was the only untracked file. The base test count was
`590 passed, 2 skipped`.

The report commit follows `5c4511d`.

## Changed files

```
 docs/manual.md         |   8 +++     validate behaviour in the adoption walkthrough (step 4)
 src/clawake/cli.py     |  48 ++++---  _plan_or_exit helper; setup uses it; validate plans with
                                      pending adoptions, prints notices and clean errors
 tests/test_validate.py | 140 ++++++   new tests for #32 (synthetic data)
```

## Checks

| Command | Result |
|---|---|
| `uv sync --locked` | pass (`Checked 21 packages`) |
| `uv run ruff check .` | pass (`All checks passed!`) |
| `uv run ruff format --check .` | pass (`52 files already formatted`) |
| `uv run pytest -q` | pass, `595 passed, 2 skipped` (base: 590 passed, 2 skipped) |
| `uv lock --check` | pass (`Resolved 29 packages`) |

No new `# noqa` or `# type: ignore`.

### Tests, before (base `3ec4c60` code) → after

| Test (`tests/test_validate.py`) | before | after |
|---|---|---|
| `test_validate_pending_adoption_is_a_notice` | FAIL (uncaught `ValueError: Refusing to overwrite unmanaged OpenClaw field…`) | pass |
| `test_validate_pending_adoption_with_string_token_is_an_error` | FAIL (uncaught `ValueError`) | pass |
| `test_validate_drift_after_adoption_is_an_error` | FAIL (uncaught `ValueError: Managed field changed outside Clawake…`) | pass |
| `test_validate_without_discord_output_is_unchanged` | pass (expected bytes captured from base) | pass |
| `test_validate_writes_nothing` | FAIL (uncaught `ValueError`) | pass |

### Mutation checks

Both mutations were run in scratch copies of `src/` via `PYTHONPATH`. The checkout was
not touched.

| Mutation | Result |
|---|---|
| Remove the `except ValueError` handling in `_plan_or_exit` | `test_validate_drift_after_adoption_is_an_error` and `…string_token_is_an_error` FAIL (2 failed, 3 passed) |
| Plan without `adopt=` in validate (`adopt = ()`) | `test_validate_pending_adoption_is_a_notice`, `…string_token_is_an_error` and `test_validate_writes_nothing` FAIL (3 failed, 2 passed) |

## Acceptance criteria

- **The new tests failed first, and the mutation checks catch both changes: met.** See
  the tables above.
- **All mandatory checks are green: met.** See the checks table.
- **Constraint 1: notice per pending member, plan with the union, preconditions still
  run: met.** Evidence: `…is_a_notice` and `…string_token_is_an_error`. The canary token
  is absent from the output (`_assert_no_private_values`).
- **Constraint 2: other refusals give `Error: <message>` and exit 1, with no traceback:
  met.** Evidence: `…drift_after_adoption_is_an_error`. `_assert_clean` checks that the
  exception is a `SystemExit` and that `Traceback` is absent.
- **Constraint 3: success output unchanged apart from the notices, no IDs or values:
  met.** Evidence: `…without_discord_output_is_unchanged` compares bytes with the base
  output, with and without site values. The notice test asserts the exact line list.
- **Constraint 4: scope: met.** `setup` semantics are unchanged, and the existing
  `test_discord_channel.py` setup and rollout tests pass. `rollout`, `status`, `pins` and
  the planning code were not touched. There are no new dependencies.

## Behaviour changes for operators

`validate` (v1 inventories) changes as follows:

- **Pending adoption:** exit 0. After the usual summary lines, it prints one stdout line
  per member and adoption.
- **Planning refusal:** exit 1 with `Error: <message>` on stderr instead of a traceback.

The exact outputs below use synthetic data, with `<tmp>` standing for the test
directory.

Pending adoption (exit 0):
```
Valid inventory: example-cad; 2 member(s), 10 artifact change(s).
site values: <tmp>/pending/private/site.json (5 values, 6 references)
pending adoption: lead discord-channel (review with: clawake setup -c <tmp>/pending/inventory.yml --adopt discord-channel)
```

Pending adoption with a plaintext string token (exit 1):
```
Error: Cannot adopt channels/discord of member lead: the live token in <tmp>/string/lead/state/openclaw.json is a plaintext string. Move it into the member env file as DISCORD_BOT_TOKEN, replace channels.discord.token with {"source": "env", "provider": "default", "id": "DISCORD_BOT_TOKEN"}, then re-run setup --adopt discord-channel. Clawake never reads, moves or deletes token values.
```

Drift guard after adoption (exit 1):
```
Error: Managed field changed outside Clawake: channels/discord
```

The `<inventory>` in the notice is the `-c` value exactly as the operator passed it.
Inventories without a pending adoption or refusal produce byte-identical output to
before.

## Live E2E request

Recommended, not required: `validate` is read-only, and setup behaviour is unchanged.
At the next rollout, run against the deployed team:

1. `clawake validate -c deploy/team.yml`
   - Before the adoption, expect exit 0 and one `pending adoption: <member>
     discord-channel (review with: …)` line per hand-written member.
   - If a live token is still a string, expect `Error: Cannot adopt …` with exit 1.
2. After `setup … --adopt discord-channel --execute`, the same command prints no notice.

## Open points and risks

- An `OSError` while reading a live `openclaw.json` (for example, permission denied) is
  still not converted to a clean error. That is the same as in `setup` before and after
  this change. Only `ValueError` was in scope.
- `validate -m <member>` only checks the selected members for pending adoptions, which
  matches the planning scope.

## Out-of-scope findings

None.
