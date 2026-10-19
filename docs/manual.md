# Clawake-Betriebshandbuch

Stand: 8. September 2026. Dieses Handbuch beschreibt die vorhandene CLI und beginnt
mit dem realen Fall „WhatsApp für den Pflegebetreuer“. Die gewünschte Weiterentwicklung
steht in der [User Journey](user_journey.md); Sicherheitsannahmen und Alternativen
in der [Sicherheitsbewertung](security.md).

## 1. Zuständigkeiten und Vorbereitung

Clawake verwaltet Images, Quadlets und den Lebenszyklus eines Mitglieds. OpenClaw
verwaltet Modelle, Plugins, Kanäle und Gespräche. Auf dem Host läuft Clawake; Befehle
zur OpenClaw-Konfiguration laufen im betreffenden Container. Ein OpenClaw-Mitglied
wird mit seinem Inventarnamen ausgewählt, hier `pflegebetreuer`.

Alle folgenden Clawake-Beispiele werden im Repository-Verzeichnis ausgeführt:

```bash
make install-dev
export CLAWAKE_PROJECT_ROOT="$PWD"
uv run clawake validate -c personal-team/team.yml -m pflegebetreuer
uv run clawake status -c personal-team/team.yml -m pflegebetreuer
uv run clawake logs -c personal-team/team.yml -m pflegebetreuer -n 50
```

`validate` prüft Schema, Rendering und Unterschiede zu installierten Dateien. Es
prüft weder Plugin-Kompatibilität noch die Funktionsfähigkeit von Podman. `status`
zeigt den systemd-Zustand; ein aktiver Dienst beweist keine funktionierende
WhatsApp-Verbindung. Diagnoseausgaben können persönliche Inhalte enthalten.

## 2. Ein Mitglied bereitstellen und initialisieren

```bash
uv run clawake setup -c personal-team/team.yml -m pflegebetreuer
uv run clawake setup -c personal-team/team.yml -m pflegebetreuer --execute
uv run clawake onboard -c personal-team/team.yml -m pflegebetreuer
uv run clawake onboard -c personal-team/team.yml -m pflegebetreuer --execute
uv run clawake dashboard -c personal-team/team.yml -m pflegebetreuer
```

Vor `setup --execute` müssen die im Inventar referenzierten Teamdefinitionen und
Environment-Dateien vorbereitet sein. Setup erzeugt Laufzeitverzeichnisse und
verwaltete OpenClaw-Einstellungen und installiert Quadlets. Neu gestartet werden nur
geänderte Mitglieder; unveränderte inaktive Mitglieder werden gestartet, unveränderte
aktive bleiben unberührt (see "Targeted `setup` and readiness-gated `sync-plugins`"). Onboarding öffnet den interaktiven
OpenClaw-Assistenten im Container. Dort werden insbesondere Modellzugang und
Laufzeiteinstellungen eingerichtet. Rolleninformationen stammen aus
`/team-definition`; eine Rollenbeschreibung ersetzt keine technische Berechtigung.

Beim ersten ausgeführten Setup nach der Umbenennung stoppt und deaktiviert Clawake
`fokus-partner.service` und entfernt dessen alte Quadlet-Definitionen. Das neue Quadlet
deklariert zusätzlich einen systemd-Konflikt mit diesem Altdienst und stoppt vor jedem
Start einen eventuell separat laufenden Container `fokus-partner`. Persistente
Workspace-Daten und historische Backups werden dabei nicht gelöscht. Damit können Alt-
und Neuinstanz die Ports nicht parallel belegen.

## 3. Fall: WhatsApp-Plugin wird wegen der Plugin-API abgelehnt

Gemeldeter Fehler:

```text
Plugin "@openclaw/whatsapp" requires plugin API >=2026.9.2,
but this OpenClaw runtime exposes 2026.8.2.
```

Beim Vorfall am 7. September wurde lesend bestätigt:

```bash
podman exec pflegebetreuer openclaw --version
# OpenClaw 2026.8.2 (0965053)
```

