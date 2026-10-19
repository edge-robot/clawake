# Clawake architectural decisions

Clawake's own decision log. Decisions shared by ClawOnshape and onshape-mcp live in
`edge-robot/clawonshape` `docs/ADR.md`; this file covers deployment and lifecycle only.
Normative companions: [architecture](architecture.md), [project overview](project-overview.md),
[inventory v2](inventory-v2.md).

This repository is public. Never record Discord IDs, host paths, host or container names,
or values from a deployed inventory here; refer to private evidence instead.

## ADR-001: Ownership ledger semantics for managed OpenClaw runtime config

Status: Accepted (2026-10-16). Implemented on `integrate/rollout-foundation`
(edge-robot/clawake#25, merge 55ea8df). Documentation in `architecture.md` pending.

### Context

Clawake writes fields into each member's `openclaw.json` (workspace, A2A, subagents,
`mcp.servers.*`, plugin config, tool grants) and records them in a per-member ownership
ledger. Operators and OpenClaw itself may also edit `openclaw.json`. Two development lines
(e505969, 4da3957 / 9b152b6 / 03445a7) introduced overlapping rules that #25 merged.

### Decision

1. **Owned fields only.** Clawake owns exactly the paths and tool names it added and recorded
   in the ledger. Pre-existing operator values, including grant names, are never claimed,
   and are kept when the inventory withdraws them.
2. **Fail-closed drift guard.** If a ledger-owned field in the live file differs from the
   ledger value, `setup` (preview and `--execute`) stops with
   `Managed field changed outside Clawake: <path>`. Clawake never overwrites silently.
3. **Explicit one-time adoption.** A not-yet-owned path is only taken over by a named rule.
   Today there is one: the disabled hosted Onshape connector at `mcp/servers/onshape`.
4. **Idempotence.** An unchanged inventory against an unchanged host is a no-op.
5. **Global grants never widen the host policy.** Explicit global tool grants never create
   a host allowlist and never accept wildcards or groups.

### Consequences

- Any OpenClaw command that rewrites an owned field (e.g. `mcp.servers.*`) blocks the next
  `setup` until the operator resolves it. Which OpenClaw commands do that is
  **unverified** per release (docs.openclaw.ai/cli/mcp documents `mcp set/unset` only).
  Record verified commands per OpenClaw version before #24 relies on unattended runs.

## ADR-002: Two MCP server models with a hard conflict rule

Status: Accepted as interim (2026-10-16). Unification is an open decision.

### Context

The live Onshape team uses the instance flags `onshape_mcp` / `onshape_mcp_enabled`,
which render the local, plain-HTTP onshape-mcp server on the private Podman network with
an external token owned by ClawOnshape. e505969 added a generic `openclaw.mcp_servers`
model (`McpServerSpec`), which only accepts HTTPS with `auth: oauth`.

### Why there are two models

They answer different questions about **who holds the credential**:

| | `onshape_mcp` flags (68679f3, 4da3957) | `openclaw.mcp_servers` (e505969) |
|---|---|---|
| Server | the local onshape-mcp container on the private Podman network | any remote MCP server on the internet |
| Transport | plain HTTP to a private network alias | HTTPS only, TLS verified |
| Credential | external token file published by **ClawOnshape** (shared ADR: ClawOnshape owns Onshape OAuth); OpenClaw runs no OAuth flow | OAuth login and token cache held by **OpenClaw's native MCP client** in the member |
| Extras | tool filter for the Onshape tools, adoption of the disabled hosted Onshape connector | strict URL, scope and tool-name validation; disabled by default |
| Used by the deployed team | yes | no |

The generic model cannot express the Onshape setup today: it rejects plain HTTP and
requires OAuth. The flags cannot express a third-party OAuth server. Both lines were
developed in parallel and were only merged in #25.

### Decision

- Both models stay. The flags are a preset for the local Onshape server.
- Configuring the same server name through both is a validation error; planning also
  refuses it. Neither model overrides the other.
- Per the shared ADR (`edge-robot/clawonshape` `docs/ADR.md`, ADR-review-2026-10-15), the
  OpenClaw native MCP client is the transport, Clawake provisions its configuration, and
  Clawake never handles OAuth tokens.

### Open decision

Whether to express the flags through `mcp_servers`. This needs `auth: external|none` and
`http` restricted to private network aliases, plus an explicit `adopt:` rule replacing
the hard-coded adoption in ADR-001 (3).

