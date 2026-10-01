"""Geschäftslogik des Webfrontends: Sync aus paperless, Korrekturen, Export, Auswertungen."""
from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from .. import pain001
from ..paperless import Paperless
from ..qrscan import find_swiss_qr
from ..swissqr import Address, QRBill, QRBillError, iban_valid, is_qr_iban, parse, validate
from .store import Store, now

CREDITOR_KEYS = ("name", "street", "building", "postal_code", "town", "country")
OVERRIDE_KEYS = {"amount", "execution_date", "iban", "reference", "message", "currency",
                 "dup_ok", "comment", "installments"} | {f"creditor_{k}" for k in CREDITOR_KEYS}
MAX_PARTS = 60


def parse_item(v: Any) -> tuple[int, int]:
    """Export-Position: 101 / "101" = ganze Rechnung, "101:2" = Rate 2."""
    doc, _, part = str(v).partition(":")
    return int(doc), int(part or 0)


# ============================================================ Hilfsfunktionen

def next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prev_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def to_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def to_amount(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", str(v).replace("'", "").replace("’", ""))
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def ref_type_for(reference: str) -> str:
    r = reference.replace(" ", "").upper()
    if not r:
        return "NON"
    if r.startswith("RF"):
        return "SCOR"
    return "QRR" if r.isdigit() else "?"


@lru_cache(maxsize=8192)
def _parse_cached(raw: str) -> QRBill:
    """QR-Payload nur einmal parsen (QRBill wird nie verändert, nur per replace() kopiert)."""
    return parse(raw, check=False)


def _empty_bill() -> QRBill:
    return QRBill(iban="", creditor=Address("S", ""), amount=None, currency="CHF",
                  debtor=None, ref_type="NON", reference="")


# ============================================================ effektive Zahlungsdaten

class Engine:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        w = cfg.get("web", {})
        self.lead_days = int(w.get("lead_days", 1))
        self.default_terms = int(w.get("default_terms_days", 30))
        self.sync_state: dict[str, Any] = {"running": False}
        self._sync_lock = threading.Lock()
        self._snap: tuple | None = None
        self._snap_lock = threading.Lock()
        self.download_workers = int(w.get("download_workers", 4))
        self.import_config_account()

    # ---------------------------------------------------------------- Ausführungsdatum
    def default_exec_date(self, due: date | None, today: date | None = None) -> date:
        earliest = next_business_day(today or date.today())
        if not due:
            return earliest
        wanted = prev_business_day(due - timedelta(days=self.lead_days))
        return max(earliest, wanted)

    def due_of(self, row: dict) -> tuple[date | None, bool]:
        """Fälligkeit aus paperless-Feld, sonst Dokumentdatum + Standard-Zahlungsfrist."""
        d = to_date(row.get("due_date"))
        if d:
            return d, False
        c = to_date(row.get("created"))
        return (c + timedelta(days=self.default_terms), True) if c else (None, True)

    # ---------------------------------------------------------------- Kern
    def paid_parts(self, doc_id: int | None = None) -> dict[int, dict[int, dict]]:
        """Bereits exportierte (nicht rückgängig gemachte) Positionen: doc_id -> part -> Info."""
        sql = ("SELECT i.doc_id, i.part, i.amount, i.exec_date, i.export_id FROM export_items i "
               "JOIN exports x ON x.id=i.export_id WHERE x.reverted_at IS NULL")
        rows = self.store.q(sql + " AND i.doc_id=?", doc_id) if doc_id else self.store.q(sql)
        out: dict[int, dict[int, dict]] = defaultdict(dict)
        for r in rows:
            out[r["doc_id"]][r["part"]] = r
        return out

    def effective(self, row: dict, dup_index: dict | None = None, paid: dict | None = None,
                  accounts: list[dict] | None = None) -> dict:
        ov = row["overrides"]
        if accounts is None:
            accounts = self.store.accounts(active_only=True)
        if paid is None:
            paid = self.paid_parts(row["doc_id"])
        paid_here = paid.get(row["doc_id"], {})
        errors: list[str] = []
        warnings: list[str] = []

        original = None
        bill = _empty_bill()
        if row.get("qr_raw"):
            try:
                bill = _parse_cached(row["qr_raw"])
                original = bill_to_dict(bill)
            except QRBillError as e:
                errors.append(f"QR-Code unlesbar: {e}")
        elif row.get("scan_error"):
            if not ov.get("iban"):
                errors.append(f"{row['scan_error']} – Zahlungsdaten manuell erfassen")

        extra_bills = []          # weitere QR-Codes im selben Dokument (z. B. Ratenscheine)
        for raw in row.get("qr_extra") or []:
            try:
                extra_bills.append(_parse_cached(raw))
            except QRBillError:
                pass

        # --- Korrekturen anwenden
        if ov.get("iban"):
            bill = replace(bill, iban=ov["iban"].replace(" ", "").upper())
        if "reference" in ov:
            ref = (ov["reference"] or "").replace(" ", "").upper()
            bill = replace(bill, reference=ref, ref_type=ref_type_for(ref))
        if "message" in ov:
            bill = replace(bill, message=ov["message"] or "")
        if ov.get("currency"):
            bill = replace(bill, currency=ov["currency"])
        cred_ov = {k: ov[f"creditor_{k}"] for k in CREDITOR_KEYS if f"creditor_{k}" in ov}

        def fix_creditor(b: QRBill) -> QRBill:
            if not cred_ov:
                return b
            c = b.creditor
            if c.adr_type == "K":  # kombinierte Adresse bei Korrektur in strukturierte überführen
                m = re.match(r"^\s*(\d{4,5})\s+(.+)$", c.building_or_line2)
                c = Address("S", c.name, c.street_or_line1, "",
                            m.group(1) if m else "", m.group(2) if m else c.building_or_line2, c.country)
            mapping = {"name": "name", "street": "street_or_line1", "building": "building_or_line2",
                       "postal_code": "postal_code", "town": "town", "country": "country"}
            c = replace(c, adr_type="S", **{mapping[k]: (v or "").strip() for k, v in cred_ov.items()})
            if c.country:
                c = replace(c, country=c.country.upper())
            return replace(b, creditor=c)

        bill = fix_creditor(bill)
        # Korrekturen am Empfänger und an der Währung gelten für alle Einzahlungsscheine des Dokuments
        qr_bills = [bill] + [fix_creditor(replace(b, currency=ov["currency"]) if ov.get("currency") else b)
                             for b in extra_bills] if row.get("qr_raw") else [bill]
        multi = len(qr_bills) > 1

        # --- Betrag
        amount = to_amount(ov.get("amount")) if ov.get("amount") not in (None, "") else None
        amount_src = "korrigiert"
        if amount is None and multi and all(b.amount is not None for b in qr_bills):
            amount = sum((b.amount for b in qr_bills), Decimal(0)).quantize(Decimal("0.01"))
            amount_src = f"{len(qr_bills)} QR-Codes"
        if amount is None and bill.amount is not None:
            amount, amount_src = bill.amount.quantize(Decimal("0.01")), "QR-Code"
        if amount is None and row.get("pl_amount"):
            amount, amount_src = to_amount(row["pl_amount"]), "paperless-Feld"
        if amount is None:
            errors.append("Kein Betrag – bitte erfassen")
            amount_src = ""
        if not multi and bill.amount is not None and amount is not None and amount != bill.amount:
            diff = bill.amount - amount
            warnings.append(f"Betrag weicht vom QR-Code ab ({bill.currency} {bill.amount} → {amount}, "
                            f"Differenz {diff:+.2f})")

        # --- Validierung (mit effektivem Betrag)
        try:
            validate(replace(bill, amount=amount))
        except QRBillError as e:
            if not (row.get("scan_error") and not ov.get("iban")):  # Fehler bereits gemeldet
                errors.append(str(e))
        c = bill.creditor
        if bill.iban and not (c.town or (c.adr_type == "K" and c.building_or_line2)):
            errors.append("Ort des Zahlungsempfängers fehlt")

        # --- Datum
        due, due_estimated = self.due_of(row)
        today = date.today()
        if ov.get("execution_date"):
            exec_date = to_date(ov["execution_date"])
            if exec_date and exec_date < today:
                errors.append("Ausführungsdatum liegt in der Vergangenheit")
            elif exec_date and exec_date.weekday() >= 5:
                warnings.append("Ausführungsdatum ist ein Wochenende – Bank führt am nächsten Bankwerktag aus")
        else:
            exec_date = self.default_exec_date(due, today)
        # --- Aufteilung in Raten
        plan = ov.get("installments") or []
        plan_auto = False
        if not plan and multi:
            plan, plan_auto = self.qr_plan(qr_bills, due, today), True
        qr_based = any("qr" in p for p in plan)
        parts: list[dict] = []
        open_amount = amount
        if plan:
            n_total = len(plan)
            total = Decimal(0)
            open_amount = Decimal(0)
            for n, p in enumerate(plan, start=1):
                pa, pd = to_amount(p.get("amount")), to_date(p.get("date"))
                done = paid_here.get(n)
                perr = []
                pb = None
                if "qr" in p:
                    qi = p["qr"]
                    pb = qr_bills[qi] if isinstance(qi, int) and 0 <= qi < len(qr_bills) else None
                    if pb is None and not done:
                        perr.append("Einzahlungsschein nicht mehr im Dokument")
                if not done:
                    if pa is None or pa <= 0:
                        perr.append("Betrag fehlt")
                    if not pd:
                        perr.append("Datum fehlt")
                    elif pd < today:
                        perr.append("Datum liegt in der Vergangenheit" + (
                            " – bereits bezahlt? Dann die Rate in der Aufteilung entfernen" if pb else ""))
                    if pb and pa:
                        try:
                            validate(replace(pb, amount=pa))
                        except QRBillError as ex:
                            perr.append(str(ex))
                    open_amount += pa or Decimal(0)
                total += pa or Decimal(0)
                parts.append({"no": n, "of": n_total, "amount": str(pa) if pa is not None else None,
                              "date": pd.isoformat() if pd else None, "errors": perr,
                              "exported": bool(done), "export_id": done["export_id"] if done else None,
                              "qr": p.get("qr"), "reference": pb.reference if pb else None,
                              "message": pb.message if pb else None,
                              "estimated": bool(p.get("estimated")) and not done,
                              "_amount": pa, "_date": pd, "_bill": pb})
            if qr_based and "amount" not in ov:
                # jeder Einzahlungsschein ist eine eigene Zahlung: Gesamtbetrag = Summe der Raten
                amount, amount_src = total.quantize(Decimal("0.01")), f"{len(qr_bills)} QR-Codes"
            elif amount is not None and total != amount:
                errors.append(f"Raten ergeben {bill.currency} {total:.2f}, zu zahlen sind {amount:.2f} "
                              f"(Differenz {amount - total:+.2f})")
            open_parts = [p for p in parts if not p["exported"]]
            if open_parts and open_parts[0]["_date"]:
                exec_date = min(p["_date"] for p in open_parts if p["_date"])
            if plan_auto and row["status"] == "open":
                if any(p["estimated"] for p in parts):
                    warnings.append("Datum einzelner Raten geschätzt – bitte in der Aufteilung prüfen")
        elif due and exec_date and exec_date > due and row["status"] == "open":
            warnings.append(f"Zahlung erfolgt nach Fälligkeit ({due:%d.%m.%Y})")

        account = resolve_account(row, bill.currency, accounts)
        if not account and row["status"] == "open":
            warnings.append("Kein Belastungskonto zugeordnet – beim Export wählen")

        eff = bill_to_dict(bill)
        eff.update(amount=str(amount) if amount is not None else None,
                   execution_date=exec_date.isoformat() if exec_date else None)
        base_ok = row["status"] == "open" and not row["held"] and not errors
        for p in parts:
            p["exportable"] = base_ok and not p["exported"] and not p["errors"]
        exportable = any(p["exportable"] for p in parts) if parts else base_ok
        result = {
            "doc_id": row["doc_id"], "title": row["title"], "correspondent": row["correspondent"],
            "created": row["created"], "asn": row["asn"],
            "due_date": due.isoformat() if due else None, "due_estimated": due_estimated,
            "status": row["status"], "held": bool(row["held"]), "export_id": row["export_id"],
            "scan_error": row["scan_error"], "amount_source": amount_src,
            "original": original, "effective": eff,
            "overrides": ov,
            "overridden": sorted(k for k in ov if k not in ("comment", "dup_ok", "installments")),
            "errors": errors, "warnings": warnings, "duplicate": False,
            "exportable": exportable,
            "account": account, "tags": row.get("tags") or [], "storage_path": row.get("storage_path"),
            "split": bool(parts), "parts": [{k: v for k, v in p.items() if not k.startswith("_")} for p in parts],
            "qr_count": len(qr_bills) if row.get("qr_raw") else 0, "plan_auto": plan_auto,
            "_partkeys": [(p, dup_key(p["_bill"].iban, p["_bill"].reference, p["_amount"], p["_bill"].message))
                          for p in parts if p["_bill"] and not p["exported"] and p["_amount"] is not None],
            "parts_done": sum(p["exported"] for p in parts),
            "open_amount": str(open_amount) if open_amount is not None else None,
            "_bill": bill, "_amount": amount, "_exec": exec_date, "_parts": parts, "_open_amount": open_amount,
            "_held": bool(row["held"]),
            "_dupkey": dup_key(bill.iban, bill.reference, amount, bill.message)
            if amount is not None and bill.iban else None,
        }
        if dup_index is not None:
            apply_duplicates(result, dup_index)
        return result

    def qr_plan(self, bills: list[QRBill], due: date | None, today: date) -> list[dict]:
        """Raten aus mehreren Einzahlungsscheinen: Betrag aus dem QR-Code, Datum aus dem Schein
        (Swico-Zahlungsbedingungen oder Datum in der Mitteilung), sonst ab Fälligkeit monatlich geschätzt."""
        plan, prev = [], None
        for i, b in enumerate(bills):
            found = due_from_bill(b)
            if found:
                d, est = found, False
            elif prev:
                d, est = add_months(prev, 1), True
            else:
                d, est = (due or today), True
            prev = d
            ex = d if d < today else self.default_exec_date(d, today)
            plan.append({"amount": str(b.amount) if b.amount is not None else None, "date": ex.isoformat(),
                         "qr": i, "estimated": est})
        return plan

    def dup_index(self, paid: dict | None = None, effs: list[dict] | None = None) -> dict:
        paid = self.paid_parts() if paid is None else paid
        if effs is None:
            accounts = self.store.accounts(active_only=True)
            effs = [self.effective(r, paid=paid, accounts=accounts) for r in self.store.invoices("status='open'")]
        idx: dict[tuple, list[dict]] = defaultdict(list)
        for e in effs:
            if e["status"] == "open" and e["_dupkey"] and not e["plan_auto"]:
                idx[e["_dupkey"]].append({"kind": "open", "doc_id": e["doc_id"]})
            if e["status"] == "open":
                for _p, k in e["_partkeys"]:   # einzelne Einzahlungsscheine
                    idx[k].append({"kind": "open", "doc_id": e["doc_id"]})
        per_doc: dict[int, list[dict]] = defaultdict(list)
        for it in self.store.q(
                "SELECT i.* FROM export_items i JOIN exports x ON x.id=i.export_id "
                "WHERE x.reverted_at IS NULL"):
            a = to_amount(it["amount"])
            idx[paid_key(it["iban"], it["reference"] or "", a)].append(
                {"kind": "paid", "doc_id": it["doc_id"], "export_id": it["export_id"],
                 "exec_date": it["exec_date"]})
            per_doc[it["doc_id"]].append(it)
        # in Raten bezahlte Rechnungen zusätzlich mit ihrer Gesamtsumme erfassen
        for doc_id, its in per_doc.items():
            if len(its) > 1:
                s = sum((to_amount(i["amount"]) or Decimal(0) for i in its), Decimal(0))
                last = max(its, key=lambda i: i["exec_date"])
                idx[paid_key(last["iban"], last["reference"] or "", s)].append(
                    {"kind": "paid", "doc_id": doc_id, "export_id": last["export_id"],
                     "exec_date": last["exec_date"]})
        return idx

    def snapshot(self) -> dict[int, dict]:
        """Alle Rechnungen fertig berechnet. Wird nur neu berechnet, wenn sich Daten geändert haben
        (Änderungszähler der Datenbank) oder ein neuer Tag begonnen hat (Datumsprüfungen)."""
        key = (self.store.gen, date.today())
        snap = self._snap
        if snap and snap[0] == key:
            return snap[1]
        with self._snap_lock:
            if self._snap and self._snap[0] == key:
                return self._snap[1]
            paid = self.paid_parts()
            accounts = self.store.accounts(active_only=True)
            effs = {r["doc_id"]: self.effective(r, None, paid, accounts) for r in self.store.invoices()}
            idx = self.dup_index(paid, list(effs.values()))
            for e in effs.values():
                apply_duplicates(e, idx)
            self._snap = (key, effs)
            return effs

    def list(self, status: str | None = "open", visible: set[int] | None = None) -> list[dict]:
        return [e for e in self.snapshot().values()
                if (status is None or e["status"] == status) and (visible is None or e["doc_id"] in visible)]

    def get(self, doc_id: int) -> dict | None:
        e = self.snapshot().get(doc_id)
        return dict(e) if e else None

    # ---------------------------------------------------------------- Raten
    def _check_installments(self, value: Any, old: list, done: dict) -> list[dict]:
        if not isinstance(value, list) or not 2 <= len(value) <= MAX_PARTS:
            raise ValueError(f"Aufteilung braucht 2 bis {MAX_PARTS} Raten")
        clean = []
        for n, p in enumerate(value, start=1):
            if not isinstance(p, dict):
                raise ValueError(f"Rate {n}: ungültig")
            a, d = to_amount(p.get("amount")), to_date(p.get("date"))
            if a is None or a <= 0:
                raise ValueError(f"Rate {n}: Betrag ungültig")
            if not d:
                raise ValueError(f"Rate {n}: Datum ungültig")
            item = {"amount": str(a), "date": d.isoformat()}
            if p.get("qr") is not None:
                if not isinstance(p["qr"], int) or not 0 <= p["qr"] < 100:
                    raise ValueError(f"Rate {n}: Einzahlungsschein ungültig")
                item["qr"] = p["qr"]
            clean.append(item)
        for n in done:  # bereits exportierte Raten dürfen sich nicht ändern
            o = {k: v for k, v in old[n - 1].items() if k != "estimated"} if n <= len(old) else None
            if n > len(clean) or o is None or clean[n - 1] != o:
                raise ValueError(f"Rate {n} ist bereits exportiert und kann nicht geändert werden")
        return clean

    # ---------------------------------------------------------------- Korrekturen
    def update_overrides(self, user: str, doc_id: int, changes: dict, pl: Paperless | None = None) -> dict:
        row = self.store.invoice(doc_id)
        if not row:
            raise KeyError(doc_id)
        if row["status"] != "open":
            raise ValueError("Bereits exportierte Rechnungen können nicht geändert werden")
        unknown = set(changes) - OVERRIDE_KEYS
        if unknown:
            raise ValueError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
        if changes.get("amount") not in (None, "") and to_amount(changes["amount"]) is None:
            raise ValueError("Betrag ungültig")
        if changes.get("execution_date") and not to_date(changes["execution_date"]):
            raise ValueError("Datum ungültig")
        if changes.get("currency") not in (None, "", "CHF", "EUR"):
            raise ValueError("Nur CHF oder EUR möglich")
        ov = dict(row["overrides"])
        done = self.paid_parts(doc_id).get(doc_id, {})
        if done:
            locked = set(changes) - {"installments", "comment", "dup_ok"}
            if locked:
                raise ValueError("Es wurden bereits Raten exportiert – nur noch die offenen Raten sind änderbar")
            if "installments" in changes and not changes["installments"]:
                raise ValueError("Aufteilung kann nicht entfernt werden, es wurden bereits Raten exportiert")
        if changes.get("installments"):
            old_plan = ov.get("installments") or []
            if not old_plan and done:   # automatisch aus den Einzahlungsscheinen gebildete Raten
                cur = self.effective(row)
                old_plan = [{"amount": p["amount"], "date": p["date"], **({"qr": p["qr"]} if p["qr"] is not None else {})}
                            for p in cur["parts"]] if cur["plan_auto"] else []
            changes["installments"] = self._check_installments(changes["installments"], old_plan, done)
            changes["execution_date"] = None  # Datum gilt pro Rate
        log = []
        for k, v in changes.items():
            old = ov.get(k)
            if k == "installments":
                if not v and k in ov:
                    del ov[k]
                    log.append("Aufteilung entfernt")
                elif v and v != old:
                    ov[k] = v
                    log.append(f"Aufteilung in {len(v)} Raten: " + ", ".join(
                        f"{p['amount']} am {to_date(p['date']):%d.%m.%Y}" for p in v))
                continue
            if v is None or v == "" and k not in ("reference", "message"):
                if k in ov:
                    del ov[k]
                    log.append(f"{k}: «{old}» → (Original)")
            elif old != v:
                ov[k] = v
                log.append(f"{k}: «{old if old is not None else 'Original'}» → «{v}»")
        warnings = []
        if log:
            self.store.set_overrides(doc_id, ov)
            self.store.audit(user, doc_id, "Korrektur", "; ".join(log))
            had, has = bool(row["overrides"].get("installments")), bool(ov.get("installments"))
            if pl and had != has:
                warnings = self.tag_installments(pl, doc_id, has)
        out = self.get(doc_id)
        out["tag_warnings"] = warnings
        return out

    def set_held(self, user: str, ids: list[int], held: bool) -> None:
        for i in ids:
            r = self.store.invoice(i)
            if r and r["status"] == "open" and bool(r["held"]) != held:
                self.store.x("UPDATE invoices SET held=? WHERE doc_id=?", int(held), i)
                self.store.audit(user, i, "Zurückgestellt" if held else "Freigegeben")

    # ---------------------------------------------------------------- Sync
    def start_sync(self, pl: Paperless, user: str) -> bool:
        if not self._sync_lock.acquire(blocking=False):
            return False
        self.sync_state = {"running": True, "user": user, "started_at": now(),
                           "done": 0, "total": 0, "new": 0, "removed": 0, "errors": []}
        threading.Thread(target=self._sync, args=(pl, user), daemon=True).start()
        return True

    def _sync(self, pl: Paperless, user: str) -> None:
        st = self.sync_state
        try:
            tcfg = self.cfg.get("tags", {})
            pending = pl.tag_id(tcfg.get("pending", "QR zu zahlen"))
            exported = pl.tag_id(tcfg.get("exported", "QR exportiert"), create=True)
            w = self.cfg.get("web", {})
            due_f = pl.custom_field_id(w["due_field"]) if w.get("due_field") else None
            amt_f = pl.custom_field_id(self.cfg["amount_field"]) if self.cfg.get("amount_field") else None
            corr = pl.names("correspondents")
            tag_names = pl.names("tags")
            spaths = pl.names("storage_paths")

            docs = list(pl.documents([pending], [exported]))
            st["total"] = len(docs)
            # Ist das Betragsfeld zugleich das Rückschreibefeld «Zahlbetrag», steht darin nach dem ersten Export
            # der bezahlte bzw. offene Betrag – dann den ursprünglich erfassten Betrag nicht mehr überschreiben.
            frozen = set(self.paid_parts()) if self.amount_field_shared() else set()
            seen = set()
            todo = []
            for doc in docs:
                seen.add(doc["id"])
                cf = {c["field"]: c.get("value") for c in doc.get("custom_fields", [])}
                meta = dict(title=doc.get("title"), correspondent=corr.get(doc.get("correspondent")),
                            created=str(doc.get("created") or doc.get("created_date") or "")[:10],
                            asn=doc.get("archive_serial_number"),
                            due_date=cf.get(due_f) if due_f else None,
                            pl_amount=str(cf.get(amt_f)) if amt_f and cf.get(amt_f) else None,
                            tags=json.dumps(sorted(tag_names.get(t, str(t)) for t in doc.get("tags", []))),
                            storage_path=spaths.get(doc.get("storage_path")),
                            synced_at=now())
                if doc["id"] in frozen:
                    meta.pop("pl_amount")
                row = self.store.invoice(doc["id"])
                if row and row["status"] == "exported":
                    st["done"] += 1
                elif row and row["modified"] == doc.get("modified") and (row["qr_raw"] or row["scan_error"]) \
                        and row.get("scan_v", 1) >= SCAN_VERSION:
                    self.store.upsert_invoice(doc["id"], **meta)   # unverändert: nur Metadaten
                    st["done"] += 1
                else:
                    todo.append((doc, meta, row))

            # Neue/geänderte Dokumente: parallel herunterladen, QR-Codes nacheinander lesen
            with ThreadPoolExecutor(max_workers=self.download_workers) as pool:
                futures = {pool.submit(pl.download_original, doc["id"]): (doc, meta, row)
                           for doc, meta, row in todo}
                for fut in as_completed(futures):
                    doc, meta, row = futures[fut]
                    try:
                        data, mime = fut.result()
                        payloads = find_swiss_qr(data, mime)
                        good, bad = [], []
                        for pl_ in payloads:
                            try:
                                parse(pl_, check=False)
                                good.append(pl_)
                            except QRBillError as e:
                                bad.append(str(e))
                        if good:
                            qr, extra, err = good[0], good[1:], None
                            if bad:
                                st["errors"].append(f"#{doc['id']}: {len(bad)} von {len(payloads)} QR-Codes unlesbar")
                        elif bad:
                            qr, extra, err = None, [], f"QR-Code unlesbar: {bad[0]}"
                        else:
                            qr, extra, err = None, [], "Kein Swiss QR Code gefunden"
                    except Exception as e:  # noqa: BLE001
                        qr, extra, err = None, [], f"Download/Scan fehlgeschlagen: {e}"
                    multi_before = bool(row and row.get("qr_extra"))
                    self.store.upsert_invoice(doc["id"], modified=doc.get("modified"), qr_raw=qr,
                                              qr_extra=json.dumps(extra), scan_v=SCAN_VERSION, scan_error=err, **meta)
                    if extra and not multi_before:
                        if row:
                            self.store.audit(user, doc["id"], "QR-Codes", f"{len(extra) + 1} QR-Codes erkannt – als Raten geführt")
                        st["errors"] += [f"#{doc['id']}: {w}" for w in self.tag_installments(pl, doc["id"], True)]
                    if not row:
                        st["new"] += 1
                        self.store.audit(user, doc["id"], "Importiert",
                                         err or (f"{len(extra) + 1} QR-Codes gelesen" if extra else "QR-Code gelesen"))
                    st["done"] += 1

            # Offene Rechnungen, die dieser Benutzer nicht mehr in der Liste sieht:
            # nur entfernen, wenn das Tag wirklich weg ist – fehlende Rechte sind kein Grund.
            superuser = pl.is_superuser()
            for r in self.store.invoices("status='open'"):
                if r["doc_id"] in seen:
                    continue
                reason = None
                try:
                    d = pl.get_document(r["doc_id"])
                    if exported in d["tags"]:
                        reason = "In paperless bereits als exportiert markiert"
                    elif pending not in d["tags"]:
                        reason = "Tag «zu zahlen» in paperless entfernt"
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404:
                        if superuser:
                            reason = "Dokument in paperless gelöscht"
                        else:
                            st["hidden"] = st.get("hidden", 0) + 1   # kein Zugriff: stehen lassen
                    else:
                        raise
                if reason and not self.paid_parts(r["doc_id"]).get(r["doc_id"]):
                    self.store.x("DELETE FROM invoices WHERE doc_id=?", r["doc_id"])
                    self.store.audit(user, r["doc_id"], "Entfernt", reason)
                    st["removed"] += 1
            st["message"] = (f"{st['total']} Dokumente geprüft, {st['new']} neu, {st['removed']} entfernt"
                             + (f", {st['hidden']} ohne Zugriff übersprungen" if st.get("hidden") else ""))
        except Exception as e:  # noqa: BLE001
            st["message"] = f"Sync fehlgeschlagen: {e}"
            st["failed"] = True
        finally:
            st["running"] = False
            st["finished_at"] = now()
            self._sync_lock.release()

    # ---------------------------------------------------------------- Export
    def export(self, pl: Paperless, user: str, items: list, account_map: dict | None = None,
               booking: dict | None = None) -> dict:
        """items: Dokument-IDs (ganze Rechnung) bzw. "doc:rate" für einzelne Raten.

        account_map: Übersteuerung pro Gruppe {"<zugeordnete Konto-ID>" | "none": Ziel-Konto-ID}.
        booking: Verbuchungsart pro Ziel-Konto {"<Konto-ID>": "batch"|"single"|"bank"}, sonst Vorgabe des Kontos.
        Pro Belastungskonto entsteht eine eigene pain.001-Datei."""
        account_map = {str(k): v for k, v in (account_map or {}).items() if v not in (None, "")}
        booking = {str(k): v for k, v in (booking or {}).items() if v}
        bad = [v for v in booking.values() if v not in pain001.BOOKING_MODES]
        if bad:
            raise ValueError([f"Verbuchungsart «{bad[0]}» unbekannt"])
        paid = self.paid_parts()
        idx = self.dup_index(paid)
        wanted: dict[int, set[int]] = defaultdict(set)
        for it in items:
            doc, part = parse_item(it)
            wanted[doc].add(part)

        problems, units = [], []   # unit = (eff, part-dict | None)
        for doc, parts in wanted.items():
            r = self.store.invoice(doc)
            if not r:
                problems.append(f"#{doc}: nicht gefunden")
                continue
            e = self.effective(r, idx, paid)
            label = f"#{doc} {e['effective']['creditor']['name'] or e['title']}"
            if e["split"]:
                if 0 in parts:  # ganze Rechnung gewählt -> alle offenen Raten
                    parts = {p["no"] for p in e["_parts"] if not p["exported"]}
                for n in sorted(parts):
                    p = next((x for x in e["_parts"] if x["no"] == n), None)
                    if not p:
                        problems.append(f"{label}: Rate {n} existiert nicht")
                    elif p["exported"]:
                        problems.append(f"{label}: Rate {n} ist bereits exportiert (Export #{p['export_id']})")
                    elif not p["exportable"]:
                        why = p["errors"] or e["errors"] or (["zurückgestellt"] if e["held"] else [])
                        problems.append(f"{label}, Rate {n}: {'; '.join(why)}")
                    else:
                        units.append((e, p))
            else:
                if parts != {0}:
                    problems.append(f"{label}: Rechnung ist nicht aufgeteilt")
                elif not e["exportable"]:
                    why = e["errors"] or (["zurückgestellt"] if e["held"] else [f"Status {e['status']}"])
                    problems.append(f"{label}: {'; '.join(why)}")
                else:
                    units.append((e, None))

        # gleiche Zahlung aus verschiedenen Dokumenten in derselben Auswahl
        keys = defaultdict(set)
        for e, p in units:
            b = (p or {}).get("_bill")
            if b is not None and p["_amount"] is not None:      # eigener Einzahlungsschein
                keys[dup_key(b.iban, b.reference, p["_amount"], b.message)].add(e["doc_id"])
            elif e["_amount"] is not None and not e["plan_auto"]:
                b = e["_bill"]
                keys[dup_key(b.iban, b.reference, e["_amount"], b.message)].add(e["doc_id"])
        for key, docs in keys.items():
            if len(docs) > 1 and not all(self.store.invoice(d)["overrides"].get("dup_ok") for d in docs):
                problems.append("Gleiche Zahlung mehrfach ausgewählt: " + ", ".join(f"#{d}" for d in sorted(docs)))
        if problems:
            raise ValueError(problems)
        if not units:
            raise ValueError(["Keine Rechnungen ausgewählt"])

        # Belastungskonto pro Zahlung: automatische Zuordnung, ggf. übersteuert
        accounts = {a["id"]: a for a in self.store.accounts(active_only=True)}
        groups: dict[int, list] = defaultdict(list)
        for e, p in units:
            auto = e["account"]["id"] if e["account"] else None
            target = account_map.get(str(auto) if auto else "none", auto)
            try:
                target = int(target) if target is not None else None
            except (TypeError, ValueError):
                target = None
            acc = accounts.get(target)
            label = f"#{e['doc_id']} {e['effective']['creditor']['name'] or e['title']}"
            if not acc:
                problems.append(f"{label}: kein Belastungskonto gewählt")
            elif acc["currency"] and acc["currency"] != e["effective"]["currency"]:
                problems.append(f"{label}: Konto «{acc['label']}» ist nur für {acc['currency']}")
            else:
                groups[acc["id"]].append((e, p))
        if problems:
            raise ValueError(problems)

        runs = []   # (konto, units, payments, xml, msg_id, verbuchung)
        for acc_id, grp in groups.items():
            acc = accounts[acc_id]
            mode = booking.get(str(acc_id)) or acc.get("booking") or "batch"
            payments = []
            for e, p in grp:
                e2e = f"PL{e['doc_id']}" + (f"-ASN{e['asn']}" if e["asn"] else "")
                if p and p.get("_bill") is not None:
                    # eigener Einzahlungsschein: Referenz und Mitteilung unverändert übernehmen
                    payments.append(pain001.Payment(p["_bill"], f"{e2e}-T{p['no']}", p["_amount"], p["_date"]))
                elif p:
                    b = e["_bill"]
                    msg = f"Teilzahlung {p['no']}/{p['of']}" + (f" {b.message}" if b.message else "")
                    payments.append(pain001.Payment(replace(b, message=msg[:140]), f"{e2e}-T{p['no']}",
                                                    p["_amount"], p["_date"]))
                else:
                    payments.append(pain001.Payment(e["_bill"], e2e, e["_amount"], e["_exec"]))
            xml = pain001.build(account_debtor(acc), payments, next_business_day(date.today()),
                                self.cfg.get("initiating_party") or acc["name"], booking=mode)
            msg_id = re.search(rb"<MsgId>([^<]+)</MsgId>", xml).group(1).decode()
            runs.append((acc, grp, payments, xml, msg_id, mode))

        created, doc_export = [], {}
        complete, partial = [], []
        with self.store.tx() as s:
            for acc, grp, payments, xml, msg_id, mode in runs:
                snap = json.dumps({**{k: acc[k] for k in ("label", "name", "iban", "bic", "currency")}, "booking": mode})
                cur = s.db.execute("INSERT INTO exports (msg_id, created_at, created_by, filename, xml, account_id, debtor) "
                                   "VALUES (?,?,?,?,?,?,?)", (msg_id, now(), user, "", xml, acc["id"], snap))
                export_id = cur.lastrowid
                filename = f"pain001_{datetime.now():%Y%m%d_%H%M}_E{export_id}_{slug(acc['label'])}.xml"
                s.db.execute("UPDATE exports SET filename=? WHERE id=?", (filename, export_id))
                for (e, p), pay in zip(grp, payments):
                    b = pay.bill
                    s.db.execute("INSERT INTO export_items (export_id, doc_id, creditor, iban, reference, currency, "
                                 "amount, exec_date, part) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (export_id, e["doc_id"], b.creditor.name, b.iban, b.reference, b.currency,
                                  str(pay.amount), pay.execution_date.isoformat(), p["no"] if p else 0))
                    what = f"Rate {p['no']}/{p['of']}, " if p else ""
                    s.db.execute("INSERT INTO audit (ts,user,doc_id,action,detail) VALUES (?,?,?,?,?)",
                                 (now(), user, e["doc_id"], "Exportiert",
                                  f"Export #{export_id} ({acc['label']}, {BOOKING_LABEL[mode]}), {what}{b.currency} {pay.amount}, "
                                  f"Ausführung {pay.execution_date}"))
                    doc_export[e["doc_id"]] = (export_id, filename)
                created.append({"id": export_id, "filename": filename, "count": len(grp), "booking": mode,
                                "account": {"id": acc["id"], "label": acc["label"], "iban": acc["iban"]}})
            for doc in dict.fromkeys(e["doc_id"] for e, _ in units):
                e = next(e for e, _ in units if e["doc_id"] == doc)
                if e["split"]:
                    n_done = s.db.execute(
                        "SELECT COUNT(DISTINCT i.part) FROM export_items i JOIN exports x ON x.id=i.export_id "
                        "WHERE x.reverted_at IS NULL AND i.doc_id=? AND i.part>0", (doc,)).fetchone()[0]
                    if n_done < len(e["_parts"]):
                        partial.append((doc, n_done, len(e["_parts"])))
                        continue
                s.db.execute("UPDATE invoices SET status='exported', export_id=? WHERE doc_id=?",
                             (doc_export[doc][0], doc))
                complete.append(doc)

        warnings = self.write_payment_fields(pl, list(dict.fromkeys(e["doc_id"] for e, _ in units)))
        for doc in complete:
            ex_id, fn = doc_export[doc]
            warnings += self._tag(pl, [doc], exported=True, note=f"Zahlung exportiert in {fn} (Export #{ex_id}) durch {user}")
        for doc, n_done, n in partial:
            ex_id, fn = doc_export[doc]
            warnings += self._note(pl, doc, f"Teilzahlung exportiert in {fn} (Export #{ex_id}), "
                                            f"{n_done} von {n} Raten erledigt – durch {user}")
        return {"exports": created, "count": len(units), "warnings": warnings,
                # Kompatibilität mit 1.3: erster Export
                "id": created[0]["id"], "filename": created[0]["filename"]}

    # ---------------------------------------------------------------- Kontostände
    def balances(self, account: str | None) -> dict:
        """Erfasste Kontostände pro Währung für die Auswahl (ein Konto oder alle aktiven zusammen)."""
        accs = self.store.accounts(active_only=True)
        if account not in (None, "", "all", "none"):
            accs = [a for a in accs if str(a["id"]) == str(account)]
        elif account == "none":
            return {}
        rows = {(r["account_id"], r["currency"]): r for r in self.store.q("SELECT * FROM balances")}
        out: dict[str, dict] = {}
        for ccy in ("CHF", "EUR"):
            cands = [a for a in accs if not a["currency"] or a["currency"] == ccy]
            got = [rows[(a["id"], ccy)] for a in cands if (a["id"], ccy) in rows]
            if not got:
                continue
            out[ccy] = {"amount": str(sum((Decimal(r["amount"]) for r in got), Decimal(0))),
                        "as_of": min(r["as_of"] for r in got), "by": got[-1]["updated_by"],
                        "entered": len(got), "accounts": len(cands),
                        "items": {str(r["account_id"]): r["amount"] for r in got}}
        return out

    def set_balance(self, user: str, account_id: int, currency: str, amount: Any) -> None:
        acc = self.store.one("SELECT * FROM accounts WHERE id=? AND active=1", account_id)
        if not acc:
            raise KeyError(account_id)
        if currency not in ("CHF", "EUR") or (acc["currency"] and acc["currency"] != currency):
            raise ValueError("Währung passt nicht zum Konto")
        if amount in (None, ""):
            self.store.x("DELETE FROM balances WHERE account_id=? AND currency=?", account_id, currency)
            self.store.audit(user, None, "Kontostand gelöscht", f"{acc['label']} {currency}")
            return
        txt = str(amount).strip().replace("'", "").replace("’", "").replace(" ", "").replace(",", ".")
        try:
            val = Decimal(txt).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise ValueError("Kontostand ungültig") from None
        if not val.is_finite() or abs(val) >= Decimal("1e12"):
            raise ValueError("Kontostand ungültig")
        self.store.x("INSERT INTO balances (account_id, currency, amount, as_of, updated_by) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(account_id, currency) DO UPDATE SET amount=excluded.amount, as_of=excluded.as_of, "
                     "updated_by=excluded.updated_by", account_id, currency, str(val), date.today().isoformat(), user)
        self.store.audit(user, None, "Kontostand erfasst", f"{acc['label']}: {currency} {val}")

    # ---------------------------------------------------------------- Konten
    ACCOUNT_FIELDS = ("label", "name", "iban", "bic", "street", "building", "postal_code", "town", "country",
                      "currency", "rules", "is_default", "sort", "active", "booking")

    def import_config_account(self) -> None:
        """Beim ersten Start mit Kontenverwaltung: Konto aus der config.toml übernehmen."""
        if self.store.one("SELECT id FROM accounts LIMIT 1") or not self.cfg.get("debtor"):
            return
        d = self.cfg["debtor"]
        self.store.x("INSERT INTO accounts (label,name,iban,bic,street,building,postal_code,town,country,booking,"
                     "is_default,sort,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,1,10,?,?)",
                     "Standard", d.get("name", ""), d.get("iban", "").replace(" ", "").upper(), d.get("bic", ""),
                     d.get("street", ""), d.get("building", ""), d.get("postal_code", ""), d.get("town", ""),
                     d.get("country", "CH"),
                     d.get("booking", "batch") if d.get("booking") in pain001.BOOKING_MODES else "batch",
                     now(), "config.toml")
        acc_id = self.store.one("SELECT id FROM accounts ORDER BY id LIMIT 1")["id"]
        self.store.x("UPDATE exports SET account_id=? WHERE account_id IS NULL", acc_id)   # bisherige Exporte

    def save_account(self, user: str, data: dict, account_id: int | None = None) -> dict:
        unknown = set(data) - set(self.ACCOUNT_FIELDS)
        if unknown:
            raise ValueError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
        old = self.store.one("SELECT * FROM accounts WHERE id=?", account_id) if account_id else None
        if account_id and not old:
            raise KeyError(account_id)
        acc = dict(old or {"bic": "", "street": "", "building": "", "postal_code": "", "town": "",
                           "country": "CH", "currency": "", "rules": "{}", "is_default": 0, "sort": 100, "active": 1,
                           "booking": "batch"})
        acc["rules"] = json.loads(acc["rules"]) if isinstance(acc["rules"], str) else acc["rules"]
        acc.update(data)
        # Prüfung
        acc["label"] = (acc.get("label") or "").strip()
        acc["name"] = (acc.get("name") or "").strip()
        acc["iban"] = (acc.get("iban") or "").replace(" ", "").upper()
        acc["bic"] = (acc.get("bic") or "").replace(" ", "").upper()
        acc["country"] = (acc.get("country") or "CH").strip().upper()
        acc["currency"] = (acc.get("currency") or "").upper()
        if not acc["label"]:
            raise ValueError("Bezeichnung fehlt")
        if not acc["name"]:
            raise ValueError("Kontoinhaber fehlt")
        if not iban_valid(acc["iban"]):
            raise ValueError("IBAN ungültig")
        if is_qr_iban(acc["iban"]):
            raise ValueError("Eine QR-IBAN kann kein Belastungskonto sein")
        if acc["bic"] and not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", acc["bic"]):
            raise ValueError("BIC ungültig")
        if acc["currency"] not in ("", "CHF", "EUR"):
            raise ValueError("Währung: leer, CHF oder EUR")
        if not re.fullmatch(r"[A-Z]{2}", acc["country"]):
            raise ValueError("Land: zweistelliger Code, z. B. CH")
        acc["booking"] = acc.get("booking") or "batch"
        if acc["booking"] not in pain001.BOOKING_MODES:
            raise ValueError("Verbuchung: batch, single oder bank")
        rules = acc.get("rules") or {}
        if not isinstance(rules, dict):
            raise ValueError("Regeln ungültig")
        acc["rules"] = {k: sorted({str(v).strip() for v in rules.get(k, []) if str(v).strip()}, key=str.lower)
                        for k in ("tags", "correspondents", "storage_paths")}
        acc["is_default"] = int(bool(acc.get("is_default")))
        acc["active"] = int(bool(acc.get("active", 1)))
        try:
            acc["sort"] = int(acc.get("sort") or 100)
        except (TypeError, ValueError):
            raise ValueError("Reihenfolge muss eine Zahl sein") from None
        cols = [c for c in self.ACCOUNT_FIELDS]
        vals = [json.dumps(acc["rules"]) if c == "rules" else acc[c] for c in cols]
        if account_id:
            self.store.x(f"UPDATE accounts SET {', '.join(c + '=?' for c in cols)}, updated_at=?, updated_by=? WHERE id=?",
                         *vals, now(), user, account_id)
            changed = [c for c in cols if str(old[c]) != str(json.dumps(acc[c]) if c == "rules" else acc[c])]
            self.store.audit(user, None, "Konto geändert", f"{acc['label']}: {', '.join(changed) or '–'}")
        else:
            account_id = self.store.x(f"INSERT INTO accounts ({', '.join(cols)}, updated_at, updated_by) "
                                      f"VALUES ({', '.join('?' for _ in cols)}, ?, ?)", *vals, now(), user)
            self.store.audit(user, None, "Konto angelegt", f"{acc['label']} ({acc['iban']})")
        return next(a for a in self.store.accounts() if a["id"] == account_id)

    def delete_account(self, user: str, account_id: int) -> str:
        acc = self.store.one("SELECT * FROM accounts WHERE id=?", account_id)
        if not acc:
            raise KeyError(account_id)
        if self.store.one("SELECT id FROM exports WHERE account_id=? LIMIT 1", account_id):
            self.store.x("UPDATE accounts SET active=0, is_default=0, updated_at=?, updated_by=? WHERE id=?",
                         now(), user, account_id)
            self.store.audit(user, None, "Konto deaktiviert", f"{acc['label']} (wird in Exporten verwendet)")
            return "deactivated"
        self.store.x("DELETE FROM accounts WHERE id=?", account_id)
        self.store.audit(user, None, "Konto gelöscht", acc["label"])
        return "deleted"

    def revert(self, pl: Paperless, user: str, export_id: int) -> dict:
        ex = self.store.one("SELECT * FROM exports WHERE id=?", export_id)
        if not ex:
            raise KeyError(export_id)
        if ex["reverted_at"]:
            raise ValueError(["Export wurde bereits rückgängig gemacht"])
        items = self.store.q("SELECT doc_id, part FROM export_items WHERE export_id=?", export_id)
        docs = list(dict.fromkeys(r["doc_id"] for r in items))
        reopened, notes = [], []
        with self.store.tx() as s:
            s.db.execute("UPDATE exports SET reverted_at=?, reverted_by=? WHERE id=?", (now(), user, export_id))
            for d in docs:
                parts = [r["part"] for r in items if r["doc_id"] == d]
                st = s.db.execute("SELECT status FROM invoices WHERE doc_id=?", (d,)).fetchone()
                if st and st[0] == "exported":  # war vollständig exportiert -> wieder offen
                    s.db.execute("UPDATE invoices SET status='open', export_id=NULL WHERE doc_id=?", (d,))
                    reopened.append(d)
                else:
                    notes.append(d)
                what = ", ".join(f"Rate {p}" for p in parts if p) or "ganze Rechnung"
                s.db.execute("INSERT INTO audit (ts,user,doc_id,action,detail) VALUES (?,?,?,?,?)",
                             (now(), user, d, "Export rückgängig", f"Export #{export_id} ({what})"))
        note = f"Export #{export_id} ({ex['filename']}) rückgängig gemacht durch {user}"
        warnings = self._tag(pl, reopened, exported=False, note=note)
        for d in notes:
            warnings += self._note(pl, d, note)
        warnings += self.write_payment_fields(pl, docs)
        return {"id": export_id, "count": len(items), "warnings": warnings}

    # ---------------------------------------------------------------- Zahlbetrag / Zahlungsdatum in paperless
    def amount_field_shared(self) -> bool:
        src = (self.cfg.get("amount_field") or "").strip().lower()
        dst = (self.cfg.get("web", {}).get("paid_amount_field", "zahlbetrag") or "").strip().lower()
        return bool(src) and src == dst

    def payment_values(self, doc_id: int) -> tuple[Decimal | None, str | None, str]:
        """(Betrag, Datum, Währung) für die paperless-Felder.

        Ganze Rechnung exportiert: bezahlter Betrag und Ausführungsdatum.
        Raten, noch nicht alle exportiert: offener Restbetrag, kein Datum.
        Alle Raten exportiert: Summe der Raten und Datum der letzten Rate. Nichts exportiert: beides leer."""
        r = self.store.invoice(doc_id)
        paid = self.paid_parts(doc_id).get(doc_id, {})
        e = self.effective(r) if r else None
        ccy = e["effective"]["currency"] if e else "CHF"
        if not paid or not e:
            return None, None, ccy
        total = sum((Decimal(i["amount"]) for i in paid.values()), Decimal(0))
        last = max(i["exec_date"] for i in paid.values())
        if e["split"] and len([p for p in e["_parts"] if p["exported"]]) < len(e["_parts"]):
            return e["_open_amount"], None, ccy
        return total, last, ccy

    def write_payment_fields(self, pl: Paperless, doc_ids: list[int]) -> list[str]:
        w = self.cfg.get("web", {})
        names = {"amount": w.get("paid_amount_field", "zahlbetrag"), "date": w.get("paid_date_field", "zahlungsdatum")}
        explicit = {k for k, key in (("amount", "paid_amount_field"), ("date", "paid_date_field")) if w.get(key)}
        fields, warnings = {}, []
        shared = self.amount_field_shared()
        for kind, name in names.items():
            if not name:
                continue
            try:
                f = pl.custom_field(name)
            except Exception as e:  # noqa: BLE001
                return [f"paperless-Felder nicht erreichbar ({e})"]
            if f:
                fields[kind] = f
            elif kind in explicit:   # nur melden, wenn ausdrücklich konfiguriert
                warnings.append(f"Feld «{name}» gibt es in paperless nicht")
        if not fields:
            return warnings
        for d in doc_ids:
            amount, when, ccy = self.payment_values(d)
            vals = {}
            if "amount" in fields:
                f = fields["amount"]
                if amount is not None:
                    vals[f["id"]] = field_value(f, amount, ccy)
                elif shared:   # nichts mehr bezahlt: ursprünglich erfassten Rechnungsbetrag wiederherstellen
                    row = self.store.invoice(d)
                    vals[f["id"]] = row["pl_amount"] if row else None
                else:
                    vals[f["id"]] = None
            if "date" in fields:
                vals[fields["date"]["id"]] = when
            try:
                pl.set_custom_fields(d, vals)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"#{d}: Zahlbetrag/Zahlungsdatum in paperless nicht gesetzt ({e})")
        return warnings

    def _note(self, pl: Paperless, doc_id: int, text: str) -> list[str]:
        try:
            pl.add_note(doc_id, text)
            return []
        except Exception as e:  # noqa: BLE001
            return [f"#{doc_id}: Notiz in paperless nicht gespeichert ({e})"]

    def tag_installments(self, pl: Paperless, doc_id: int, on: bool) -> list[str]:
        """Tag «Ratenzahlung» setzen bzw. entfernen."""
        name = self.cfg.get("tags", {}).get("installments", "Ratenzahlung")
        if not name:
            return []
        try:
            tag = pl.tag_id(name, create=True)
            doc = pl.get_document(doc_id)
            if on != (tag in doc["tags"]):
                pl.update_tags(doc, add=[tag] if on else [], remove=[] if on else [tag])
            return []
        except Exception as e:  # noqa: BLE001
            return [f"Tag «{name}» in paperless nicht gesetzt ({e})"]

    def _tag(self, pl: Paperless, ids: list[int], exported: bool, note: str) -> list[str]:
        tcfg = self.cfg.get("tags", {})
        warnings = []
        try:
            pending = pl.tag_id(tcfg.get("pending", "QR zu zahlen"))
            done = pl.tag_id(tcfg.get("exported", "QR exportiert"), create=True)
        except Exception as e:  # noqa: BLE001
            return [f"paperless-Tags nicht erreichbar: {e}"]
        remove_pending = tcfg.get("remove_pending", True)
        for i in ids:
            try:
                doc = pl.get_document(i)
                if exported:
                    pl.update_tags(doc, add=[done], remove=[pending] if remove_pending else [])
                else:
                    pl.update_tags(doc, add=[pending], remove=[done])
                pl.add_note(i, note)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"#{i}: paperless nicht aktualisiert ({e})")
        return warnings

    # ---------------------------------------------------------------- Auswertungen
    def stats(self, visible: set[int] | None = None, account: str | None = None, horizon: str = "auto") -> dict:
        """account: None = alle, "none" = ohne Zuordnung, sonst Konto-ID."""
        today = date.today()

        def acc_match(acc_id):
            return account in (None, "", "all") or (str(acc_id) if acc_id else "none") == str(account)

        items = [e for e in self.list("open", visible) if acc_match(e["account"]["id"] if e["account"] else None)]
        hist_items = [it for it in self.store.q(
            "SELECT i.*, x.account_id FROM export_items i JOIN exports x ON x.id=i.export_id WHERE x.reverted_at IS NULL")
            if (visible is None or it["doc_id"] in visible) and acc_match(it["account_id"])]
        cur = sorted({e["effective"]["currency"] for e in items}
                     | {r["currency"] for r in hist_items}
                     or {"CHF"})

        def zero():
            return {c: Decimal(0) for c in cur}

        kpi = {"open": zero(), "open_count": 0, "due7": zero(), "overdue": zero(), "overdue_count": 0,
               "held": zero(), "held_count": 0, "errors": sum(1 for e in items if e["errors"]),
               "exportable": sum(1 for e in items if e["exportable"])}
        creditors: dict[str, dict] = defaultdict(lambda: {"sum": zero(), "count": 0, "oldest_due": None})
        flows: list[tuple] = []   # (Datum oder None = überfällig, Währung, Betrag, "open"|"scheduled")

        for e in items:
            ccy = e["effective"]["currency"]
            if e["_amount"] is None:
                continue
            # Zahlungseinheiten: offene Raten oder die ganze Rechnung
            due_inv = to_date(e["due_date"])
            if e["split"]:
                units = [(p["_amount"], p["_date"], p["_date"]) for p in e["_parts"]
                         if not p["exported"] and p["_amount"] and p["_date"]]
            else:
                units = [(e["_amount"], e["_exec"], due_inv)]
            rest = sum((u[0] for u in units), Decimal(0))
            if e["held"]:
                kpi["held"][ccy] += rest
                kpi["held_count"] += 1
                continue
            kpi["open"][ccy] += rest
            kpi["open_count"] += 1
            if not e["split"] and due_inv and due_inv < today:
                kpi["overdue"][ccy] += rest
                kpi["overdue_count"] += 1
            name = e["effective"]["creditor"]["name"] or e["correspondent"] or e["title"]
            c = creditors[name]
            c["sum"][ccy] += rest
            c["count"] += 1
            for amt, ex, due in units:
                if due and due <= today + timedelta(days=7):
                    kpi["due7"][ccy] += amt
                if due and (c["oldest_due"] is None or due.isoformat() < c["oldest_due"]):
                    c["oldest_due"] = due.isoformat()
                # Liquidität nach Ausführungsdatum
                flows.append((None if (not e["split"] and due and due < today) else ex, ccy, amt, "open"))

        # bereits exportierte, noch nicht ausgeführte Zahlungen belasten das Konto ebenfalls
        for it in hist_items:
            d = to_date(it["exec_date"])
            if d and d >= today:
                flows.append((d, it["currency"], to_amount(it["amount"]) or Decimal(0), "scheduled"))

        unit, periods, end = timeline(horizon, today, max((f[0] for f in flows if f[0]), default=None))
        buckets = [{"key": "overdue", "label": "Überfällig", "open": zero(), "scheduled": zero()}]
        buckets += [{"key": p[0].isoformat(), "label": p[2], "from": p[0].isoformat(),
                     "to": (p[1] - timedelta(days=1)).isoformat(), "open": zero(), "scheduled": zero()} for p in periods]
        later = {"key": "later", "label": "Später", "open": zero(), "scheduled": zero()}
        for d, ccy, amt, kind in flows:
            if d is None:
                b = buckets[0]
            elif d >= end:
                b = later
            else:
                b = next(buckets[i + 1] for i, p in enumerate(periods) if p[0] <= d < p[1]) if d >= periods[0][0] \
                    else buckets[1]
            b[kind].setdefault(ccy, Decimal(0))
            b[kind][ccy] += amt
        if any(later["open"].values()) or any(later["scheduled"].values()):
            buckets.append(later)

        # Historie (letzte 12 Monate, nach Ausführungsdatum)
        start = date(today.year - (1 if today.month < 12 else 0), (today.month % 12) + 1, 1)
        months = []
        d = start
        while d <= today:
            months.append(d.strftime("%Y-%m"))
            d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        hist = {m: zero() for m in months}
        top: dict[str, dict] = defaultdict(lambda: {"sum": zero(), "count": 0})
        for it in hist_items:
            if it["exec_date"] < start.isoformat():
                continue
            m = it["exec_date"][:7]
            a = to_amount(it["amount"]) or Decimal(0)
            if m in hist:
                hist[m].setdefault(it["currency"], Decimal(0))
                hist[m][it["currency"]] += a
            t = top[it["creditor"]]
            t["sum"].setdefault(it["currency"], Decimal(0))
            t["sum"][it["currency"]] += a
            t["count"] += 1

        issues = [{"doc_id": e["doc_id"], "title": e["title"],
                   "creditor": e["effective"]["creditor"]["name"] or e["correspondent"],
                   "kind": "duplicate" if e["duplicate"] and not e["overrides"].get("dup_ok")
                   else ("scan" if e["scan_error"] else "error"),
                   "messages": e["errors"]} for e in items if e["errors"]]

        def ser(d):
            return {k: str(v.quantize(Decimal("0.01"))) for k, v in d.items()}

        return {
            "currencies": cur, "today": today.isoformat(),
            "kpi": {k: ser(v) if isinstance(v, dict) else v for k, v in kpi.items()},
            "creditors": sorted(({"name": k, "sum": ser(v["sum"]), "count": v["count"],
                                  "oldest_due": v["oldest_due"]} for k, v in creditors.items()),
                                key=lambda x: -sum(Decimal(s) for s in x["sum"].values())),
            "liquidity": [{**b, "open": ser(b["open"]), "scheduled": ser(b["scheduled"]),
                           "sum": ser({c: b["open"].get(c, Decimal(0)) + b["scheduled"].get(c, Decimal(0))
                                       for c in set(b["open"]) | set(b["scheduled"])})} for b in buckets],
            "timeline": {"unit": unit, "horizon": horizon, "end": end.isoformat()},
            "balances": self.balances(account),
            "history": [{"month": m, "sum": ser(v)} for m, v in hist.items()],
            "top_creditors": sorted(({"name": k, "sum": ser(v["sum"]), "count": v["count"]}
                                     for k, v in top.items()),
                                    key=lambda x: -sum(Decimal(s) for s in x["sum"].values()))[:10],
            "issues": issues,
        }


