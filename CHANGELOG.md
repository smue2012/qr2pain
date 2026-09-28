# Changelog

## 1.4.1 – 2026-09-28
- Zahlungsliste: neue Spalte «Speicherpfad» (sortierbar, in der Suche enthalten), auch in der Detailansicht

## 1.4.0 – 2026-09-28
- **Mehrere Zahlungskonten** (z. B. für getrennte Firmen): Verwaltung im neuen Tab «Konten», nur für paperless-Superuser
- Automatische Zuordnung pro Rechnung: Währung, dann Regeln nach paperless-Tag, Korrespondent oder Speicherpfad, sonst Standardkonto
- Export gruppiert nach Konto, Konto pro Gruppe übersteuerbar; pro Konto eine eigene pain.001-Datei
- Kontospalte und Kontofilter in der Zahlungsliste, Kontofilter in den Auswertungen, Konto in der Exporthistorie
- Beim Update wird das Konto aus der `config.toml` als «Standard» übernommen; bisherige Exporte werden ihm zugeordnet

## 1.3.0 – 2026-09-28
- Statuszeile unten links mit der Programmversion (Link zu den Versionshinweisen), auch auf der Anmeldeseite
- GitHub Actions auf aktuelle Versionen (Node.js 24) umgestellt, Runner fest auf Ubuntu 24.04

## 1.2.1 – 2026-09-28
- GitHub-Repository mit Actions für Tests und Docker-Image (ghcr.io, amd64 und arm64)
- `docker-compose.yml` mit Image aus der Registry
- Healthcheck-Endpunkt `/api/health` und Docker-`HEALTHCHECK`
- Herstellerangabe in der pain.001-Datei neutral («qr2pain contributors»)
- Tests als pytest-Suite, ISO-Schema für die Validierung im Repository

## 1.2.0 – 2026-09-28
- **Zugriff nach paperless-Rechten**: Jeder Benutzer sieht und bearbeitet nur Rechnungen, deren Dokument er in paperless sehen darf. Das gilt für Liste, Detail, Vorschau, Auswertungen, Exporthistorie, XML-Download und Rückgängig.
- Laden aus paperless entfernt Rechnungen nur, wenn das Tag wirklich fehlt; fehlende Rechte entfernen nichts mehr.
- Performance: Berechnungen werden zwischengespeichert, Downloads laufen parallel, der QR-Scan beginnt auf der letzten Seite, Vorschaubilder werden zwischengespeichert, API-Antworten komprimiert.
- Behoben: PDF-Bibliothek konnte bei gleichzeitiger Vorschau und Laden abstürzen.
- Behoben: Rechnung ohne Referenz wurde nicht als Duplikat einer bereits bezahlten erkannt.

## 1.1.0 – 2026-09-27
- **Ratenzahlung**: Rechnungen in Raten mit eigenem Betrag und Datum aufteilen, Raten einzeln exportieren, Tag «Ratenzahlung» in paperless.
- Exportdateien tragen die Exportnummer im Namen.
- Datenbank wird automatisch migriert.

## 1.0.1 – 2026-09-27
- Docker: Login-Cookie `secure_cookie = "auto"`, Datenordner fix `/data`.

## 1.0.0 – 2026-09-27
- Erste Version: Kommandozeile und Webfrontend (Liste, Korrekturen, Auswertungen, Exporte), pain.001.001.09 nach SPS 2026.