## ADR-003: Member readiness model and one-step rollout

Status: Accepted (2026-10-16, user decision). Tracks edge-robot/clawake#24, #23 and #5
(comment C1). Decided: default readiness path `/readyz`; channel probing is opt-in per
member. Implementation order: P1 #23 readiness and `status`, P2 #5 targeted restarts and
`sync-plugins`, P3 #24 `rollout`.

### Context

On 2026-10-16 a single-member plugin rollout needed three `--execute` runs plus manual
probes (private evidence: `edge-robot/clawonshape`
`docs/evidence/2026-10-16-plugin-command-spike.md`, C1):

- `setup --execute` restarts **every** selected member. That was the deliberate fix for
  #5 problem 1 (unchanged units never started), and it interrupts unchanged members.
- `status` maps a running unit to `healthy` (`cli._status_state`), while the gateway is
  still starting.
- `sync-plugins --execute` does not wait for the gateway. It fails with `ECONNREFUSED`,
  continues with the next member, and reinstalls identical packages.

Readiness signals, verified on 2026-10-16 against OpenClaw 2026.9.9 with read-only probes:

- `GET /readyz` on the gateway port returns `200 {"ready":true}`.
  docs.openclaw.ai/cli/gateway/query: `/readyz` "stays red while startup plugin sidecars,
  channels, or configured hooks are still settling".
- `/healthz` is liveness only. Clawake's current `HealthSpec.path` default `/health` also
  answers 200 and is therefore not a readiness signal.
- Per the docs, `openclaw gateway status` "does not certify plugin or channel readiness".
  Its exit code 0 means "diagnostics completed".

### Decision

1. **One readiness model for every member**, shared by `status`, `setup`, `restart`,
   `sync-plugins`, `upgrade` and `rollout`:

   | State | Meaning |
   |---|---|
   | `stopped` | unit inactive |
   | `starting` | unit active, `/readyz` not 200 yet, or not reachable, within the start window |
   | `ready` | `/readyz` 200 |
   | `degraded` | `/readyz` 200, but a pinned plugin is missing, disabled, not `loaded`, or its installed `version` differs from the version inside the pinned archive; or (opt-in per member) `openclaw channels status --json` (never `--probe`) reports a configured channel account not running; or any of these checks cannot be parsed |
   | `failed` | unit failed, or still not ready after the bounded wait |

   The probe reads only status fields. It never reads container env, logs content or
   tokens.
2. **Readiness path.** Change the default `HealthSpec.path` to `/readyz`. `/healthz` stays
   available for liveness-only use.
3. **Restart only what changed.**
   - `setup --execute` restarts members whose rendered artifacts changed.
   - Unchanged but inactive members are **started**, never restarted, which keeps the #5
     problem 1 fix.
   - Unchanged active members are left alone.
4. **`sync-plugins` waits, skips and stops.**
   - Wait for `ready` (bounded) before installing.
   - Skip install and restart when the installed sha256 equals the pin.
   - Stop at the first failing member, exit non-zero, and print a per-member summary.
5. **`clawake rollout`** composes the above, preview first:
   - validate;
   - one combined preview (artifact diffs, plugin pins old→new, members to start or
     restart, grants);
   - with `--execute`: backup → apply → restart changed → wait ready → sync plugins →
     verify sha → wait ready → summary;
   - stop on the first failure and print the backup path.
6. **Bounded waits.** Timeouts come from the inventory with safe defaults, e.g. a
   180 s start window, chosen because the Discord provider was observed connecting about
   70 s after container start. There are no unbounded retries.

### Open questions

- Is `openclaw channels status --probe` cheap and side-effect free enough for every
  `status` call, or only for `rollout`? Unverified.
- Installed-package integrity: `openclaw plugins list --json` (2026.9.9, verified live
  2026-10-19) reports `id`, `version`, `enabled`, `status` but no sha256. P1 therefore
  compares versions. Whether `sync-plugins` should record the installed archive sha256
  itself is decided in P2.
- Live JSON shape of `openclaw channels status --json` is unverified; channel checking
  stays off by default until a live sample is recorded.
- Where do runtime pins (plugin sha, candidate IDs) live? This is #20 (M11). `rollout`
  must not need hand edits of the versioned inventory.
