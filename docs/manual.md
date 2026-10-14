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
verwaltete OpenClaw-Einstellungen, installiert Quadlets und startet die ausgewählten
Dienste neu – auch bei unveränderten Quadlets. Onboarding öffnet den interaktiven
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
