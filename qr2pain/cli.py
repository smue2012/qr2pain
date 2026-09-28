"""qr2pain – QR-Rechnungen aus paperless-ngx als pain.001 exportieren."""
from __future__ import annotations

import argparse
import logging
import re
import sys
import tomllib
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import pain001
from .paperless import Paperless
from .qrscan import find_swiss_qr
from .swissqr import QRBillError, parse

log = logging.getLogger("qr2pain")


def next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def monetary_value(raw) -> Decimal | None:
    """paperless-Monetary-Feld: 'CHF123.45' oder '123.45'."""
    if raw in (None, ""):
        return None
    m = re.search(r"(\d+(?:\.\d{1,2})?)", str(raw))
    try:
        return Decimal(m.group(1)) if m else None
    except InvalidOperation:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qr2pain", description=__doc__)
    ap.add_argument("-c", "--config", default="config.toml", help="Konfigurationsdatei (TOML)")
    ap.add_argument("-o", "--out", help="Ausgabedatei (Standard: pain001_<Zeitstempel>.xml)")
    ap.add_argument("-d", "--date", help="Ausführungsdatum YYYY-MM-DD (Standard: nächster Werktag)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Nur anzeigen/erzeugen, Dokumente in paperless NICHT markieren")
    ap.add_argument("--limit", type=int, help="Maximale Anzahl Dokumente")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    pcfg, dcfg, tcfg = cfg["paperless"], cfg["debtor"], cfg.get("tags", {})

    debtor = pain001.Debtor(**dcfg)
    try:
        debtor.check()
    except ValueError as e:
        log.error("%s", e)
        return 2

    exec_date = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
                 else next_business_day(date.today()))

    pl = Paperless(pcfg["url"], pcfg["token"], pcfg.get("verify_tls", True))
    log.info("paperless-ngx verbunden (API-Version %s)", pl.api_version or "?")

    pending_tag = pl.tag_id(tcfg.get("pending", "QR zu zahlen"))
    exported_tag = pl.tag_id(tcfg.get("exported", "QR exportiert"), create=True)
    error_tag = pl.tag_id(tcfg["error"], create=True) if tcfg.get("error") else None
    remove_pending = tcfg.get("remove_pending", True)
    amount_field = pl.custom_field_id(cfg["amount_field"]) if cfg.get("amount_field") else None

    payments: list[pain001.Payment] = []
    docs_ok: list[dict] = []
    errors: list[tuple[dict, str]] = []
    seen: set[tuple] = set()

    # Dokumente mit Fehler-Tag werden übersprungen, bis der Tag entfernt wird
    exclude = [exported_tag] + ([error_tag] if error_tag else [])
    for n, doc in enumerate(pl.documents([pending_tag], exclude)):
        if args.limit and n >= args.limit:
            break
        label = f"#{doc['id']} «{doc['title']}»"
        try:
            data, mime = pl.download_original(doc["id"])
            payloads = find_swiss_qr(data, mime)
            if not payloads:
                raise QRBillError("kein Swiss QR Code gefunden")
            if len(payloads) > 1:
                raise QRBillError(f"{len(payloads)} QR-Codes gefunden – bitte manuell prüfen")
            bill = parse(payloads[0])

            amount = bill.amount
            if amount is None and amount_field:
                cf = next((c for c in doc.get("custom_fields", []) if c["field"] == amount_field), None)
                amount = monetary_value(cf and cf.get("value"))
            if amount is None:
                raise QRBillError("QR-Rechnung ohne Betrag (und kein Betrag im Zusatzfeld)")

            key = (bill.iban, bill.reference, amount)
            if key in seen:
                raise QRBillError("Duplikat (gleiche IBAN/Referenz/Betrag bereits im Lauf)")
            seen.add(key)

            e2e = f"PL{doc['id']}" + (f"-ASN{doc['archive_serial_number']}"
                                      if doc.get("archive_serial_number") else "")
            payments.append(pain001.Payment(bill, e2e, amount))
            docs_ok.append(doc)
            log.info("OK   %-40s %s %10s  %s  %s %s", label[:40], bill.currency, amount,
                     bill.creditor.name[:30], bill.ref_type, bill.reference)
        except Exception as e:  # noqa: BLE001 – pro Dokument weitermachen
            errors.append((doc, str(e)))
            log.warning("FEHLER %s: %s", label, e)

    if not payments:
        log.warning("Keine exportierbaren Rechnungen gefunden.")
        return 1 if errors else 0

    xml = pain001.build(debtor, payments, exec_date, cfg.get("initiating_party"))
    out = Path(args.out or f"pain001_{datetime.now():%Y%m%d_%H%M%S}.xml")
    out.write_bytes(xml)

    totals: dict[str, Decimal] = {}
    for p in payments:
        totals[p.bill.currency] = totals.get(p.bill.currency, Decimal(0)) + p.amount
    log.info("→ %s: %d Zahlungen, %s, Ausführung %s", out, len(payments),
             ", ".join(f"{c} {v:.2f}" for c, v in totals.items()), exec_date)

    if args.dry_run:
        log.info("Dry-Run: paperless wurde nicht verändert.")
    else:
        for doc in docs_ok:
            pl.update_tags(doc, add=[exported_tag], remove=[pending_tag] if remove_pending else [])
            pl.add_note(doc["id"], f"Zahlung exportiert in {out.name} (Ausführung {exec_date})")
        if error_tag:
            for doc, msg in errors:
                pl.update_tags(doc, add=[error_tag], remove=[])
                pl.add_note(doc["id"], f"qr2pain: {msg}")
        log.info("%d Dokumente als exportiert markiert.", len(docs_ok))

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
