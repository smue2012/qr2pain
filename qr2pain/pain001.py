"""Erzeugt pain.001.001.09 gemäss Swiss Payment Standards 2026 (IG Credit Transfer v2.3)
aus QR-Rechnungen (Mapping gem. Annex B)."""
from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .swissqr import Address, QRBill, iban_valid, is_qr_iban

NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
SOFTWARE_NAME = "qr2pain"
SOFTWARE_PROVIDER = "qr2pain contributors"
SOFTWARE_VERSION = "1.2.1"
SPS_IG_VERSION = "0203"

# Zulässiger SPS-Zeichensatz (Basic Latin, Latin-1 Supplement, Latin Extended A, ȘșȚț, €)
_ALLOWED = re.compile(r"[^\u0020-\u007E\u00A0-\u017F\u0218-\u021B\u20AC]")
_REF_ALLOWED = re.compile(r"[^A-Za-z0-9 '()+,\-./:?]")


def clean(text: str, maxlen: int) -> str:
    text = _ALLOWED.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:maxlen]


def ref_id(text: str, maxlen: int = 35) -> str:
    """Bereinigt Referenz-Elemente (MsgId, PmtInfId, InstrId, EndToEndId) gem. SPS 3.2."""
    t = _REF_ALLOWED.sub("", text).replace("//", "/").strip(" /")
    return t[:maxlen].rstrip(" /") or "NOTPROVIDED"


@dataclass
class Debtor:
    name: str
    iban: str
    bic: str = ""            # optional; sonst wird die IID aus der IBAN verwendet
    street: str = ""
    building: str = ""
    postal_code: str = ""
    town: str = ""
    country: str = "CH"

    def check(self) -> None:
        iban = self.iban.replace(" ", "").upper()
        if not iban_valid(iban):
            raise ValueError(f"Belastungs-IBAN {iban} ungültig")
        if is_qr_iban(iban):
            raise ValueError("Belastungskonto darf keine QR-IBAN sein")
        self.iban = iban


@dataclass
class Payment:
    bill: QRBill
    end_to_end_id: str
    amount: Decimal  # effektiv zu zahlender Betrag (bei leerem QR-Betrag extern gesetzt)
    execution_date: date | None = None  # None = Standarddatum des Laufs


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrib) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = text
    return el


def _postal_address(parent: ET.Element, a: Address) -> None:
    """Strukturiert (Typ S) oder hybrid (Typ K, ältere QR-Rechnungen)."""
    pa = _sub(parent, "PstlAdr")
    if a.adr_type == "S":
        if a.street_or_line1:
            _sub(pa, "StrtNm", clean(a.street_or_line1, 70))
        if a.building_or_line2:
            _sub(pa, "BldgNb", clean(a.building_or_line2, 16))
        if a.postal_code:
            _sub(pa, "PstCd", clean(a.postal_code, 16))
        _sub(pa, "TwnNm", clean(a.town, 35) or "NOTPROVIDED")
        _sub(pa, "Ctry", a.country.upper())
    else:
        # Kombiniert: Zeile 2 = "PLZ Ort" -> in TwnNm/PstCd aufteilen, Zeile 1 als AdrLine
        m = re.match(r"^\s*(\d{4,5})\s+(.+)$", a.building_or_line2)
        if m:
            _sub(pa, "PstCd", m.group(1))
            _sub(pa, "TwnNm", clean(m.group(2), 35))
        else:
            _sub(pa, "TwnNm", clean(a.building_or_line2, 35) or "NOTPROVIDED")
        _sub(pa, "Ctry", a.country.upper())
        if a.street_or_line1:
            _sub(pa, "AdrLine", clean(a.street_or_line1, 70))


def _amt(v: Decimal) -> str:
    return f"{v.quantize(Decimal('0.01'))}"


