"""Parser und Validierung für Swiss QR Code (QR-Rechnung, Implementation Guidelines v2.x)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


class QRBillError(ValueError):
    pass


@dataclass
class Address:
    adr_type: str  # "S" strukturiert, "K" kombiniert (seit Nov. 2025 nicht mehr zulässig, ältere Rechnungen)
    name: str
    street_or_line1: str = ""
    building_or_line2: str = ""
    postal_code: str = ""
    town: str = ""
    country: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.name


@dataclass
class QRBill:
    iban: str
    creditor: Address
    amount: Decimal | None
    currency: str
    debtor: Address | None
    ref_type: str          # QRR | SCOR | NON
    reference: str
    message: str = ""      # unstrukturierte Mitteilung
    bill_info: str = ""    # Rechnungsinformationen (//S1/...)
    alt_procedures: list[str] = field(default_factory=list)

    @property
    def is_qr_iban(self) -> bool:
        return is_qr_iban(self.iban)


# ---------------------------------------------------------------- Prüfziffern

def iban_valid(iban: str) -> bool:
    iban = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    num = "".join(str(int(c, 36)) for c in rearranged)
    return int(num) % 97 == 1


def is_qr_iban(iban: str) -> bool:
    iban = iban.replace(" ", "").upper()
    if iban[:2] not in ("CH", "LI") or not iban[4:9].isdigit():
        return False
    return 30000 <= int(iban[4:9]) <= 31999


_MOD10_TABLE = [0, 9, 4, 6, 8, 2, 7, 1, 3, 5]


def qrr_valid(ref: str) -> bool:
    ref = ref.replace(" ", "")
    if not re.fullmatch(r"\d{27}", ref):
        return False
    carry = 0
    for ch in ref[:-1]:
        carry = _MOD10_TABLE[(carry + int(ch)) % 10]
    return (10 - carry) % 10 == int(ref[-1])


def scor_valid(ref: str) -> bool:
    ref = ref.replace(" ", "").upper()
    if not re.fullmatch(r"RF\d{2}[A-Z0-9]{1,21}", ref):
        return False
    rearranged = ref[4:] + ref[:4]
    num = "".join(str(int(c, 36)) for c in rearranged)
    return int(num) % 97 == 1


# ---------------------------------------------------------------- Parser

def _addr(f: list[str], i: int) -> Address:
    return Address(*[x.strip() for x in f[i:i + 7]])


def parse(payload: str, check: bool = True) -> QRBill:
    """Parst den Inhalt eines Swiss QR Codes. Wirft QRBillError bei ungültigen Daten
    (mit check=False nur bei strukturell unlesbarem Payload)."""
    lines = payload.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) < 31 or lines[0].strip() != "SPC":
        raise QRBillError("Kein Swiss QR Code (Header 'SPC' fehlt)")
    if not lines[1].strip().startswith("02"):
        raise QRBillError(f"Nicht unterstützte QR-Version {lines[1]!r}")
    if lines[30].strip() != "EPD":
        raise QRBillError("Trailer 'EPD' fehlt – Payload unvollständig")

    lines += [""] * (34 - len(lines))
    iban = lines[3].strip().replace(" ", "").upper()
    creditor = _addr(lines, 4)
    # 11-17: Endgültiger Zahlungsempfänger (reserviert, leer)

    amt_raw = lines[18].strip()
    amount = None
    if amt_raw:
        try:
            amount = Decimal(amt_raw)
        except InvalidOperation as e:
            raise QRBillError(f"Ungültiger Betrag {amt_raw!r}") from e
    currency = lines[19].strip()
    debtor = _addr(lines, 20)
    ref_type = lines[27].strip()
    reference = lines[28].strip().replace(" ", "")
    message = lines[29].strip()
    bill_info = lines[31].strip() if len(lines) > 31 else ""
    alt = [l.strip() for l in lines[32:34] if l.strip()]

    bill = QRBill(iban, creditor, amount, currency,
                  None if debtor.is_empty else debtor,
                  ref_type, reference, message, bill_info, alt)
    if check:
        validate(bill)
    return bill


def validate(b: QRBill) -> None:
    if not iban_valid(b.iban):
        raise QRBillError(f"IBAN {b.iban} ungültig")
    if b.iban[:2] not in ("CH", "LI"):
        raise QRBillError(f"IBAN {b.iban} ist keine CH/LI-IBAN")
    if b.currency not in ("CHF", "EUR"):
        raise QRBillError(f"Währung {b.currency!r} nicht erlaubt")
    if b.amount is not None and not (Decimal("0.01") <= b.amount <= Decimal("999999999.99")):
        raise QRBillError(f"Betrag {b.amount} ausserhalb des zulässigen Bereichs")
    if b.creditor.is_empty or not b.creditor.country:
        raise QRBillError("Zahlungsempfänger unvollständig")

    if b.is_qr_iban:
        if b.ref_type != "QRR":
            raise QRBillError("QR-IBAN verlangt Referenztyp QRR")
        if not qrr_valid(b.reference):
            raise QRBillError(f"QR-Referenz {b.reference} ungültig (Prüfziffer)")
    else:
        if b.ref_type == "QRR":
            raise QRBillError("QR-Referenz nur mit QR-IBAN zulässig")
        if b.ref_type == "SCOR" and not scor_valid(b.reference):
            raise QRBillError(f"Creditor Reference {b.reference} ungültig")
        if b.ref_type == "NON" and b.reference:
            raise QRBillError("Referenztyp NON darf keine Referenz enthalten")
        if b.ref_type not in ("SCOR", "NON"):
            raise QRBillError(f"Unbekannter Referenztyp {b.ref_type!r}")
