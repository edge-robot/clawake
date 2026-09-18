# Robotik-Leitungsteam

Drei dauerhafte OpenClaw-Gateways führen je einen Verantwortungsbereich. Jede Leitung
kann innerhalb ihrer Instanz temporäre Fachagenten erzeugen, deren Ergebnisse prüfen und
zusammenführen. Clawake verwaltet Deployment und Konfiguration; OpenClaw erledigt Delegation
und Kommunikation.

| Instanz | Leitung | Dashboard |
| --- | --- | --- |
| robotics-product-owner | Product & Systems Director, Product Owner mit technischer Gesamtverantwortung und Tony-Fadell-inspiriertem Stil | http://127.0.0.1:19089 |
| robotics-engineer | Engineering Director für Hardware, Software, Integration und Qualität | http://127.0.0.1:19189 |
| project-partner-manager | Program & Partnerships Director für Projekt-, Kunden- und Lieferantenkoordination | http://127.0.0.1:19289 |

## Kommunikation und Delegation

Produktleitung und Engineering-Leitung sowie Produktleitung und Programmleitung sind über
den offiziellen A2A-Kanal verbunden. Die beiden Fachleitungen stimmen sich zunächst über
die Produktleitung ab. Konfigurierte Peer-Namen sind bei der Produktleitung
`a2a:engineering` und `a2a:partnerships`, bei den Fachleitungen jeweils `a2a:product`.

Jede Instanz veröffentlicht für A2A nur die Leitung `main` und routet eingehende
A2A-Nachrichten explizit an diese. Ein Auftrag enthält ID, Ziel, Kontext, Grenzen und
Abnahmekriterien. Antworten gehören zum ursprünglichen Auftrag. Eine Annahme oder ein
Timeout gilt nicht als Abschluss; Arbeit darf nicht blind doppelt versendet werden.

Innerhalb jeder Instanz delegiert die Leitung über `sessions_spawn`. Die Konfiguration
begrenzt dies auf drei parallele Subagenten, maximal drei aktive Kinder pro Session,
eine Delegationsebene und standardmäßig 900 Sekunden pro Lauf. Das sind maximal neun
parallele Subagenten im Team. Es gibt kein zusätzliches teamweites Budget in Clawake.
`tools.sessions.visibility=tree` begrenzt den Session-Zugriff nach OpenClaws Regeln;
die Hauptsession darf dabei auch andere Sessions desselben Agenten sehen.

Subagenten besitzen getrennte Sessions, aber nicht automatisch getrennte Dateisysteme.
Die Leitungen müssen Schreibzugriffe aufteilen und Ergebnisse prüfen. Die Rollen regeln
dies ausdrücklich; echte Werkzeugbeschränkungen bleiben zusätzlich OpenClaw-Aufgabe.
Ein gemeinsamer Dateimount oder eine README als Nachrichtenbus ist nicht vorgesehen.
Projektrepositories oder Artefaktspeicher können später gezielt angebunden werden.

## Netzwerke und Ports

Alle Mitglieder nutzen das gemeinsame Podman-Netz `robotics-coordination`.
A2A-Ziele sind beispielsweise `http://robotics-engineer:18789/a2a/v1`.
Der interne Gateway-Port 18789 darf sich wiederholen, weil jeder Container einen
eigenen Netzwerk-Namensraum besitzt. A2A nutzt denselben HTTP-Server wie Gateway und
Dashboard; dafür wird kein zweiter Port veröffentlicht. Gleiches gilt für interne
Browser/CDP-Ports.

Am Host werden nur folgende Loopback-Ports veröffentlicht:

| Instanz | Gateway, Dashboard und A2A |
| --- | --- |
| Produktleitung | 19089 |
| Engineering-Leitung | 19189 |
| Programmleitung | 19289 |

Clawake erkennt innerhalb eines Inventars doppelte Host-Portbelegungen einschließlich
Wildcard-Überlappungen und normalisierter IPv6-Adressen. TCP und UDP bleiben getrennt.
Unabhängige Inventare und bereits laufende fremde Prozesse sind nicht Teil dieser
statischen Validierung. Vor dem Start die aktuelle Hostbelegung mit `ss -ltn` prüfen.
Die Hostprüfung am 18. September 2026 ergab keine Belegung der oben genannten Ports.

Das Netzwerk erlaubt ausgehende Modell- und Recherchezugriffe. Es ist kein abgeschottetes
`Internal=true`-Netz. Der Loopback-Hostzugang und die A2A-Authentifizierung bleiben aktiv.
Mitgliedsbezogenes Setup legt das gemeinsame Netz bei Bedarf an. Teardown erhält es,
solange andere im Inventar eingetragene Mitglieder es nutzen; beim gemeinsamen Teardown
wird die Netzwerk-Unit gestoppt und ihre Definition entfernt.

## OpenClaw 2026.9.4

Die Image-Digests wurden am 18. September 2026 direkt aus GHCR für diese Tags abgerufen:

| Tag | Manifest-Digest |
| --- | --- |
| 2026.9.4 | `sha256:cc596b846506a5f4cfcee111394a2725f375f01cca2ebb492a161fd1b747f101` |
| 2026.9.4-browser | `sha256:0862ab9a097166049800a6c86026b035b768af0949f651d0e73222555b28fbbd` |