- Backup scope for `rollout`: inventory plus per-member managed config only, or also
  workspaces (reuse `services/backup.py`)?

## ADR-004: Runtime pins live in a private overlay referenced by the inventory

Status: Accepted (2026-10-19, user decision: option A, image digests included).
Tracks edge-robot/clawake#20 (M11) and is a prerequisite for #24 (`clawake rollout`).

### Context

The versioned inventory of the deployed Onshape team is changed on every rollout:

- the plugin `artifact_path` and `sha256` per member are edited by hand;
- `upgrade` / rollback rewrite the image `tag`, `digest`, `known_good_tag` and
  `known_good_digest` (`services/upgrade.py` `apply_upgrade`, `rollback_to_known_good`);
- ClawOnshape's provisioning script writes the Candidate workspace ID. That part leaves
  with edge-robot/clawonshape#11 P4 and is not Clawake's concern.

As a result, the file is permanently dirty in a working tree that other actors share,
and `rollout` would need a hand edit of a versioned file.

Options considered:

- **(A)** a private pin overlay referenced by the inventory;
- **(B)** `${VAR}` expansion from env files;
- **(C)** `rollout` rewriting the inventory.

### Decision

1. **Reference from the inventory.** A v1 inventory may declare
   `runtime_pins: <path>`, either relative to the inventory file or expanded like other
   paths. The file is Clawake-owned private state: not versioned, mode 0600.
2. **Allowlisted fields only.** The overlay may set, per declared member:
   - `image.tag`, `image.digest`, `image.known_good_tag`, `image.known_good_digest`;
   - for declared archive plugins, `plugins.<id>.artifact_path` and
     `plugins.<id>.sha256`.

   Anything else is rejected, including unknown members, undeclared plugin ids, npm
   plugins and other keys.
3. **Merge before validation, conflicts fail closed.** The overlay is applied to the raw
   inventory before model validation.
   - If both files set a field to **different** values, loading fails.
   - Equal values are allowed, so the migration can proceed step by step.
4. **Writers.** With `runtime_pins` declared:
   - `upgrade` / rollback write image fields to the overlay instead of the inventory;
   - `rollout` (P3b) writes plugin pins only after a verified rollout.

   Writes are atomic (temporary file + rename, mode 0600) and keep timestamped backups of
   the previous overlay, which are the rollback target. Without `runtime_pins`,
   behaviour is unchanged.
5. **Migration.**
   - A preview-first command extracts the current pins from an inventory into a new
     overlay.
   - The operator then removes the pinned fields from the versioned inventory and adds
     `runtime_pins`.
   - For the deployed team, the operator session does this, because it owns
     `clawonshape/deploy/team.json`.
6. **v2 inventories are unchanged.** v2 rejects `runtime_pins` until v2 rollout exists.

### Consequences

- The versioned inventory stays reviewable and clean. The effective deployment is
  inventory plus overlay, and `clawake pins show` must make the source of each value
  visible.
- The overlay is a single point of truth for "what is deployed". Its backups replace hand
  backups of `team.json`.
- The Candidate ID remains in the inventory until clawonshape#11 P4 removes it there.

## ADR-005: Discord channel provisioning from the inventory and private site values

Status: Accepted (2026-10-19, user decision). Tracks edge-robot/clawake#26; folds in #21 (M22).
Prerequisite for removing the `openclaw config patch` workaround from the ClawOnshape
onboarding runbook.

### Context

- `channels.discord` of the deployed members exists only in their private runtime
  `openclaw.json`. It was written by hand, is never versioned, and is lost on a fresh
  machine. Clawake owns only `channels/a2a/*` (ADR-001 ledger).
- Live key paths (read 2026-10-19, values not read), Lead and Engineer alike:
  `enabled`, `groupPolicy`, `guilds.<id>.channels.<id>.enabled`, `token`. The `token` is a
  plaintext **string** on both members, including the member where Discord is disabled.
  The file is rewritten by Clawake and OpenClaw and is part of the member backups.
