"""Unit-Tests: Prüfziffern, Parser, Adresstypen, pain.001-Aufbau."""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import xmlschema

sys.path.insert(0, str(Path(__file__).parent.parent))

from qr2pain import pain001  # noqa: E402
from qr2pain.swissqr import QRBillError, iban_valid, is_qr_iban, parse, qrr_valid, scor_valid  # noqa: E402

XSD = xmlschema.XMLSchema(str(Path(__file__).parent / "xsd" / "pain.001.001.09.xsd"))


def payload(adr="S", iban="CH4431999123000889012", ref_type="QRR", ref="210000000003139471430009017",
            amount="1949.75", ccy="CHF"):
    if adr == "S":
        cred = ["S", "Robert Schneider AG", "Rue du Lac", "1268", "2501", "Biel", "CH"]
    else:
        cred = ["K", "Robert Schneider AG", "Rue du Lac 1268", "2501 Biel", "", "", "CH"]
    return "\n".join(["SPC", "0200", "1", iban, *cred, *[""] * 7, amount, ccy, *[""] * 7,
                      ref_type, ref, "Rechnung 4711", "EPD"])


def test_check_digits():
    assert iban_valid("CH93 0076 2011 6238 5295 7")
    assert not iban_valid("CH93 0076 2011 6238 5295 8")
    assert is_qr_iban("CH4431999123000889012") and not is_qr_iban("CH5800791123000889012")
    assert qrr_valid("210000000003139471430009017") and not qrr_valid("210000000003139471430009018")
    assert scor_valid("RF18539007547034") and not scor_valid("RF19539007547034")


def test_parse_structured_and_combined():
    b = parse(payload("S"))
    assert b.amount == Decimal("1949.75") and b.creditor.town == "Biel" and b.ref_type == "QRR"
    k = parse(payload("K"))
    assert k.creditor.adr_type == "K" and k.creditor.building_or_line2 == "2501 Biel"


@pytest.mark.parametrize("kw,msg", [
    (dict(iban="CH5800791123000889012"), "QR-Referenz nur mit QR-IBAN"),
    (dict(ref="210000000003139471430009018"), "Prüfziffer"),
    (dict(ccy="USD"), "Währung"),
    (dict(iban="CH4431999123000889012", ref_type="SCOR", ref="RF18539007547034"), "QR-IBAN verlangt"),
])
def test_parse_rejects(kw, msg):
    with pytest.raises(QRBillError, match=msg):
        parse(payload(**kw))


def test_pain001_per_date_and_currency_valid():
    deb = pain001.Debtor(name="Muster AG", iban="CH93 0076 2011 6238 5295 7")
    s = parse(payload("S"))
    k = parse(payload("K", iban="CH5800791123000889012", ref_type="NON", ref="", ccy="EUR", amount="10"))
    xml = pain001.build(deb, [
        pain001.Payment(s, "PL1", Decimal("100"), date(2026, 10, 1)),
        pain001.Payment(s, "PL1-T2", Decimal("50"), date(2026, 11, 2)),
        pain001.Payment(k, "PL2", Decimal("10")),
    ], date(2026, 10, 1)).decode()
    XSD.validate(xml)
    assert xml.count("<PmtInf>") == 3                       # CHF 01.10., CHF 02.11., EUR 01.10.
    assert "<Prtry>QRR</Prtry>" in xml and "<AdrLine>Rue du Lac 1268</AdrLine>" in xml
    assert "<CtrlSum>160.00</CtrlSum>" in xml


def test_debtor_rejects_qr_iban():
    with pytest.raises(ValueError):
        pain001.Debtor(name="X", iban="CH4431999123000889012").check()