Produkt- und Programmleitung nutzen das Browser-Image, die Engineering-Leitung das
Standardimage. A2A ist gebündelt und wird über die Laufzeitkonfiguration aktiviert;
eine separate npm-Installation ist nicht erforderlich. Die Peer-Felder wurden gegen
das [Release-Schema](https://github.com/openclaw/openclaw/blob/v2026.9.4/extensions/a2a/src/config-schema.ts)
geprüft. Die Konfiguration ersetzt keinen realen Kommunikations- und Modellzugangstest.

## Vorbereitung

Im Repository-Verzeichnis:

```bash
export CLAWAKE_PROJECT_ROOT="$PWD"
mkdir -p robotics-team/env
cp robotics-team/env.example robotics-team/env/product-owner.env
cp robotics-team/env.example robotics-team/env/robotics-engineer.env
cp robotics-team/env.example robotics-team/env/project-partner-manager.env
chmod 600 robotics-team/env/*.env
```

Pro Instanz einen eigenen langen zufälligen `OPENCLAW_GATEWAY_TOKEN` und die benötigten
Provider-Zugangsdaten setzen. Vier weitere unabhängige Zufallstokens bilden die
gerichteten A2A-Verbindungen; derselbe Token muss an beiden Enden einer Richtung stehen:

| Variablen | Benötigte Environment-Dateien |
| --- | --- |
| A2A_PRODUCT_TO_ENGINEERING, A2A_ENGINEERING_TO_PRODUCT | product-owner.env und robotics-engineer.env |
| A2A_PRODUCT_TO_PARTNERSHIPS, A2A_PARTNERSHIPS_TO_PRODUCT | product-owner.env und project-partner-manager.env |

Nicht benötigte Variablen dürfen leer bleiben. Gateway-Tokens nicht für A2A wiederverwenden.
Environment-Dateien und Laufzeitdaten sind ignoriert. Keine Zugangsdaten in Git eintragen.
Die `USER.md`-Dateien enthalten ausschließlich nicht geheimen Projektkontext.

## Vorschau und Inbetriebnahme

```bash
uv run clawake validate -c robotics-team/team.yml
uv run clawake setup -c robotics-team/team.yml
```

Beide Befehle schreiben nichts und benötigen keine ausgefüllten Secrets. Die Vorschau
umfasst Quadlets, OpenClaw-Konfiguration und das interne Eigentumsprotokoll. Es werden nur
Pfade und geänderte Schlüsselnamen angezeigt, keine Konfigurationswerte.

Erst die Ausführung installiert die Dateien und startet die ausgewählten Services:

```bash
uv run clawake setup -c robotics-team/team.yml --execute
uv run clawake onboard -c robotics-team/team.yml -m robotics-product-owner --execute
uv run clawake onboard -c robotics-team/team.yml -m robotics-engineer --execute
uv run clawake onboard -c robotics-team/team.yml -m project-partner-manager --execute
uv run clawake status -c robotics-team/team.yml
uv run clawake dashboard -c robotics-team/team.yml
```

Setup prüft vor Schreibzugriffen die erforderlichen A2A- und Gateway-Variablen in den
Environment-Dateien. Anschließend pro Leitung Modellzugang und tatsächlich verfügbare
Werkzeuge prüfen. Einen einfachen A2A-Auftrag mit eindeutiger ID und Rückantwort sowie
einen Subagentenauftrag testen, bevor reale Projektarbeit delegiert wird.
A2A bietet laut [Dokumentation](https://docs.openclaw.ai/channels/a2a) derzeit keine
zuverlässige Remote-Abbruchfunktion; ein lokaler Stop beendet nicht automatisch Arbeit
auf einer anderen Instanz.

## Konfigurationsbesitz und Sicherung

Clawake verwaltet nur seine deklarierten Laufzeitfelder. Ein ignoriertes
`clawake-managed-runtime.json` speichert diese Zuordnung mit Secret-Verweisen statt
Tokenwerten. Entfernte Peers werden beim nächsten Setup des betroffenen Mitglieds
entfernt. Bei Änderungen an einer Verbindung beide Enden anwenden, am einfachsten
durch Setup des gesamten Teams. Bestehende abweichende, bisher unverwaltete Felder
führen zu einem Konflikt statt zu stiller Überschreibung. Ein vorhandenes
`plugins.allow` muss A2A ausdrücklich zulassen.

Setup, Onboarding-Abgleich und Upgrade verwenden denselben Konfigurationsplan.
Upgrade berechnet ihn nach einer OpenClaw-Migration erneut. Jede Datei wird atomar ersetzt;
die gesamte Mehrdatei-Operation ist keine Transaktion. Änderungen an Dateien zwischen
Planung und Anwendung brechen die Anwendung ab, bevor geplante Dateien ersetzt werden.

Die Backup-Policy hält drei Upgrade-Sicherungsstände je Instanz. Setup erzeugt derzeit
keine automatische Sicherung. Environment-Dateien benötigen eine separate geschützte
Sicherung. Der bestehende Backup-Code kann unlesbare Pfade überspringen; vor wichtigen
Upgrades die Wiederherstellbarkeit prüfen.

Die vorhandene, maskierte `openclaw.service` wird durch dieses Inventar nicht verändert.