- No `users`/`roles` allowlist and no explicit `dmPolicy`/`allowFrom` (#21). The proof-based
  mutation release (edge-robot/clawonshape#11) relies on the channel's sender allowlist.
- The IDs involved (guild, channel, user, role, application) are not secrets, but they are
  private. They must not enter this public repository or a versioned inventory.
- OpenClaw facts (docs.openclaw.ai, fetched 2026-10-19; docs only, not live-verified):
  - `/channels/discord/setup`: `channels.discord.token` accepts a SecretRef
    `{source: "env", provider: "default", id: "<VAR>"}`; config values win over the
    `DISCORD_BOT_TOKEN` env fallback; `applicationId` skips the startup lookup;
    `accounts.<id>` exists for several bots.
  - `/channels/discord/access-control`: `dmPolicy` is `pairing` (default), `allowlist`,
    `open` or `disabled`; `allowFrom` is the DM allowlist. `groupPolicy` is `open`,
    `allowlist` or `disabled`. Guilds are keyed by ID; per guild and per channel:
    `requireMention`, `users` (IDs recommended; names need `dangerouslyAllowNameMatching`),
    `roles` (IDs only). A guild with a `channels` map denies unlisted channels.
  - Config rewrites by OpenClaw: `doctor --fix` migrates the legacy keys `dm.policy`,
    `dm.allowFrom` and per-channel `allow`. Pairing approvals are stored in the state
    database (`channel_pairing_allow_entries`), not in `openclaw.json`. Explicit
    `openclaw config patch` and agent-driven config edits write the file.

Options considered for the private values:

- **(A)** a second private file, `site_values`, written only by the operator;
- **(B)** a section in the runtime pin overlay (ADR-004);
- **(C)** host environment variables through the existing `${VAR}` expansion.

(B) mixes two writers and lifecycles: Clawake rewrites pins on every rollout and keeps 20
backups; site values change once per site. `pins show` and pin rollback would carry site
data. (C) is not reviewable, not validated and differs per shell.

### Decision

1. **Site values file (option A).** A v1 inventory may declare `site_values: <path>`
   (resolved like `runtime_pins`). Format:
   ```json
   {"version": 1, "cluster": "<cluster.name>", "values": {"<name>": "<string>" | ["<string>", ...]}}
   ```
   - Names match `^[a-z0-9][a-z0-9_.-]*$`. Unknown top-level keys and a `cluster`
     mismatch are rejected.
   - The operator writes the file; **Clawake never writes it.** Loading refuses a group- or
     world-writable file and warns if it is group- or world-readable (ADR-004 rules).
   - It never holds secrets. Tokens stay in member env files.
   - v2 rejects `site_values` until v2 rollout exists.
2. **References.** Any inventory string may be exactly `${site:<name>}`. The reference is
   replaced by the site value (a string, or a list of strings where the field is a list)
   after `${VAR}` expansion and before the runtime pin overlay and model validation.
   - A partial reference (`"x-${site:a}"`) is an error, so IDs are never concatenated.
   - An unknown name fails loading; an unused value is a warning in `validate`.
   - Substitution is generic: it also applies to plugin `config`, so the approver set can
     come from the same value as the channel allowlist (#21). Whether ClawOnshape consumes
     it there is ClawOnshape's decision (see *Open decisions*).
3. **Discord model, per member.** Optional `openclaw.discord` block:
   ```yaml
   discord:
     enabled: true
     token_env: DISCORD_BOT_TOKEN        # name only, never a value
     application_id: ${site:discord.application_id}   # optional
     dm_policy: allowlist                # disabled (default) | allowlist | pairing; no "open"
     allow_from: ${site:discord.dm_allow}            # required iff dm_policy=allowlist, else forbidden
     group_policy: allowlist             # allowlist | disabled; no "open"
     guilds:
       - id: ${site:discord.guild}
         require_mention: true
         users: []                       # guild defaults, optional
         roles: []
         channels:
           - id: ${site:discord.lead_channel}
             require_mention: true
             users: ${site:discord.approvers}
   ```
   - Guilds and channels are lists with an `id` field, so IDs are never mapping keys in a
     versioned file. Duplicate IDs are rejected.
   - After substitution every `id`, `users`, `roles` and `allow_from` entry must be a
     numeric Discord snowflake. Names, tags, `*` and `dangerouslyAllowNameMatching` are
     not offered.
   - With `group_policy: allowlist`, every enabled channel needs a non-empty `users` or
     `roles` list, its own or inherited from the guild (#21: no channel open to the whole
     guild).
   - `enabled: false` renders only `{"enabled": false}`; all other keys are rejected.
   - Default account only. `accounts.<id>` is out of scope.
4. **Rendering and ownership.** Clawake renders the whole `channels.discord` object as
   **one** ledger-owned field `channels/discord` (ADR-001):
   - `token` is always `{source: "env", provider: "default", id: <token_env>}`;
   - keys are rendered in OpenClaw's current names only, so `doctor --fix` has nothing to
     migrate;
   - any change inside the object made outside Clawake (config patch, agent edit) trips the
     drift guard. Pairing approvals do not, because they live in the state database.
   - `setup` checks that `token_env` is declared as a key in the member's env file. It
     parses key names only and never reads, prints or stores the value.
   - A member without a `discord` block: Clawake leaves `channels.discord` untouched and
     unowned (unchanged behaviour).
5. **Named one-time adoption `discord-channel`.** An existing, not-yet-owned
   `channels.discord` is taken over only when `setup` is given `--adopt discord-channel`.
   - **Precondition, fail closed:** the live `token` must not be a string. If it is,
     `setup` stops with an instruction for the operator: move the token into the member
     env file under `token_env` and replace the field with the SecretRef. The message
     never contains the value. Clawake never reads, moves or deletes a token value.
   - The preview lists the key paths that are added, removed and changed. IDs are
     redacted as `<id>`; no values are printed.
   - `--execute` takes the member backup first, then replaces the object and records
     ownership.
   - Without the flag, an unowned existing object is a preview error naming the rule.
6. **Acceptance.** After rendering, the P1 opt-in probe `readiness.channels` must report
   the Discord account `ready` for every member with `enabled: true`.

### Consequences

- A fresh machine needs the inventory, the site values file and the member env files.
  The onboarding runbook loses its `openclaw config patch` step.
- Operators edit Discord access in the site values file and run `setup`. A change made
  through the OpenClaw UI or an agent blocks the next `setup` until it is moved into the
  site values file or reverted, as intended by ADR-001.
- Public evidence and issues refer to site value **names**, never to values.
- The disabled member stops carrying a plaintext token copy once adopted.

### Open decisions

- ~~**ClawOnshape bindings**~~ — **decided 2026-10-19** in edge-robot/clawonshape
  `docs/ADR.md` ADR-2026-10-19 (option A): plugin bindings (`requesterSenderId`,
  `discordChannelId`, approver set) use `${site:…}` references in plugin `config`. Shared
  site value names: `discord.guild`, `discord.lead_channel`, `discord.requester`,
  `discord.approvers` (list), `discord.dm_allow` (list), `discord.application_id`;
  `onshape.document`, `onshape.workspace`, `onshape.element` are ClawOnshape-only.
  Clawake only provides the substitution.
- **OpenClaw config rewrites** are documented, not live-verified. Before #24 relies on
  unattended `rollout`, verify on the deployed OpenClaw version that a pairing approval
  and a gateway restart leave `channels.discord` byte-identical.

### Amendment 2026-10-19 (user decision): OpenClaw schema alignment and `configWrites`

Facts from the live OpenClaw config JSON schema (`openclaw config schema`, deployed
OpenClaw 2026.9.9, read by the architect 2026-10-19; schema only, no config values):

- The schema is strict: `additionalProperties: false` on `channels.discord`, on
  `guilds.<id>` and on `guilds.<id>.channels.<id>`. A key OpenClaw does not know makes
  the whole config invalid. Every rendered key must therefore exist in the schema.
- Confirmed keys: top-level `applicationId`; per-guild and per-channel `requireMention`,
  `users`, `roles`; per-channel `enabled`.
- The env SecretRef `id` must match `^[A-Z][A-Z0-9_]{0,127}$`.
- `channels.discord.configWrites` exists. Per docs.openclaw.ai `/channels/discord/events`
  ("Config writes"), channel-initiated config writes (`/config set`, `/config unset`)
  are **enabled by default**.

Decisions:

7. **`token_env`** must match the SecretRef id pattern `^[A-Z][A-Z0-9_]{0,127}$`.
8. **`configWrites: false`** is always rendered for an enabled member. It is not
   configurable in the inventory. Config changes go through the inventory and `setup`,
   never through chat. This also keeps the ledger-owned `channels/discord` free of
   channel-initiated drift.
9. References inside YAML flow lists must be quoted (`["${site:x}"]`); unquoted
   `[${site:x}]` is a YAML parse error.