BOOKING_LABEL = {"batch": "Sammelbuchung", "single": "Einzelbuchung", "bank": "Bankvorgabe"}
MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
SCAN_VERSION = 2   # 2: alle QR-Codes eines Dokuments (Ratenscheine)
HORIZONS = {"30d": ("day", 30), "8w": ("week", 8), "3m": ("week", 13), "6m": ("month", 6), "12m": ("month", 12)}


def _month_start(d: date, add: int = 0) -> date:
    m = d.month - 1 + add
    return date(d.year + m // 12, m % 12 + 1, 1)


def timeline(horizon: str, today: date, last: date | None) -> tuple[str, list[tuple[date, date, str]], date]:
    """Perioden (von, bis exklusiv, Beschriftung) für die Liquiditätsvorschau.

    «auto»: bis zur letzten geplanten Zahlung; Tage bis 31 Tage, Wochen bis 16 Wochen, sonst Monate."""
    if horizon in HORIZONS:
        unit, n = HORIZONS[horizon]
    else:
        span = ((last or today) - today).days
        if span <= 31:
            unit, n = "day", max(14, span + 1)
        elif span <= 16 * 7:
            unit, n = "week", span // 7 + 1
        else:
            unit, n = "month", min(24, (last.year - today.year) * 12 + last.month - today.month + 1)
    periods = []
    if unit == "day":
        for i in range(n):
            d = today + timedelta(days=i)
            periods.append((d, d + timedelta(days=1), f"{DAYS[d.weekday()]} {d:%d.%m.}"))
    elif unit == "week":
        start = today - timedelta(days=today.weekday())
        for i in range(n):
            d = start + timedelta(weeks=i)
            periods.append((d, d + timedelta(weeks=1), f"KW {d.isocalendar()[1]}"))
    else:
        for i in range(n):
            d = _month_start(today, i)
            periods.append((d, _month_start(today, i + 1), f"{MONTHS[d.month - 1]} {d:%y}"))
    return unit, periods, periods[-1][1]


def resolve_account(row: dict, currency: str, accounts: list[dict]) -> dict | None:
    """Belastungskonto einer Rechnung bestimmen.

    1. nur aktive Konten mit passender Währung (leer = alle Währungen)
    2. Regeln: Tag, Korrespondent oder Speicherpfad aus paperless (Gross-/Kleinschreibung egal)
    3. sonst Standardkonto; bei mehreren gewinnt die kleinere Reihenfolge-Nummer
    """
    tags = {t.lower() for t in (row.get("tags") or [])}
    corr = (row.get("correspondent") or "").lower()
    spath = (row.get("storage_path") or "").lower()
    # passende Währung zuerst (EUR-Konto vor «alle Währungen»), dann Reihenfolge
    eligible = sorted((a for a in accounts if not a["currency"] or a["currency"] == currency),
                      key=lambda a: (a["currency"] != currency, a["sort"], a["id"]))

    def hit(a):
        r = a["rules"] or {}
        for t in r.get("tags", []):
            if t.lower() in tags:
                return f"Tag «{t}»"
        for c in r.get("correspondents", []):
            if c.lower() == corr:
                return f"Korrespondent «{c}»"
        for sp in r.get("storage_paths", []):
            if sp.lower() == spath:
                return f"Speicherpfad «{sp}»"
        return None

    for a in eligible:
        why = hit(a)
        if why:
            return {"id": a["id"], "label": a["label"], "why": why}
    for a in eligible:
        if a["is_default"]:
            return {"id": a["id"], "label": a["label"], "why": "Standardkonto" + (f" {a['currency']}" if a["currency"] else "")}
    return None


def field_value(field: dict, amount: Decimal, ccy: str):
    """Betrag passend zum Feldtyp in paperless: Geldbetrag «CHF123.45», Zahl oder Text."""
    v = amount.quantize(Decimal("0.01"))
    t = field.get("data_type")
    if t == "monetary":
        return f"{ccy}{v}"
    if t == "float":
        return float(v)
    if t == "integer":
        return int(v.to_integral_value())
    return str(v)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})\b")