Das Inventar pinnte damals `ghcr.io/openclaw/openclaw:2026.8.2-browser` auf einen
Digest. Die Plugin-Auswahl hatte ein Paket angefordert, dessen Mindest-API neuer war
als die laufende Runtime. Dieser erste Installationsversuch scheiterte an der
Kompatibilitätsprüfung.

Ein Neustart startet dasselbe alte Image. Auch eine neue Clawake-Version erneuert
OpenClaw nicht automatisch. Für das ausgewählte Paket ist eine Runtime mit API
mindestens `2026.9.2` erforderlich. Das offizielle Release
[2026.9.2](https://github.com/openclaw/openclaw/releases/tag/v2026.9.2) existiert.
Die Veröffentlichung eines passenden Browser-Images samt Digest muss vor dem
Upgrade zusätzlich geprüft werden; sie wurde hier nicht verifiziert.

Alternativ kommt eine nachweislich kompatible ältere Plugin-Version oder eine im
alten Image gebündelte Variante infrage. Ob diese für WhatsApp verfügbar und
geeignet ist, ist offen. Die aktuelle Dokumentation unterscheidet explizite
ClawHub-Quellen von gebündelten und npm-Paketen; deren Auflösungsregeln dürfen nicht
ungeprüft auf 2026.8.2 übertragen werden.
[Quelle: OpenClaw-Plugins](https://docs.openclaw.ai/tools/plugin).

## 4. Upgrade vorbereiten

Die Browser-Variante beibehalten, sofern der Pflegebetreuer weiterhin Browserwerkzeuge
benötigt. Vorab Release-Hinweise, Hostanforderungen und Plugin-Kompatibilität prüfen.
Den tatsächlich veröffentlichten Tag und den zugehörigen **Image-Manifest-Digest**
aus der [offiziellen Container-Registry](https://github.com/openclaw/openclaw/pkgs/container/openclaw)
ermitteln; die SHA256-Prüfsumme eines Release-ZIP ist kein Container-Digest.

Für zukünftige Upgrades müssen die folgenden Variablen bewusst gesetzt werden; sie
enthalten hier keine behaupteten Zielwerte:

```bash
: "${CLAWAKE_TARGET_TAG:?Setze den verifizierten Ziel-Tag inklusive Image-Variante}"
: "${CLAWAKE_TARGET_DIGEST:?Setze den passenden sha256-Image-Digest}"
uv run clawake upgrade -c personal-team/team.yml -m pflegebetreuer \
  --to "$CLAWAKE_TARGET_TAG" --digest "$CLAWAKE_TARGET_DIGEST"
```

Diese Vorschau kontaktiert keine Registry. Erst die Ausführung prüft Tag und Digest.
Vorher laufende Arbeit abschließen und ein Wartungsfenster vorsehen. Im aktuellen
Inventar sind Backup vor Mutation und sieben aufzubewahrende Sicherungen aktiviert.

**Aktuelle Backup-Grenze:** Der Backup-Code überspringt fehlende bzw. unlesbare
Pfade teilweise, ohne das Upgrade zuverlässig abzubrechen. Ein erzeugtes Archiv
beweist daher keine vollständige Sicherung. Environment-Dateien außerhalb des
Workspaces sind nicht automatisch enthalten; zusätzliche Quellen benötigen
`backup_policy.paths`. Zugangsdaten und WhatsApp-Sitzungen gehören zur geschützten
Wiederherstellungsbasis. Vor einem wichtigen Upgrade vollständige, lesbare und
wiederherstellbare Sicherungen unabhängig prüfen. Details im
[Upgrade-Playbook](upgrade-playbook.md).

## 5. Upgrade ausführen und prüfen

Nach geprüfter Zielversion und Sicherung:

```bash
uv run clawake upgrade -c personal-team/team.yml -m pflegebetreuer \
  --to "${CLAWAKE_TARGET_TAG:?Ziel-Tag fehlt}" \
  --digest "${CLAWAKE_TARGET_DIGEST:?Ziel-Digest fehlt}" --execute
uv run clawake status -c personal-team/team.yml -m pflegebetreuer
podman exec pflegebetreuer openclaw --version
```

Clawake prüft das Image, stoppt den Dienst, sichert nach Policy, führt Migrationen
im Zielimage aus, aktualisiert das Inventar und die Quadlets und prüft nach dem
Neustart HTTP-Health sowie Runtime-Version und Image-Referenz. Das ist keine atomare
Transaktion und kein Plugin-Kompatibilitätstest. Nicht per Paketmanager oder
Selbstupdate im laufenden Container aktualisieren: Sonst weichen Laufzeit und
gepinntes Inventar voneinander ab.

## 6. WhatsApp nach dem Runtime-Upgrade einrichten

Zuerst die in der gewählten Version verfügbaren Befehle und Plugins prüfen:

```bash
podman exec pflegebetreuer openclaw plugins --help
podman exec pflegebetreuer openclaw plugins list
podman exec pflegebetreuer openclaw channels --help
```

Für den Pflegebetreuer sind npm-Version und Registry-Integrität im Team-Inventar
explizit gepinnt. Installation und Prüfung erfolgen deshalb über Clawake:

```bash
uv run clawake sync-plugins -c personal-team/team.yml -m pflegebetreuer
uv run clawake sync-plugins -c personal-team/team.yml -m pflegebetreuer --execute
```

Clawake lässt keine Tags oder Versionsbereiche zu. Nach der Installation prüft es die
von OpenClaw erfasste aufgelöste Version und SHA-512-Integrität, bevor die weitere
Plugin-Konfiguration angewendet und die Instanz neu gestartet wird.
[Quelle: Plugin-Verwaltung](https://docs.openclaw.ai/tools/plugin).

Vor dem Verbinden die WhatsApp-Zugriffspolitik im OpenClaw-Setup konfigurieren:
Pairing bzw. ausdrücklich erlaubte Absender, begrenzte Gruppen und passende
Werkzeugrechte. Anschließend mit der für die Zielversion bestätigten Syntax:

```bash
podman exec -it pflegebetreuer openclaw channels login --channel whatsapp
podman exec pflegebetreuer openclaw channels status --probe
podman exec pflegebetreuer openclaw security audit
```

QR-Verknüpfung des WhatsApp-Kontos und Freigabe eines Nachrichtensenders sind zwei
verschiedene Schritte. QR und Sitzungsdaten vertraulich behandeln. Einen echten
Nachrichtentest bewusst mit dem eigenen Testkontakt durchführen; ein grüner
systemd-Status reicht nicht als Abnahme.
[Quelle: WhatsApp-Einrichtung](https://docs.openclaw.ai/channels/whatsapp),
[Security CLI](https://docs.openclaw.ai/cli/security).

## 7. Pflege-Vault und geschützten Schlüssel einrichten

`pflege-vault` speichert ausschließlich AES-256-GCM-Ciphertext im persistenten
OpenClaw-State. Der Master-Key darf weder im Chat noch in `team.yml`, `.env`,
Shell-Argumenten oder Logs erscheinen. Im lokalen Control UI unter
`Settings -> Secrets` einen `Protected secret` namens
`PFLEGE_VAULT_MASTER_KEY` anlegen. Für diesen Config-SecretRef bleiben die
Egress-Hosts leer.

Das Team-Inventar pinnt Archiv, Vault-Pfad, SecretRef und die zweifache Agenten-ACL:
Das optionale Tool wird nur für Agent `main` allowgelistet und das Plugin prüft die
Laufzeit-`agentId` zusätzlich selbst. Danach den üblichen Plugin-Sync ausführen.

Ein nicht zustellbarer agentischer Credential-Prompt ist kein Grund, den Schlüssel
im Chat einzufügen. Stattdessen immer die direkte Secrets-Seite oder den interaktiven
`openclaw secrets store set`-Befehl verwenden.

## 8. Fehler und Wiederherstellung

| Symptom | Nächster Schritt |
| --- | --- |
| Mindest-Plugin-API zu neu | Runtime/Plugin-Versionen abgleichen; keine Rechte lockern |
| `npm` meldet `EAI_AGAIN` | DNS im Container und auf dem Host vergleichen; bei einem defekten Podman-Forwarder einen geprüften Resolver über `dns_servers` im Mitglied konfigurieren und Setup erneut anwenden |
| Registry-Prüfung schlägt fehl | Tag, Variante und Digest prüfen; Dienst wurde noch nicht gestoppt |
| Migration schlägt fehl | Logs und Sicherung prüfen; alter Dienst kann neu gestartet worden sein, Zustand kann bereits migriert sein |
| Fehler nach Inventaränderung | Dienst bleibt nach Fehlerbehandlung gestoppt; Recovery-Pfade auswerten |
| Dienst aktiv, WhatsApp offline | Kanaldiagnose, Kontoverknüpfung und Zugriffspolitik prüfen |

Es gibt noch keinen `clawake rollback`-Befehl. Bei Bedarf Dienst stoppen, Sicherung
zuerst in ein separates Verzeichnis entpacken und prüfen, passende Runtime-Daten
und das Image-Paar gezielt wiederherstellen und erst danach Setup ausführen. Ein
altes Image mit bereits migrierten Daten ist kein verlässlicher Rollback.

`teardown --execute` entfernt ausgewählte Container und Quadlets, bewahrt aber
Workspace-Daten. Es ist weder ein Backup noch eine vollständige Löschung von
Zugangsdaten oder personenbezogenen Informationen.

## Getrennter OpenClaw-State

`state_path` ist ein optionaler absoluter Hostpfad je Instanz. Ohne diesen Wert
bleibt `workspace_path/.openclaw` der Standard. Mount, Konfigurationsverwaltung,
Plugin-Ledger, Browser-Cache, Doctor/Upgrade und Backup verwenden denselben Pfad.
Ein außerhalb des Workspace liegender State wird automatisch ins Backup
aufgenommen. Das reguläre Backup meldet übersprungene unlesbare Dateien; für
eine Migration ist ein vollständiges, geprüftes Offline-Backup erforderlich.

`clawake stop -c INVENTAR [--member NAME]` zeigt die Vorschau; `--execute` hält
die ausgewählten Dienste an und erhält Daten und Quadlets. Für den MCP-Dienst
entsprechend `clawake onshape-mcp -c SERVICE --action stop [--execute]`.

## Member readiness and `status`

`status` reports one readiness state per v1 member: `stopped`, `starting`, `ready`,
`degraded` or `failed`. The rules, the probes and the inventory field `readiness` are
described in [inventory-v2.md](inventory-v2.md#member-readiness-readiness-v1-members).

```bash
uv run clawake status -c personal-team/team.yml -m pflegebetreuer
uv run clawake status -c personal-team/team.yml --wait
uv run clawake status -c personal-team/team.yml --wait --timeout 300 --format json
```

- Text output adds `readiness=<state>` to each member line, one `reason: <code>: <detail>`
  line per reason, one `plugin:` line per pinned plugin, and a `WARNING: degraded` line.
- JSON keeps all previous keys and adds `readiness`, `reasons` (`[{code, detail}]`) and
  `plugins` (`[{id, pinned_version, installed_version, status, enabled}]`) per instance.
  The old `state` key (`healthy`/`failed`, systemd only) is deprecated in favour of
  `readiness`.
- `--wait` polls every 2 s until each selected member is `ready` or `degraded`, or until
  its `readiness.start_window_seconds` (default 180 s) has passed; `--timeout <s>`
  overrides the window. Members still not settled become `failed` with reason
  `start_window_exceeded`; stopped members stay `stopped`. `--timeout` requires `--wait`.
  v2 inventories reject both options.
- Exit code: 1 if any member is `stopped` or `failed`; with `--wait` also if any member
  is not `ready` or `degraded` at the end. `degraded` alone exits 0.
- `status` is read-only. It never starts, restarts or installs anything.

## Targeted `setup` and readiness-gated `sync-plugins`

ADR-003 (decision items 3, 4 and 6). Both commands act on v1 members one at a time and
stop at the first failure; later members are not touched. v2 inventories are unchanged.

### `setup`

The preview prints one action per selected member, with its reason:

```text
Member actions:
 - example-lead: restart (example-lead.container)
 - example-dev: none (unchanged, unit_active)
 - example-ops: start (unchanged, unit_inactive)
```

- `restart`: a rendered artifact of the member changed (the file names are listed), or its
  managed OpenClaw runtime config or runtime ledger would change (`runtime_config`).
- `start`: nothing changed, but the unit is not active (`unit_inactive`, `unit_failed`,
  `unit_not_found`, ...). The preview reads the unit state with `systemctl --user status`,
  which is read-only.
- `none`: nothing changed and the unit is active. The member is not restarted and not
  waited on.

With `--execute`, Clawake runs the usual preparation (legacy services, runtime
directories, artifacts, `daemon-reload`). Then, per member in inventory order, it runs
`systemctl --user restart|start` and waits until the member is `ready` or `degraded`,
bounded by `readiness.start_window_seconds`. `degraded` is accepted with a `WARNING`
line. A failed command or a `failed`/`stopped` readiness stops the run, and the command
exits with code 1. A summary table follows:

```text
Setup summary:
  member        action   result   readiness  ready_s  reasons
  example-lead  restart  ok       ready      46       -
  example-dev   none     ok       -          -        -
  example-ops   start    not_run  -          -        -
```

### `sync-plugins`

The preview probes each member once (read-only, as `status` does) and prints per plugin
`version:` (the version inside the pinned archive, or the npm spec version) and an
`action:` line:

- `skip`: the archive digest recorded in the plugin ledger equals the pin, and
  `openclaw plugins list --json` shows the plugin enabled, `loaded` and at the pinned
  version. For npm pins, `openclaw plugins inspect <id> --runtime --json` must show the
  pinned spec and integrity instead of a ledger entry.
- `install (<reasons>)`: reasons are `ledger_missing`, `digest_changed`,
  `version_mismatch`, `plugin_missing`, `plugin_disabled`, `plugin_not_loaded`,
  `integrity_mismatch` or `inspect_unparseable`.
- `install (unverified) (member_not_ready)`: the member is not ready, so the skip rule
  cannot be checked yet. `--execute` decides after the readiness wait.

The preview ends with `Members to restart: ...`; `(if install)` marks members whose
restart depends on an unverified decision.

With `--execute`, per member: wait for `ready`/`degraded` (a `stopped` member fails at once
with "run setup --execute first"), install the plugins that are not skipped, record each
installed archive digest, apply managed plugin config and grants, restart only if
something was installed or the config changed, wait again, and verify that every pinned
plugin is listed at its pinned version and `loaded`. The summary has one row per member
and plugin (action, plugin, version, digest prefix, readiness, seconds to ready).

The installed-archive ledger is the `installedPlugins` object in
`<state>/clawake-managed-plugin-runtime.json`. On the first `sync-plugins --execute` after
this change, every archive plugin is installed once (`ledger_missing`), even if the
running version already matches, because the digest was never recorded. Later runs skip
it.

### `upgrade`

After the restart, `upgrade --execute` waits for readiness for up to
`readiness.start_window_seconds` (default 180 s) instead of `health.retries` ×
`health.interval_seconds`. `degraded` completes with a warning. Any other final state
takes the existing failure path: the upgraded service is stopped, and the backup paths
are printed.

## Runtime pins (ADR-004)

With `runtime_pins`, image tag/digest pins and archive plugin pins live in a private
overlay file instead of the versioned inventory. The format and rules are in
`docs/inventory-v2.md`, section "Runtime pin overlay".

- `clawake validate -c <inventory>` prints
  `runtime pins: <path> (N members, M fields)` when an overlay is in use.
- `clawake pins show -c <inventory> [--format json]` is read-only. It prints every
  effective pin per member and its source: `[inventory]`, `[overlay]`, or
  `[overlay, also in inventory]` for an equal value present in both files (JSON:
  `"source"` and `"also_in_inventory"`).
- `upgrade --execute` with an overlay prints `pins: <path> (inventory file is not
  modified)` in the preview, writes the new image fields to the overlay and prints the
  backup of the previous overlay (`Updated runtime pins; previous pins: ...`; on failure
  `Runtime pins recovery file: ...`). The preview already fails if the inventory still
  contains an image field the upgrade would change (conflict).

### Migration walkthrough (synthetic paths)

1. Create a private directory outside any git checkout:
   `install -d -m 0700 ~/.config/clawake/example-cad`.
2. Preview the overlay:
   `clawake pins init -c deploy/team.yml --output ~/.config/clawake/example-cad/pins.json`.
   The preview prints the exact JSON, the inventory fields to remove
   (`instances[<member>].image.tag`, ..., `instances[<member>].plugins[<id>].sha256`) and
   the top-level `runtime_pins` line to add. It refuses an existing output file.
3. Write it: the same command with `--execute`. It creates the file with mode 0600 and
   never edits the inventory.
4. Add the `runtime_pins` line to the inventory. Until the listed fields are removed,
   both files carry equal values, which is allowed.
5. Remove the listed fields from the inventory.
6. Prove equivalence with read-only commands:
   - `clawake validate -c deploy/team.yml` (shows the `runtime pins:` line);
   - `clawake pins show -c deploy/team.yml` (every pin `[overlay]`);
   - `clawake setup -c deploy/team.yml` (preview: `0/N member(s) changed`, every member
     action `none`);
   - `clawake sync-plugins -c deploy/team.yml` (preview: `action: skip` for every plugin
     that was skipped before the migration).

To roll back the overlay, copy a file from `pins-backups/` over `pins.json`
(`install -m 0600 <backup> pins.json`) and re-run the previews.

## One-step `rollout` (ADR-003 item 5, ADR-004)

```text
clawake rollout -c <inventory> [-m <member>]... [--plugin <id>=<path-to-tgz>]... [--execute]
```

`rollout` reconciles the selected v1 members (default: the whole team) in one reviewed
operation: setup actions, plugin sync, and optionally new archive plugin pins. It never
changes images; `upgrade` stays the image path. v2 inventories exit with code 2.

### Request checks (exit code 2, before any host access)

- `-m` names an unknown member.
- `--plugin` is not `<id>=<path>`, or the same id is given twice.
- `--plugin` is used but the inventory has no `runtime_pins`. Run `clawake pins init` first
  (see "Runtime pins").
- The plugin id is not declared on any selected member, or is declared as an npm plugin.
  A `--plugin` applies to every selected member that declares the id.
- The archive does not exist, is not readable, or has no readable `version` in
  `package/package.json`.
- The candidate state is invalid. This includes a conflict when the inventory itself still
  carries `artifact_path`/`sha256` for the plugin; remove those fields from the inventory
  (step 5 of the migration).

The candidate state is the inventory plus the overlay plus the new pins, validated in
memory exactly as `load_inventory` would. The overlay is not written yet.

### Preview

The preview reads host state only (rendered files, `systemctl --user status`, the readiness
probe, the plugin ledger, the managed config). Per member it prints:

```text
Member example-lead:
  readiness now: ready
  setup: restart (example-lead.container)
  plugin example-cad: 1.2.3 (sha256:d93bcc4e3ea0) -> 1.2.4 (sha256:86c0abb6a376)
    action: install (digest_changed, version_mismatch)
  grants: managed plugin config and agent grants unchanged
  backup: .backups/example-lead-<UTC timestamp>.tar.gz
Overlay backup: .backups/pins-<UTC timestamp>.json
Order with --execute: all backups first; then one member at a time: ...
Members to restart: example-lead
DRY RUN rollout complete. Re-run with --execute.
```

`readiness now` is measured against the committed pins. The `action:` line uses the
`sync-plugins` rules against the candidate pins. A plugin without a new pin shows
`unchanged pin`. A member without any planned change shows `backup: none (no change
planned)` and is not backed up.

### `--execute`

1. **Backups first.** One `backup_instance` archive per member that will change (when
   `backup_policy.enabled` and `pre_mutation`), plus a 0600 copy of the current overlay
   when pins change, all in `.backups/` of the working directory. A failed backup aborts
   before anything is deployed.
2. **Per member, in inventory order:** deploy the rendered candidate files, run
   `daemon-reload` (only if files changed), run the setup action (`restart` / `start` /
   `none`) and wait for readiness within the start window, then run the `sync-plugins`
   steps (install or skip, ledger, config/grants, restart only on change, wait, verify the
   pinned version).
3. **Commit pins.** Only after the member verified, its new pins are written to the
   overlay in one atomic write (the previous overlay goes to `pins-backups/`).
4. **Stop at the first failure.** Later members are not touched and keep their committed
   pins; the failed member keeps its old pins in the overlay. Clawake prints:

   ```text
   Rollout stopped at example-lead: plugin sync or verification failed. Later members were not touched.
   The deployed state of example-lead now differs from the committed runtime pins.
   Backups: .backups/example-lead-20261019T101439Z.tar.gz, .backups/pins-20261019T101439.342141Z.json
   To return to the committed pins, run: clawake rollout -c deploy/team.json -m example-lead --execute
   ```

   and exits with code 1. The suggested command (without `--plugin`) redeploys the
   committed pins.

The summary table is printed on success and on failure:

```text
Rollout summary:
  member        setup    plugin       action   version       digest               result  readiness  ready_s  pins
  example-lead  restart  example-cad  install  1.2.3->1.2.4  sha256:86c0abb6a376  ok      ready      46       yes
  example-dev   none     example-cad  skip     1.2.3         sha256:d93bcc4e3ea0  ok      ready      -        -
```

`pins` is `yes` (committed), `no` (a new pin was not committed) or `-` (no new pin).
A plugin rollout restarts a member twice: once for the new archive mount, once after the
install. Expect about 2 × 46–60 s per member.

Re-running the same command after a successful rollout is a no-op: `skip`, no restart,
no backup, no overlay write. `rollout` without `--plugin` on an unchanged team is a no-op
too.

Differences from `setup`: `rollout` does not retire legacy services and does not check
A2A environment files (it never reads secrets). Run `setup` for those.

## Discord channel from the inventory (ADR-005)

Clawake renders `channels.discord` of each v1 member that declares `openclaw.discord`. It
owns the whole object as the ledger field `channels/discord`. Private IDs come from the
operator-written `site_values` file through `${site:<name>}` references. The bot token is
only ever an env SecretRef. Format and rules are in `docs/inventory-v2.md`, sections
"Site values" and "Discord channel".

- `setup` (preview and `--execute`) exits with code 2 if `token_env` is not a key in any
  of the member's `env_files`. Only key names are parsed; values are never read into
  output, errors or the ledger.
- The preview prints changed key paths below `channels/discord` with IDs as `<id>` and
  `(added)`, `(removed)` or `(changed)` markers. It never prints values.
- After Clawake owns the object, a change made through `openclaw config patch`, the UI or
  an agent blocks the next `setup` with
  `Managed field changed outside Clawake: channels/discord`. Move the change into the
  inventory or the site values file, or revert it. Pairing approvals live in the OpenClaw
  state database and do not trip the guard.
- `rollout` has no `--adopt`. While an adoption is pending, it fails with the same
  `Refusing to overwrite unmanaged OpenClaw field: channels/discord ...` error as `setup`.

### Acceptance check

There is no Discord-specific probe. Set `readiness.channels: true` on every member with
`discord.enabled: true` and run `clawake status -c <inventory> --wait`. The member must
reach `ready`; a Discord account that is not up makes it `degraded` with a
`channel_not_running`, `channel_not_connected` or `channel_lifecycle` reason. Whether a
disabled Discord account counts as "not up" is not verified, so leave
`readiness.channels` unset on disabled members.

### Migration walkthrough for a hand-written block (synthetic values)

The example team has two members: `example-lead` with Discord enabled and
`example-engineer` with Discord disabled. Both carry a hand-written `channels.discord`
with a plaintext string `token`.

1. **Token to the env file and SecretRef.** Clawake never reads, moves or deletes a token
   value; the operator does this by hand on each member.
   - `example-lead`: add `DISCORD_BOT_TOKEN=<bot token>` to the member env file listed in
     `env_files`. In the member's `openclaw.json`, replace `channels.discord.token` with
     `{"source": "env", "provider": "default", "id": "DISCORD_BOT_TOKEN"}`.
   - `example-engineer`: Discord is disabled. Remove `channels.discord.token` from its
     `openclaw.json`. Optionally replace it with a SecretRef instead.
2. **Write the site values file** outside any git checkout:
   `install -d -m 0700 ~/.config/clawake/example-cad`, then create
   `~/.config/clawake/example-cad/site.json` with mode 0600:

   ```json
   {
     "version": 1,
     "cluster": "example-cad",
     "values": {
       "discord.guild": "100000000000000001",
       "discord.lead_channel": "100000000000000002",
       "discord.approvers": ["100000000000000003", "100000000000000004"]
     }
   }
   ```

3. **Add the blocks to the inventory**, with `site_values:
   ${HOME}/.config/clawake/example-cad/site.json` at the top level:

   ```yaml
   # example-lead
   openclaw:
     discord:
       enabled: true
       token_env: DISCORD_BOT_TOKEN
       group_policy: allowlist
       guilds:
         - id: ${site:discord.guild}
           channels:
             - id: ${site:discord.lead_channel}
               users: ${site:discord.approvers}
   readiness: {channels: true}
   # example-engineer
   openclaw:
     discord: {enabled: false}
   ```

4. **Read-only checks:**
   - `clawake validate -c deploy/team.yml` prints
     `site values: <path> (3 values, 3 references)` and no `unused values` warning.
   - `clawake setup -c deploy/team.yml` (no flag) stops with
     `Refusing to overwrite unmanaged OpenClaw field: channels/discord (named adoption
     'discord-channel': review the preview, then run setup with --adopt discord-channel)`.
     This confirms that both blocks are still unowned.
   - `clawake setup -c deploy/team.yml --adopt discord-channel` previews the adoption. If
     a token is still a string, it stops with `Cannot adopt channels/discord of member
     <member>: the live token in <path> is a plaintext string. ...`; repeat step 1.
     Otherwise it lists the changed `channels/discord/...` key paths (IDs as `<id>`) and
     ` - <member>: adopt discord-channel (backup first: .backups/<member>-<UTC
     timestamp>.tar.gz)` for each member.
5. **Execute:** `clawake setup -c deploy/team.yml --adopt discord-channel --execute`.
   For each adopting member, a `backup_instance` archive is written to `.backups/` before
   any file changes. A failed backup stops the run before anything is written. Then setup
   replaces the objects, records ownership and restarts the changed members one at a time.
6. **Accept:** `clawake status -c deploy/team.yml -m example-lead --wait` must report
   `ready` (see "Acceptance check").
7. **Idempotence:** the same `setup ... --adopt discord-channel` preview now shows
   `0/2 member(s) changed` and no `Adoptions:` block. From now on, run plain `setup`.

To roll back, restore the member's `openclaw.json` and `clawake-managed-runtime.json`
from the archive in `.backups/`.
