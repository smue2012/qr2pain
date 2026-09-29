# Changelog

## 1.3.1 – 2026-09-29

- «zahlbetrag» kann zugleich das Betragsfeld für Rechnungen ohne QR-Betrag sein: Nach dem ersten Export wird es nicht
  mehr als Rechnungsbetrag gelesen, «Rückgängig» stellt den ursprünglichen Betrag wieder her

## 1.3.0 – 2026-09-29

- Zahlbetrag und Zahlungsdatum werden nach dem Export in benutzerdefinierte paperless-Felder geschrieben
  (`zahlbetrag`, `zahlungsdatum`; bei Raten der offene Restbetrag, Datum erst nach der letzten Rate)
- «Rückgängig» berechnet die Felder neu bzw. leert sie

## 1.2.0 – 2026-09-29

**Verbuchungsart (BtchBookg)**
- pro Zahlungskonto wählbar: Sammelbuchung (Standard, wie bisher), Einzelbuchung oder Vorgabe der Bank
- im Export-Dialog pro Datei übersteuerbar; die gewählte Art steht in der Exporthistorie
- Kommandozeile: `booking = "batch" | "single" | "bank"` im Abschnitt `[debtor]`

## 1.1.1 – 2026-09-29

- Kontostand wird nach kurzer Tipp-Pause oder mit Enter gespeichert, mit Rückmeldung «✓ gespeichert» neben dem Feld
- bei «Alle Konten» mit mehreren Konten erscheint neben dem Feld eine Kontoauswahl, statt dass das Feld gesperrt ist
- ungültige Eingaben werden direkt beim Feld gemeldet

## 1.1.0 – 2026-09-29

**Liquiditätsvorschau**
- Zeitraum wählbar: automatisch (Tage, Wochen oder Monate bis zur letzten geplanten Zahlung), 30 Tage, 8 Wochen, 3, 6 oder 12 Monate
- bereits exportierte Zahlungen mit Ausführungsdatum in der Zukunft erscheinen als «exportiert, geplant»
- überfällige Rechnungen als eigener Balken, Zahlungen nach dem Zeitraum unter «Später»
- optionales Feld «Kontostand heute» pro Konto und Währung mit Kontostand-Verlauf und Kennzahl «Tiefster Kontostand»
- runde Achsenbeschriftung, Legende und Tooltips mit Aufteilung

**Korrekturen**
- CSRF-Prüfung gilt auch für PUT-Anfragen
- Navigation läuft auf schmalen Bildschirmen nicht mehr über den Rand

## 1.0.0 – 2026-09-28

Erste Version.

**Export**
- Swiss QR-Rechnungen aus paperless-ngx (v2/v3) als pain.001.001.09 nach Swiss Payment Standards 2026
- QR-Code-Erkennung in PDFs und Bildern, Prüfung von IBAN/QR-IBAN, QR-Referenz und Creditor Reference
- ein B-Level pro Währung und Ausführungsdatum, Rückverfolgung über EndToEndId `PL<Dokument>-ASN<Archivnummer>`
- Kommandozeile (`python -m qr2pain`) und Webfrontend

**Webfrontend**
- Zahlungsliste mit Filtern, Suche, Sortierung, Dokumentvorschau und Änderungsprotokoll
- Korrekturen (Betrag, Ausführungsdatum, Empfänger, Referenz), manuelle Erfassung ohne QR-Code, Zurückstellen
- Ratenzahlung mit eigenem Betrag und Datum pro Rate, Tag «Ratenzahlung» in paperless
- Duplikatprüfung gegen offene Rechnungen und die Exporthistorie
- mehrere Zahlungskonten mit automatischer Zuordnung (Währung, paperless-Tag, Korrespondent, Speicherpfad), eine Datei pro Konto
- Auswertungen: Kennzahlen, Liquiditätsvorschau, offene Beträge und Zahlungshistorie, Fehler und Duplikate
- Exporthistorie mit erneutem Download und «Rückgängig»
- Anmeldung mit dem paperless-Benutzer, Zugriff nach paperless-Rechten, Kontenverwaltung nur für Superuser
- Statuszeile mit Programmversion, Healthcheck `/api/health`

**Betrieb**
- Docker-Image auf ghcr.io (amd64/arm64), `docker-compose.yml`, systemd- und nginx-Beispiele
- automatische Datenbank-Migration, Tests und Image-Build über GitHub Actions