_DUE_WORDS = re.compile(r"(fällig|zahlbar|bis|per|spätestens|termin|échéance|payable|due)\W*(am|le|on)?\W*$", re.I)


def due_from_bill(b: QRBill) -> date | None:
    """Fälligkeit eines Einzahlungsscheins: Swico-Rechnungsinformationen (//S1/ Datum + Zahlungsfrist)
    oder ein Datum in der Mitteilung («fällig am 31.10.2026», «zahlbar bis …»)."""
    info = b.bill_info or ""
    if info.startswith("//S1/"):
        tags, parts = {}, info[5:].split("/")
        for i in range(0, len(parts) - 1, 2):
            tags[parts[i]] = parts[i + 1]
        try:
            inv = datetime.strptime(tags["11"][:6], "%y%m%d").date() if tags.get("11") else None
        except ValueError:
            inv = None
        terms = [c.split(":") for c in tags.get("40", "").split(";") if ":" in c]
        days = [int(t[1]) for t in terms if t[1].isdigit() and t[0] in ("0", "0.0", "0.00")]
        if inv and days:
            return inv + timedelta(days=max(days))
    text = " ".join(x for x in (b.message, info) if x)
    hits = []
    for m in _DATE_RE.finditer(text):
        d_, mo, y = (int(g) for g in m.groups())
        y += 2000 if y < 100 else 0
        try:
            hits.append((date(y, mo, d_), bool(_DUE_WORDS.search(text[:m.start()]))))
        except ValueError:
            continue
    keyed = [d for d, k in hits if k]
    if keyed:
        return keyed[-1]
    return hits[0][0] if len(hits) == 1 else None


