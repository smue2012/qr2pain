# qr2pain

[![Tests](https://github.com/smue2012/qr2pain/actions/workflows/tests.yml/badge.svg)](https://github.com/smue2012/qr2pain/actions/workflows/tests.yml)
[![Docker-Image](https://github.com/smue2012/qr2pain/actions/workflows/docker.yml/badge.svg)](https://github.com/smue2012/qr2pain/pkgs/container/qr2pain)

Exportiert Schweizer QR-Rechnungen aus **paperless-ngx (v2/v3)** als Zahlungsdatei
**pain.001.001.09** (Swiss Payment Standards 2026) für den Upload ins E-Banking –
wahlweise per **Webfrontend** (prüfen, korrigieren, auswerten) oder per **Kommandozeile**.

> DTA wird von Schweizer Banken seit 2018 nicht mehr angenommen; pain.001.001.03 läuft im
> November 2026 aus. qr2pain erzeugt deshalb ausschliesslich pain.001.001.09.

## Schnellstart mit Docker / Portainer

qr2pain läuft als eigener Container neben paperless-ngx und spricht es über das Docker-Netzwerk an.

1. **Konfiguration anlegen:** `config.example.toml` als `config.toml` speichern und anpassen: Belastungskonto unter `[debtor]`,
   paperless-Adresse unter `[paperless] url`. Die Adresse ist der Containername des paperless-Webservers, immer mit Port 8000,
   z. B. `http://paperless-ngx-webserver-1:8000`. Leserechte setzen: `chmod 644 config.toml`.
2. **Netzwerk von paperless ermitteln:** `docker network ls`, typischerweise `<stackname>_default`.
3. **Stack anlegen** mit [`docker-compose.yml`](docker-compose.yml): Pfad zur `config.toml` (absolut) und Netzwerknamen eintragen.
4. `http://<host>:8012` öffnen und mit dem paperless-Benutzer anmelden.

**Updates:** In Portainer beim Stack «Update the stack» mit «Re-pull image» ausführen. Die Datenbank im Volume `qr2pain-data`
bleibt erhalten und wird bei Bedarf automatisch migriert.

| Image-Tag | Inhalt |
|---|---|
| `latest` | letzte Release-Version (empfohlen) |
| `1.0`, `1.0.0` | feste Version |
| `edge` | aktueller Stand von `main` |

> Nach Änderungen an der `config.toml` genügt ein Neustart des Containers.
> Eine Stack-Aktualisierung braucht es nur bei Änderungen an Ports, Volumes, Netzwerk oder Image.

## Webfrontend

### Funktionen

**Zahlungen**
- Übersicht aller Rechnungen mit Tag «QR zu zahlen»: Empfänger, Referenz, Fälligkeit, Ausführungsdatum, Betrag, Status
- Filter «Bereit», «Mit Problemen», «Zurückgestellt», «Alle offenen» sowie Suche und Sortierung
- Detailansicht mit Dokumentvorschau und Änderungsprotokoll
- **Korrigieren**:
  - Betrag, z. B. für Skonto oder eine Teilzahlung
  - Ausführungsdatum pro Zahlung
  - IBAN, Referenz, Mitteilung und Adresse
  - interner Kommentar

  Die Werte aus dem QR-Code bleiben erhalten. Jede Änderung wird protokolliert und lässt sich einzeln zurücksetzen.
- Dokumente ohne lesbaren QR-Code: Die Zahlungsdaten können von Hand erfasst werden.
- **Zurückstellen / Freigeben** einzelner Rechnungen oder einer Auswahl
- **Ratenzahlung**: Eine Rechnung lässt sich in Raten mit eigenem Betrag und eigenem Datum aufteilen.
  - Vorschlag nach Anzahl und Abstand (monatlich, 14-täglich, wöchentlich, vierteljährlich), Rundungsdifferenz auf der letzten Rate, Wochenenden auf Montag verschoben
  - Jede Rate ist in der Liste eine eigene Zeile und wird einzeln oder zusammen exportiert.
  - In der Zahlung steht «Teilzahlung n/N», die EndToEndId endet auf `-T<n>`.
  - In paperless erhält die Rechnung das Tag «Ratenzahlung» (`[tags] installments`). Als exportiert gilt sie erst nach der letzten Rate.
  - Nach der ersten exportierten Rate sind Betrag und Empfängerdaten gesperrt. Offene Raten bleiben änderbar.
- **Duplikatprüfung** gleicht mit anderen offenen Rechnungen und mit allen bisherigen Exporten ab (IBAN + Referenz + Betrag). Mögliche Duplikate werden blockiert, bis sie bestätigt sind.
- **pain.001 erstellen** aus der Auswahl: Vorher erscheint eine Zusammenfassung pro Ausführungsdatum und Währung. Danach markiert das Tool die Dokumente in paperless als exportiert und hängt eine Notiz an.

**Auswertungen** (pro Währung)
- Kennzahlen: offen, fällig in 7 Tagen, überfällig, zurückgestellt, mit Problemen
- **Liquiditätsvorschau** nach Ausführungsdatum (siehe unten)
- offene Beträge nach Empfänger
- bezahlte Beträge pro Monat und Top-Empfänger der letzten 12 Monate
- Liste der Fehler und Duplikate

**Exporte**
- Verlauf aller Zahlungsdateien mit Positionen
- XML erneut herunterladen
- **Export rückgängig machen**: Die Rechnungen werden wieder offen und in paperless zurückgetaggt. Nur verwenden, wenn die Datei nicht bei der Bank ausgeführt wurde.

### Liquiditätsvorschau

Die Vorschau zeigt die Abflüsse pro Periode, gestapelt nach **offen** und **exportiert, geplant**. Geplant sind
Zahlungen, deren pain.001 schon erstellt ist, deren Ausführungsdatum aber noch nicht erreicht ist. Überfällige
Rechnungen stehen in einem eigenen roten Balken vorne, alles nach dem Zeitraum in «Später».

- **Zeitraum:** «Automatisch» reicht bis zur letzten geplanten Zahlung und wählt die Einteilung selbst:
  bis 31 Tage täglich, bis 16 Wochen wöchentlich, sonst monatlich (höchstens 24 Monate).
  Fest wählbar sind 30 Tage, 8 Wochen, 3 Monate, 6 und 12 Monate. Die Wahl bleibt im Browser gespeichert.
- **Kontostand heute** (optional): Ist für das gewählte Konto und die Währung ein Stand erfasst, erscheint darunter
  der Kontostand-Verlauf nach allen Zahlungen, dazu die Kennzahl «Tiefster Kontostand» mit Warnung, ab wann das Konto
  ins Minus fällt. Eingaben wie `12'345.60` sind erlaubt, ein leeres Feld löscht den Stand.
  Gespeichert wird nach kurzer Tipp-Pause oder mit Enter. Bei «Alle Konten» mit mehreren Konten wählt man das Konto
  direkt neben dem Feld; der Verlauf rechnet dann mit der Summe der erfassten Stände.
  Eingaben werden mit Datum und Benutzer protokolliert.

### Zahlungskonten

Für getrennte Firmen oder Bereiche lassen sich im Tab **«Konten»** mehrere Belastungskonten verwalten:
Bezeichnung, Kontoinhaber mit Adresse, IBAN, BIC, optional eine Währung. Anlegen und Ändern dürfen nur
**paperless-Superuser**, alle anderen sehen die Konten und wählen sie beim Export aus.

Jede Rechnung erhält ihr Konto automatisch:
1. **Währung:** Ein Konto «nur EUR» kommt nur für EUR-Zahlungen in Frage, ein Konto mit passender Währung hat Vorrang.
2. **Regeln:** paperless-Tag, Korrespondent oder Speicherpfad, z. B. Tag «Firma B» → Konto Firma B.
3. **Standardkonto,** wenn keine Regel passt. Bei mehreren Treffern entscheidet die Reihenfolge-Nummer.

Beim Export sind die Zahlungen nach Konto gruppiert. Das Konto lässt sich pro Gruppe übersteuern. **Pro Konto entsteht
eine eigene pain.001-Datei,** die im E-Banking des jeweiligen Kontos hochgeladen wird. Konten, die schon in Exporten
vorkommen, werden beim Löschen nur deaktiviert.

Beim ersten Start wird das Konto aus `[debtor]` der `config.toml` als «Standard» übernommen.

**Verbuchung auf dem Kontoauszug** (pain.001-Feld `BtchBookg`), pro Konto voreingestellt und im Export-Dialog
pro Datei änderbar:

| Einstellung | pain.001 | Wirkung |
|---|---|---|
| Sammelbuchung (Standard) | `true` | eine Belastung pro Ausführungsdatum und Währung (Sammelbeleg) |
| Einzelbuchung | `false` | jede Zahlung als eigene Buchung, einfacher abzugleichen |
| Vorgabe der Bank | Feld fehlt | es gilt die Einstellung im E-Banking-Vertrag |

Einzelbuchungen kosten je nach Bank eine Gebühr pro Buchung.

### Zahlbetrag und Zahlungsdatum in paperless

Nach jedem Export schreibt qr2pain zwei benutzerdefinierte Felder ins paperless-Dokument, sofern es sie gibt
(Namen einstellbar mit `paid_amount_field` / `paid_date_field` unter `[web]`, Standard `zahlbetrag` und `zahlungsdatum`):

| Situation | zahlbetrag | zahlungsdatum |
|---|---|---|
| ganze Rechnung exportiert | bezahlter Betrag | Ausführungsdatum |
| Ratenzahlung, noch Raten offen | offener Restbetrag | leer |
| alle Raten exportiert | Summe aller Raten | Ausführungsdatum der letzten Rate |
| Export rückgängig gemacht | wird neu berechnet bzw. geleert | wird neu berechnet bzw. geleert |

Das Feld darf dasselbe sein wie `amount_field` (Betrag für Rechnungen ohne QR-Betrag). qr2pain liest den Betrag dann
nur, solange noch nichts exportiert ist, und stellt beim «Rückgängig» den ursprünglich erfassten Betrag wieder her.

Geschrieben wird mit dem paperless-Konto des angemeldeten Benutzers: Fehlt das Änderungsrecht am Dokument, lehnt
paperless die Änderung ab und qr2pain zeigt eine Warnung. Die übrigen Felder des Dokuments bleiben unverändert.

Am besten legst du «zahlbetrag» als Feldtyp *Geldbetrag* und «zahlungsdatum» als *Datum* an. Das Zahlungsdatum ist
das gewünschte Ausführungsdatum aus der pain.001, nicht die tatsächliche Belastung durch die Bank.

### Ausführungsdatum

Standard ist die Fälligkeit minus `lead_days`, auf den vorherigen Bankwerktag gelegt, frühestens aber
der nächste Bankwerktag. Die Fälligkeit kommt aus einem paperless-Datumsfeld (`due_field`). Ohne dieses Feld gilt
Dokumentdatum + `default_terms_days`; das Frontend kennzeichnet sie dann als «geschätzt».

### Anmeldung und Rechte

- Angemeldet wird mit dem **paperless-Benutzer**. qr2pain holt dafür über `/api/token/` den Token dieses Benutzers.
- **Jeder Benutzer sieht nur Rechnungen, deren Dokument er in paperless sehen darf.** Das gilt für Liste, Detailansicht,
  Vorschau, Auswertungen und Exporthistorie. Korrigieren, Zurückstellen, Aufteilen, Exportieren, XML-Download und
  Rückgängig werden zusätzlich auf dem Server geprüft.
- qr2pain fragt die Berechtigungen gebündelt bei paperless ab und merkt sie sich pro Anmeldung
  (`access_cache_seconds`, Standard 60 s). Geänderte Rechte in paperless greifen spätestens danach.
- Enthält ein Export auch Rechnungen, die du nicht sehen darfst, siehst du nur deine Positionen und einen Hinweis.
  Download und Rückgängig sind dann nur für Benutzer möglich, die alle Positionen sehen.
- Beim Laden aus paperless wird eine Rechnung nur entfernt, wenn das Tag «QR zu zahlen» tatsächlich fehlt.
  Als gelöscht gilt ein Dokument nur, wenn ein paperless-Superuser es nicht mehr findet. Fehlende Rechte des ladenden
  Benutzers entfernen nichts.
- Tagging und Notizen laufen mit den Rechten des angemeldeten Benutzers. `allowed_users` schränkt den Zugang zusätzlich ein.
- Sessions liegen im Arbeitsspeicher. Nach einem Neustart muss man sich neu anmelden.

### Performance

- Berechnete Zahlungsdaten werden zwischengespeichert und nur nach Änderungen neu berechnet
  (2000 Rechnungen: rund 150 ms nach einer Änderung, sonst unter 1 ms).
- Beim Laden aus paperless werden nur neue oder geänderte Dokumente heruntergeladen, parallel
  (`download_workers`, Standard 4). Der QR-Code wird von der letzten Seite her gesucht, zuerst mit niedriger Auflösung.
- Vorschaubilder werden zwischengespeichert, API-Antworten komprimiert (gzip).

### Installation auf einem Server (systemd + nginx)

```bash
sudo useradd --system --home /opt/qr2pain qr2pain
sudo mkdir -p /opt/qr2pain /etc/qr2pain /var/lib/qr2pain
sudo cp -r qr2pain requirements.txt /opt/qr2pain/
sudo python3 -m venv /opt/qr2pain/.venv
sudo /opt/qr2pain/.venv/bin/pip install -r /opt/qr2pain/requirements.txt
sudo cp config.example.toml /etc/qr2pain/config.toml      # anpassen!
sudo chown -R qr2pain: /var/lib/qr2pain && sudo chmod 640 /etc/qr2pain/config.toml && sudo chgrp qr2pain /etc/qr2pain/config.toml
sudo cp deploy/qr2pain-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now qr2pain-web
```

Danach nginx gemäss `deploy/nginx.conf` einrichten: eigener Hostname oder Unterpfad, HTTPS ist Pflicht.
Das Webfrontend soll nur im internen Netz oder per VPN erreichbar sein. Für den Betrieb mit Docker siehe «Schnellstart» oben.

Für Monitoring gibt es `GET /api/health` (ohne Anmeldung). Das Docker-Image nutzt ihn als Healthcheck.

**Backup:** `/var/lib/qr2pain/qr2pain.sqlite` enthält Korrekturen, Exporthistorie und Protokoll.

### paperless vorbereiten

1. Tag **«QR zu zahlen»** anlegen. «QR exportiert» legt qr2pain selbst an.
2. Optional das Datumsfeld **«Fällig am»** anlegen und bei Rechnungen befüllen, z. B. per Workflow.
3. Optional ein Monetary-Feld **«Betrag»** für QR-Rechnungen ohne Betrag.
4. Den qr2pain-Benutzern Leserechte auf die Rechnungen geben und Änderungsrechte für Tags und Notizen.

## Kommandozeile

```bash
python -m qr2pain -c config.toml [--dry-run] [-d 2026-10-01] [-o datei.xml] [--limit N]
```

Die Kommandozeile verarbeitet alle Dokumente mit «QR zu zahlen» ohne Korrekturen, mit dem API-Token aus
`[paperless] token`. Korrekturen aus dem Webfrontend kennt sie nicht. Verwende deshalb pro Zahlungslauf eines von beiden.
Exit-Code 0 = ok, 1 = mindestens ein Dokument fehlerhaft, 2 = Konfigurationsfehler.

## Mapping (SPS IG 2.3, Annex B)

- QR-IBAN + QRR → `Strd/CdtrRefInf/Tp/CdOrPrtry/Prtry = QRR`; IBAN + SCOR → `Cd = SCOR`
- Mitteilung und Rechnungsinformationen (`//S1/…`) → `AddtlRmtInf` bzw. `Ustrd`
- Adresse Typ S → strukturiert; Typ K (alte Rechnungen) → hybrid (PLZ/Ort strukturiert, Strasse als `AdrLine`)
- ein B-Level pro Währung **und** Ausführungsdatum
- Debtor Agent: BIC aus der Konfiguration, sonst die IID aus der IBAN (`CHBCC`)
- EndToEndId = `PL<Dokument-ID>-ASN<Archivnummer>` → Rückverfolgung bis ins paperless-Dokument

## Entwicklung

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

Die Tests laufen bei jedem Push automatisch auf GitHub:
- Parser und Prüfziffern
- Validierung der pain.001-Dateien gegen das ISO-Schema (`tests/xsd/`)
- Kommandozeile end-to-end
- Webfrontend mit zwei paperless-Benutzern: Rechte, Duplikate, Raten, Export und Rückgängig

**Lokal ausprobieren ohne echtes paperless:** `python tests/mock_paperless.py 8765` startet ein simuliertes paperless
mit 10 Beispielrechnungen und zwei Benutzern: `stephan`/`geheim` sieht alles, `buchhaltung`/`geheim2` nur 5 Dokumente.
Dazu `[paperless] url = "http://127.0.0.1:8765"` setzen und
`QR2PAIN_CONFIG=config.toml uvicorn qr2pain.web.app:app --port 8010` starten.

**Release:** Version in `qr2pain/__init__.py` erhöhen, `CHANGELOG.md` ergänzen,
dann auf GitHub ein Release mit Tag `v1.0.1` (bzw. der neuen Version) anlegen. GitHub baut das Image und veröffentlicht es als `latest`.

## Lizenz

MIT, siehe [LICENSE](LICENSE). Ohne Gewähr: Zahlungsdateien vor der Freigabe im E-Banking prüfen.