def build(debtor: Debtor, payments: list[Payment], execution_date: date,
          initiating_party: str | None = None, msg_id: str | None = None) -> bytes:
    if not payments:
        raise ValueError("Keine Zahlungen")
    debtor.check()

    now = datetime.now().astimezone().replace(microsecond=0)
    msg_id = ref_id(msg_id or f"QR2PAIN-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}")

    ET.register_namespace("", NS)
    doc = ET.Element(f"{{{NS}}}Document")
    root = _sub(doc, "CstmrCdtTrfInitn")

    # ---------------- A-Level
    total = sum((p.amount for p in payments), Decimal(0))
    gh = _sub(root, "GrpHdr")
    _sub(gh, "MsgId", msg_id)
    _sub(gh, "CreDtTm", now.isoformat())
    _sub(gh, "NbOfTxs", str(len(payments)))
    _sub(gh, "CtrlSum", _amt(total))
    ip = _sub(gh, "InitgPty")
    _sub(ip, "Nm", clean(initiating_party or debtor.name, 70))
    cd = _sub(ip, "CtctDtls")
    for code, val in (("NAME", SOFTWARE_NAME), ("PRVD", SOFTWARE_PROVIDER),
                      ("VRSN", SOFTWARE_VERSION), ("SPSV", SPS_IG_VERSION)):
        o = _sub(cd, "Othr")
        _sub(o, "ChanlTp", code)
        _sub(o, "Id", val)

    # ---------------- B-Level: je Währung und Ausführungsdatum ein Block (SPS-Vorgabe)
    groups: dict[tuple[str, date], list[Payment]] = {}
    for p in payments:
        groups.setdefault((p.bill.currency, p.execution_date or execution_date), []).append(p)

    for n, ((ccy, exec_dt), group) in enumerate(sorted(groups.items()), start=1):
        pi = _sub(root, "PmtInf")
        _sub(pi, "PmtInfId", ref_id(f"{msg_id[:30]}-{n}"))
        _sub(pi, "PmtMtd", "TRF")
        _sub(pi, "BtchBookg", "true")
        _sub(pi, "NbOfTxs", str(len(group)))
        _sub(pi, "CtrlSum", _amt(sum((p.amount for p in group), Decimal(0))))
        red = _sub(pi, "ReqdExctnDt")
        _sub(red, "Dt", exec_dt.isoformat())

        dbtr = _sub(pi, "Dbtr")
        _sub(dbtr, "Nm", clean(debtor.name, 140))
        if debtor.town:
            pa = _sub(dbtr, "PstlAdr")
            if debtor.street:
                _sub(pa, "StrtNm", clean(debtor.street, 70))
            if debtor.building:
                _sub(pa, "BldgNb", clean(debtor.building, 16))
            if debtor.postal_code:
                _sub(pa, "PstCd", clean(debtor.postal_code, 16))
            _sub(pa, "TwnNm", clean(debtor.town, 35))
            _sub(pa, "Ctry", debtor.country.upper())

        acct = _sub(_sub(pi, "DbtrAcct"), "Id")
        _sub(acct, "IBAN", debtor.iban)

        fin = _sub(_sub(pi, "DbtrAgt"), "FinInstnId")
        if debtor.bic:
            _sub(fin, "BICFI", debtor.bic.replace(" ", "").upper())
        else:
            csm = _sub(fin, "ClrSysMmbId")
            _sub(_sub(csm, "ClrSysId"), "Cd", "CHBCC")
            _sub(csm, "MmbId", str(int(debtor.iban[4:9])))

        # ---------------- C-Level
        for i, p in enumerate(group, start=1):
            b = p.bill
            tx = _sub(pi, "CdtTrfTxInf")
            pid = _sub(tx, "PmtId")
            _sub(pid, "InstrId", ref_id(f"{n}-{i}"))
            _sub(pid, "EndToEndId", ref_id(p.end_to_end_id))
            _sub(_sub(tx, "Amt"), "InstdAmt", _amt(p.amount), Ccy=ccy)

            cdtr = _sub(tx, "Cdtr")
            _sub(cdtr, "Nm", clean(b.creditor.name, 140))
            _postal_address(cdtr, b.creditor)
            _sub(_sub(_sub(tx, "CdtrAcct"), "Id"), "IBAN", b.iban)

            rmt = _sub(tx, "RmtInf")
            if b.ref_type in ("QRR", "SCOR"):
                strd = _sub(rmt, "Strd")
                cri = _sub(strd, "CdtrRefInf")
                cop = _sub(_sub(cri, "Tp"), "CdOrPrtry")
                if b.ref_type == "QRR":
                    _sub(cop, "Prtry", "QRR")
                else:
                    _sub(cop, "Cd", "SCOR")
                _sub(cri, "Ref", b.reference)
                for extra in (b.message, b.bill_info):
                    if extra.strip():
                        _sub(strd, "AddtlRmtInf", clean(extra, 140))
            else:
                text = b.message or b.bill_info
                if text.strip():
                    _sub(rmt, "Ustrd", clean(text, 140))
                else:
                    tx.remove(rmt)

    ET.indent(doc, space="  ")
    xml = ET.tostring(doc, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n").encode("utf-8")