def account_debtor(a: dict) -> pain001.Debtor:
    return pain001.Debtor(name=a["name"], iban=a["iban"], bic=a["bic"], street=a["street"], building=a["building"],
                          postal_code=a["postal_code"], town=a["town"], country=a["country"] or "CH")


def slug(text: str) -> str:
    t = re.sub(r"[^A-Za-z0-9]+", "-", text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")).strip("-")
    return t[:30] or "Konto"


def _dup_candidates(key: tuple, idx: dict, doc_id: int) -> list[dict]:
    iban, ref, amount, _ = key
    cands = idx.get(key, [])
    if not ref:  # ohne Referenz: Historie kennt die Mitteilung nicht -> IBAN + Betrag vergleichen
        cands = cands + idx.get((iban, "", amount, "*"), [])
    return [o for o in cands if o.get("doc_id") != doc_id]


def _dup_text(dups: list[dict]) -> str:
    return "; ".join(f"bereits bezahlt mit Export #{d['export_id']} ({d['exec_date']})" if d["kind"] == "paid"
                     else f"gleich wie Dokument #{d['doc_id']}" for d in dups)


def apply_duplicates(e: dict, idx: dict) -> None:
    """Duplikatprüfung auf ein fertig berechnetes Ergebnis anwenden (Raten derselben Rechnung zählen nicht).

    Bei mehreren Einzahlungsscheinen wird jeder Schein einzeln geprüft."""
    if e["status"] != "open":
        return
    ok = e["overrides"].get("dup_ok")
    if e["_dupkey"] and not e["plan_auto"]:
        dups = _dup_candidates(e["_dupkey"], idx, e["doc_id"])
        if dups:
            e["duplicate"] = True
            if ok:
                e["warnings"].append(f"Mögliches Duplikat bestätigt: {_dup_text(dups)}")
            else:
                e["errors"].append(f"Mögliches Duplikat: {_dup_text(dups)}")
                e["exportable"] = False
                for p in e["_parts"]:
                    p["exportable"] = False
                for p in e["parts"]:
                    p["exportable"] = False
    for p, key in e.get("_partkeys", []):
        dups = _dup_candidates(key, idx, e["doc_id"])
        if not dups:
            continue
        e["duplicate"] = True
        if ok:
            e["warnings"].append(f"Rate {p['no']}: mögliches Duplikat bestätigt: {_dup_text(dups)}")
            continue
        p["errors"].append(f"Mögliches Duplikat: {_dup_text(dups)}")
        p["exportable"] = False
        for q in e["parts"]:
            if q["no"] == p["no"]:
                q["exportable"] = False
        e["warnings"].append(f"Rate {p['no']}: mögliches Duplikat ({_dup_text(dups)})")
    if e["_parts"]:
        e["exportable"] = any(p["exportable"] for p in e["_parts"])


def paid_key(iban: str, reference: str, amount: Decimal | None) -> tuple:
    """Schlüssel für bereits exportierte Zahlungen (Mitteilung ist dort nicht gespeichert)."""
    return (iban, reference, str(amount), "") if reference else (iban, "", str(amount), "*")


def dup_key(iban: str, reference: str, amount: Decimal | None, message: str) -> tuple:
    """Gleiche Zahlung = gleiche IBAN + Referenz + Betrag (ohne Referenz: + Mitteilung)."""
    return (iban, reference or "", str(amount), "" if reference else (message or "").strip().lower())


def bill_to_dict(b: QRBill) -> dict:
    c = b.creditor
    if c.adr_type == "K":
        m = re.match(r"^\s*(\d{4,5})\s+(.+)$", c.building_or_line2)
        cred = {"name": c.name, "street": c.street_or_line1, "building": "",
                "postal_code": m.group(1) if m else "", "town": m.group(2) if m else c.building_or_line2,
                "country": c.country, "combined": True}
    else:
        cred = {"name": c.name, "street": c.street_or_line1, "building": c.building_or_line2,
                "postal_code": c.postal_code, "town": c.town, "country": c.country, "combined": False}
    return {"iban": b.iban, "qr_iban": b.is_qr_iban if b.iban else False, "currency": b.currency,
            "amount": str(b.amount) if b.amount is not None else None,
            "ref_type": b.ref_type, "reference": b.reference, "message": b.message,
            "bill_info": b.bill_info, "creditor": cred}
