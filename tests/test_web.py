"""Webfrontend end-to-end gegen ein simuliertes paperless mit zwei Benutzern.

stephan     sieht alle Dokumente (Superuser)
buchhaltung sieht nur 101–105
"""
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
import xmlschema

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

XSD = xmlschema.XMLSchema(str(HERE / "xsd" / "pain.001.001.09.xsd"))
D = lambda n: (date.today() + timedelta(days=n)).isoformat()  # noqa: E731


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    import mock_paperless
    srv = mock_paperless.serve(0)
    tmp = tmp_path_factory.mktemp("qr2pain")
    cfg = tmp / "config.toml"
    cfg.write_text(f"""
amount_field = "Betrag"
[paperless]
url = "http://127.0.0.1:{srv.server_address[1]}"
[tags]
pending = "QR zu zahlen"
exported = "QR exportiert"
[debtor]
name = "Muster AG"
iban = "CH93 0076 2011 6238 5295 7"
town = "Luzern"
[web]
due_field = "Fällig am"
""", encoding="utf-8")
    os.environ["QR2PAIN_CONFIG"] = str(cfg)
    os.environ["QR2PAIN_DATA"] = str(tmp / "data")
    from fastapi.testclient import TestClient
    from qr2pain.web.app import app

    def client(user, pw):
        c = TestClient(app, headers={"X-Requested-With": "qr2pain"})
        r = c.post("/api/login", json={"username": user, "password": pw})
        assert r.status_code == 200, r.text
        return c

    yield {"client": client, "mock": mock_paperless}
    srv.shutdown()


def sync(c):
    assert c.post("/api/sync").status_code == 200
    for _ in range(300):
        st = c.get("/api/sync").json()
        if not st["running"]:
            return st
        time.sleep(0.1)
    raise AssertionError("Sync hängt")


def ids(c):
    return sorted(x["doc_id"] for x in c.get("/api/invoices").json())


def test_login_and_csrf(web):
    from fastapi.testclient import TestClient
    from qr2pain.web.app import app
    anon = TestClient(app)
    assert anon.get("/api/invoices").status_code == 401
    assert anon.post("/api/login", json={"username": "stephan", "password": "falsch"},
                     headers={"X-Requested-With": "qr2pain"}).status_code == 401
    assert anon.post("/api/sync").status_code == 403        # ohne CSRF-Header
    h = anon.get("/api/health").json()
    assert h["status"] == "ok" and h["version"]


def test_permissions(web):
    bh = web["client"]("buchhaltung", "geheim2")
    st = web["client"]("stephan", "geheim")
    assert sync(bh)["new"] == 5
    assert ids(bh) == [101, 102, 103, 104, 105]
    assert sync(st)["new"] == 6
    assert ids(st) == list(range(101, 112))
    # erneuter Sync mit weniger Rechten entfernt nichts
    s = sync(bh)
    assert s["removed"] == 0 and s.get("hidden") == 6
    assert len(ids(st)) == 11
    # Direktzugriffe auf fremde Dokumente
    for method, url, kw in [("get", "/api/invoices/108", {}), ("patch", "/api/invoices/108", {"json": {"comment": "x"}}),
                            ("get", "/api/invoices/108/pages", {}),
                            ("post", "/api/invoices/hold", {"json": {"ids": [108], "held": True}}),
                            ("post", "/api/exports", {"json": {"items": ["108"]}})]:
        assert getattr(bh, method)(url, **kw).status_code == 404, url
    assert Decimal_(bh.get("/api/stats").json()["kpi"]["open"]["CHF"]) < Decimal_(st.get("/api/stats").json()["kpi"]["open"]["CHF"])


def Decimal_(v):
    from decimal import Decimal
    return Decimal(v)


def test_duplicates_installments_export_revert(web):
    st = web["client"]("stephan", "geheim")
    L = {x["doc_id"]: x for x in st.get("/api/invoices").json()}
    assert L[105]["duplicate"] and not L[105]["exportable"]
    st.patch("/api/invoices/105", json={"dup_ok": True})
    assert st.get("/api/invoices/105").json()["exportable"]

    # Raten: Summe muss stimmen
    bad = st.patch("/api/invoices/108", json={"installments": [{"amount": "600", "date": D(3)},
                                                                {"amount": "600", "date": D(40)}]}).json()
    assert any("Raten ergeben" in e for e in bad["errors"])
    ok = st.patch("/api/invoices/108", json={"installments": [{"amount": "600", "date": D(3)},
                                                               {"amount": "613.40", "date": D(40)}]}).json()
    assert ok["split"] and ok["exportable"] and not ok["errors"]
    assert "Ratenzahlung" in tag_names(web, 108)
    m = web["mock"]
    for name in ("Ratenzahlung",):   # von qr2pain angelegte Tags gehören niemandem (sonst «privat»)
        tid = next(k for k, v in m.TAGS.items() if v == name)
        assert m.TAG_OWNERS[tid] is None

    ex = st.post("/api/exports", json={"items": ["108:1", "105", "102"]}).json()
    xml = st.get(f"/api/exports/{ex['id']}/xml").text
    XSD.validate(xml)
    assert "PL108-ASN2108-T1" in xml and "Teilzahlung 1/2" in xml

    L = {x["doc_id"]: x for x in st.get("/api/invoices").json()}
    assert 105 not in L and 102 not in L and L[108]["parts_done"] == 1
    assert any("bereits bezahlt" in e for e in L[106]["errors"])        # Duplikat gegen Historie
    assert st.patch("/api/invoices/108", json={"amount": "1"}).status_code == 400   # gesperrt
    assert "QR exportiert" in tag_names(web, 102) and "QR zu zahlen" in tag_names(web, 108)
    assert web["mock"].TAG_OWNERS[next(k for k, v in web["mock"].TAGS.items() if v == "QR exportiert")] is None

    # Export mit fremder Position: buchhaltung sieht nur eigene, darf nicht herunterladen
    bh = web["client"]("buchhaltung", "geheim2")
    mine = [e for e in bh.get("/api/exports").json() if e["id"] == ex["id"]][0]
    assert mine["hidden"] == 1 and {i["doc_id"] for i in mine["items"]} == {105, 102}
    assert bh.get(f"/api/exports/{ex['id']}/xml").status_code == 404

    rv = st.post(f"/api/exports/{ex['id']}/revert").json()
    assert rv["count"] == 3
    L = {x["doc_id"]: x for x in st.get("/api/invoices").json()}
    assert 105 in L and 102 in L and L[108]["parts_done"] == 0
    assert "QR zu zahlen" in tag_names(web, 102)


def tag_names(web, doc_id):
    m = web["mock"]
    return [m.TAGS[t] for t in m.DOCS[doc_id]["tags"]]


# ---------------------------------------------------------------- Zahlungskonten

ACC_A = {"label": "Firma A CHF", "name": "Firma A AG", "iban": "CH93 0076 2011 6238 5295 7", "town": "Luzern", "currency": "CHF",
         "is_default": True, "sort": 10}
ACC_E = {"label": "Firma A EUR", "name": "Firma A AG", "iban": "CH5604835012345678009", "town": "Luzern",
         "currency": "EUR", "is_default": True, "sort": 20}
ACC_B = {"label": "Firma B", "name": "Firma B GmbH", "iban": "CH7609000000123456789", "town": "Zug",
         "rules": {"tags": ["firma b"], "storage_paths": ["Firma B/Rechnungen"]}, "sort": 30}


def test_accounts_permissions_and_validation(web):
    st = web["client"]("stephan", "geheim")
    bh = web["client"]("buchhaltung", "geheim2")
    assert st.get("/api/me").json()["superuser"] and not bh.get("/api/me").json()["superuser"]
    accs = st.get("/api/accounts").json()
    assert [a["label"] for a in accs] == ["Standard"] and accs[0]["is_default"]   # aus config.toml übernommen
    assert bh.post("/api/accounts", json=ACC_B).status_code == 403
    assert bh.get("/api/accounts").status_code == 200                          # lesen darf jeder
    for bad, msg in [({**ACC_B, "iban": "CH12 3456"}, "IBAN"), ({**ACC_B, "iban": "CH4431999123000889012"}, "QR-IBAN"),
                     ({**ACC_B, "currency": "USD"}, "Währung"), ({**ACC_B, "label": ""}, "Bezeichnung")]:
        r = st.post("/api/accounts", json=bad)
        assert r.status_code == 400 and msg in r.json()["detail"][0], r.json()


def test_accounts_assignment_and_export(web):
    st = web["client"]("stephan", "geheim")
    std = st.get("/api/accounts").json()[0]
    a = st.post("/api/accounts", json=ACC_A).json()
    e = st.post("/api/accounts", json=ACC_E).json()
    b = st.post("/api/accounts", json=ACC_B).json()
    assert st.patch(f"/api/accounts/{std['id']}", json={"is_default": False, "sort": 90}).status_code == 200
    sync(st)                                          # Tags/Speicherpfad aus paperless übernehmen
    L = {x["doc_id"]: x for x in st.get("/api/invoices").json()}
    assert L[101]["account"]["id"] == a["id"]         # Standardkonto CHF
    assert L[102]["account"]["id"] == e["id"]         # EUR -> EUR-Konto
    assert L[109]["account"]["id"] == b["id"] and "Tag" in L[109]["account"]["why"]
    assert L[110]["account"]["id"] == b["id"] and "Speicherpfad" in L[110]["account"]["why"]
    assert L[110]["storage_path"] == "Firma B/Rechnungen" and L[101]["storage_path"] is None

    # Export: 101 (A), 102 (E), 109 (B), 103 übersteuert auf B -> 3 Dateien
    r = st.post("/api/exports", json={"items": ["101", "102", "109", "103"],
                                      "accounts": {str(a["id"]): a["id"]}})
    assert r.status_code == 200, r.text
    # 103 gehört zu A; Gruppe A komplett auf A lassen -> 3 Dateien (A: 101+103, E: 102, B: 109)
    out = r.json()
    files = {x["account"]["label"]: x for x in out["exports"]}
    assert set(files) == {"Firma A CHF", "Firma A EUR", "Firma B"} and files["Firma A CHF"]["count"] == 2
    for label, x in files.items():
        xml = st.get(f"/api/exports/{x['id']}/xml").text
        XSD.validate(xml)
        iban = {"Firma A CHF": "CH9300762011623852957", "Firma A EUR": "CH5604835012345678009",
                "Firma B": "CH7609000000123456789"}[label]
        assert f"<IBAN>{iban}</IBAN>" in xml.split("<CdtTrfTxInf>")[0], label   # Belastungskonto im B-Level
    ex = {x["id"]: x for x in st.get("/api/exports").json()}
    assert ex[files["Firma B"]["id"]]["account"]["label"] == "Firma B"
    st_ = st.get(f"/api/stats?account={b['id']}").json()
    assert st_["kpi"]["open"].get("CHF") == "318.60"                    # nur noch 110 offen bei Firma B
    for x in out["exports"]:
        st.post(f"/api/exports/{x['id']}/revert")

    # Übersteuerung: EUR-Rechnung auf ein reines CHF-Konto ist nicht erlaubt
    r = st.post("/api/exports", json={"items": ["102"], "accounts": {str(e["id"]): a["id"]}})
    assert r.status_code == 409 and "nur für" in r.json()["detail"][0]
    # Übersteuerung einer Gruppe auf anderes Konto
    r = st.post("/api/exports", json={"items": ["103"], "accounts": {str(a["id"]): b["id"]}}).json()
    assert r["exports"][0]["account"]["id"] == b["id"]
    st.post(f"/api/exports/{r['id']}/revert")

    # Konto mit Exporten wird beim Löschen nur deaktiviert, unbenutztes wird gelöscht
    assert st.delete(f"/api/accounts/{b['id']}").json()["result"] == "deactivated"
    tmp = st.post("/api/accounts", json={**ACC_B, "label": "Temp"}).json()
    assert st.delete(f"/api/accounts/{tmp['id']}").json()["result"] == "deleted"
    L = {x["doc_id"]: x for x in st.get("/api/invoices").json()}
    assert L[109]["account"]["id"] == a["id"]                           # Firma B inaktiv -> Standard


# ---------------------------------------------------------------- Liquiditätsvorschau

def test_timeline_units():
    from qr2pain.web.service import timeline
    today = date(2026, 9, 29)                                   # Dienstag, KW 40
    unit, p, end = timeline("30d", today, None)
    assert unit == "day" and len(p) == 30 and p[0][2] == "Di 29.09."
    unit, p, end = timeline("8w", today, None)
    assert unit == "week" and len(p) == 8 and p[0][0] == date(2026, 9, 28) and p[0][2] == "KW 40"
    unit, p, end = timeline("12m", today, None)
    assert unit == "month" and len(p) == 12 and p[0][2] == "Sep 26" and end == date(2027, 9, 1)
    # automatisch: bis zur letzten geplanten Zahlung
    assert timeline("auto", today, None)[0] == "day" and len(timeline("auto", today, None)[1]) == 14
    assert timeline("auto", today, today + timedelta(days=25))[0] == "day"
    unit, p, _ = timeline("auto", today, today + timedelta(days=60))
    assert unit == "week" and len(p) == 9
    unit, p, _ = timeline("auto", today, date(2027, 3, 15))
    assert unit == "month" and len(p) == 7
    assert len(timeline("auto", today, date(2031, 1, 1))[1]) == 24        # gedeckelt
    assert timeline("unsinn", today, None)[0] == "day"                    # unbekannt -> automatisch


def test_liquidity_horizon_scheduled_and_balance(web):
    st = web["client"]("stephan", "geheim")
    bh = web["client"]("buchhaltung", "geheim2")
    for h, unit, n in [("30d", "day", 30), ("8w", "week", 8), ("3m", "week", 13), ("6m", "month", 6)]:
        s = st.get(f"/api/stats?horizon={h}").json()
        assert s["timeline"]["unit"] == unit
        assert len([b for b in s["liquidity"] if b["key"] not in ("overdue", "later")]) == n, h
        assert s["liquidity"][0]["key"] == "overdue"

    def total(s, kind):
        return sum(Decimal_(b[kind].get("CHF", "0")) for b in s["liquidity"])

    before = st.get("/api/stats?horizon=12m").json()
    ex = st.post("/api/exports", json={"items": ["101"]}).json()
    after = st.get("/api/stats?horizon=12m").json()
    moved = total(before, "open") - total(after, "open")
    assert moved > 0 and total(after, "scheduled") - total(before, "scheduled") == moved   # exportiert = geplant
    st.post(f"/api/exports/{ex['id']}/revert")
    assert total(st.get("/api/stats?horizon=12m").json(), "scheduled") == total(before, "scheduled")

    # Kontostand: optional, jeder angemeldete Benutzer darf ihn erfassen
    acc = next(a for a in st.get("/api/accounts").json() if a["label"] == "Firma A CHF")
    assert st.get(f"/api/stats?account={acc['id']}").json()["balances"] == {}
    r = bh.put("/api/balances", json={"account_id": acc["id"], "currency": "CHF", "amount": "12'345.60"})
    assert r.status_code == 200, r.text
    b = st.get(f"/api/stats?account={acc['id']}").json()["balances"]["CHF"]
    assert b["amount"] == "12345.60" and b["by"] == "buchhaltung" and b["as_of"] == date.today().isoformat()
    assert b["items"] == {str(acc["id"]): "12345.60"}
    assert st.get("/api/stats").json()["balances"]["CHF"]["amount"] == "12345.60"          # Summe aller Konten
    for bad, code in [({"amount": "abc"}, 400), ({"amount": "NaN"}, 400), ({"currency": "EUR"}, 400),
                      ({"account_id": 9999}, 404)]:
        r = st.put("/api/balances", json={"account_id": acc["id"], "currency": "CHF", "amount": "1", **bad})
        assert r.status_code == code, (bad, r.text)
    assert st.put("/api/balances", json={"account_id": acc["id"], "currency": "CHF", "amount": ""}).status_code == 200
    assert st.get(f"/api/stats?account={acc['id']}").json()["balances"] == {}
    assert st.put("/api/balances", json={"account_id": acc["id"], "currency": "CHF"},
                  headers={"X-Requested-With": ""}).status_code == 403                    # CSRF



# ---------------------------------------------------------------- Verbuchungsart

def test_booking_mode(web):
    st = web["client"]("stephan", "geheim")
    acc = next(a for a in st.get("/api/accounts").json() if a["label"] == "Firma A CHF")
    assert acc["booking"] == "batch"                                          # Standard: Sammelbuchung
    assert st.patch(f"/api/accounts/{acc['id']}", json={"booking": "quer"}).status_code == 400
    assert st.patch(f"/api/accounts/{acc['id']}", json={"booking": "single"}).json()["booking"] == "single"

    def xml_of(body):
        r = st.post("/api/exports", json=body)
        assert r.status_code == 200, r.text
        x = r.json()["exports"][0]
        xml = st.get(f"/api/exports/{x['id']}/xml").text
        XSD.validate(xml)
        st.post(f"/api/exports/{x['id']}/revert")
        return x, xml

    x, xml = xml_of({"items": ["101"]})                                       # Vorgabe des Kontos
    assert x["booking"] == "single" and "<BtchBookg>false</BtchBookg>" in xml
    x, xml = xml_of({"items": ["101"], "booking": {str(acc["id"]): "batch"}})  # beim Export übersteuert
    assert "<BtchBookg>true</BtchBookg>" in xml
    x, xml = xml_of({"items": ["101"], "booking": {str(acc["id"]): "bank"}})   # Feld weglassen
    assert "BtchBookg" not in xml
    ex = next(e for e in st.get("/api/exports").json() if e["id"] == x["id"])
    assert ex["account"]["booking"] == "bank"
    r = st.post("/api/exports", json={"items": ["101"], "booking": {str(acc["id"]): "egal"}})
    assert r.status_code == 409
    st.patch(f"/api/accounts/{acc['id']}", json={"booking": "batch"})


# ---------------------------------------------------------------- Zahlbetrag / Zahlungsdatum zurückschreiben

def fields_of(web, doc_id):
    m = web["mock"]
    return {m.FIELDS[c["field"]]: c["value"] for c in m.DOCS[doc_id].get("custom_fields", [])}


def test_payment_fields_written_back(web):
    st = web["client"]("stephan", "geheim")
    # ganze Rechnung: bezahlter Betrag und Ausführungsdatum
    inv = st.get("/api/invoices/101").json()
    ex = st.post("/api/exports", json={"items": ["101"]}).json()
    f = fields_of(web, 101)
    assert f["Zahlbetrag"] == f"CHF{Decimal_(inv['effective']['amount']):.2f}"
    assert f["Zahlungsdatum"] == inv["effective"]["execution_date"]
    assert f["Fällig am"]                                              # übrige Felder bleiben erhalten
    st.post(f"/api/exports/{ex['id']}/revert")
    f = fields_of(web, 101)
    assert f["Zahlbetrag"] is None and f["Zahlungsdatum"] is None

    # Raten: offener Betrag, Datum erst nach der letzten Rate
    assert st.get("/api/invoices/108").json()["split"]
    e1 = st.post("/api/exports", json={"items": ["108:1"]}).json()
    f = fields_of(web, 108)
    assert f["Zahlbetrag"] == "CHF613.40" and f.get("Zahlungsdatum") is None
    e2 = st.post("/api/exports", json={"items": ["108:2"]}).json()
    f = fields_of(web, 108)
    assert f["Zahlbetrag"] == "CHF1213.40" and f["Zahlungsdatum"] == D(40)
    st.post(f"/api/exports/{e2['id']}/revert")
    f = fields_of(web, 108)
    assert f["Zahlbetrag"] == "CHF613.40" and f["Zahlungsdatum"] is None
    st.post(f"/api/exports/{e1['id']}/revert")
    assert fields_of(web, 108)["Zahlbetrag"] is None


def test_shared_amount_field(web):
    """Betragsfeld (Rechnung ohne QR-Betrag) ist zugleich das Rückschreibefeld: Ausgangsbetrag bleibt erhalten."""
    from qr2pain.web.app import engine
    old_web = dict(engine.cfg.get("web", {}))
    engine.cfg["web"]["paid_amount_field"] = "Betrag"          # amount_field = "Betrag" aus der Fixture
    try:
        st = web["client"]("stephan", "geheim")
        assert st.get("/api/invoices/103").json()["amount_source"] == "paperless-Feld"
        ok = st.patch("/api/invoices/103", json={"installments": [{"amount": "60", "date": D(5)},
                                                                   {"amount": "60", "date": D(35)}]}).json()
        assert not ok["errors"], ok["errors"]
        e1 = st.post("/api/exports", json={"items": ["103:1"]}).json()
        assert fields_of(web, 103)["Betrag"] == "CHF60.00"      # offener Rest
        sync(st)                                                 # darf den Rechnungsbetrag nicht verändern
        inv = st.get("/api/invoices/103").json()
        assert inv["effective"]["amount"] == "120.00" and not inv["errors"]
        e2 = st.post("/api/exports", json={"items": ["103:2"]}).json()
        assert fields_of(web, 103)["Betrag"] == "CHF120.00" and fields_of(web, 103)["Zahlungsdatum"] == D(35)
        st.post(f"/api/exports/{e2['id']}/revert")
        st.post(f"/api/exports/{e1['id']}/revert")
        f = fields_of(web, 103)
        assert f["Betrag"] == "CHF120.00" and f["Zahlungsdatum"] is None   # Ausgangsbetrag wiederhergestellt
        sync(st)
        assert st.get("/api/invoices/103").json()["effective"]["amount"] == "120.00"
        st.patch("/api/invoices/103", json={"installments": None})
    finally:
        engine.cfg["web"] = old_web



# ---------------------------------------------------------------- mehrere Einzahlungsscheine

def test_due_from_bill():
    from qr2pain.swissqr import Address, QRBill
    from qr2pain.web.service import due_from_bill
    b = QRBill("CH4431999123000889012", Address("S", "X", town="Y", country="CH"), None, "CHF", None, "NON", "")
    assert due_from_bill(replace_(b, bill_info="//S1/10/4711/11/260915/40/0:30")) == date(2026, 10, 15)
    assert due_from_bill(replace_(b, message="Rate 2, zahlbar bis 31.12.2026")) == date(2026, 12, 31)
    assert due_from_bill(replace_(b, message="Rechnung vom 01.09.2026, fällig am 30.09.26")) == date(2026, 9, 30)
    assert due_from_bill(replace_(b, message="Vertrag 01.01.2026 / 01.02.2026")) is None   # mehrdeutig
    assert due_from_bill(replace_(b, message="3. Rate")) is None


def replace_(b, **kw):
    from dataclasses import replace
    return replace(b, **kw)


def test_multi_qr_installments(web):
    st = web["client"]("stephan", "geheim")
    m = web["mock"]
    x = st.get("/api/invoices/111").json()
    assert x["qr_count"] == 3 and x["plan_auto"] and x["split"] and not x["errors"], x["errors"]
    assert [p["amount"] for p in x["parts"]] == ["400.00", "400.00", "400.50"]
    assert x["effective"]["amount"] == "1200.50"
    assert [p["reference"][-3:] for p in x["parts"]] == [m.qrr("501")[-3:], m.qrr("502")[-3:], m.qrr("503")[-3:]]
    assert [p["estimated"] for p in x["parts"]] == [False, False, True]
    assert x["parts"][0]["date"] <= m._d1.isoformat() and x["parts"][1]["date"] <= m._d2.isoformat()
    assert x["parts"][2]["date"] > x["parts"][1]["date"]
    assert any("geschätzt" in w for w in x["warnings"])
    assert "Ratenzahlung" in tag_names(web, 111)

    # erste Rate exportieren: eigene Referenz und Mitteilung, kein «Teilzahlung»-Präfix
    ex = st.post("/api/exports", json={"items": ["111:1"]}).json()
    xml = st.get(f"/api/exports/{ex['id']}/xml").text
    XSD.validate(xml)
    assert m.qrr("501") in xml and m.qrr("502") not in xml and "1. Rate, zahlbar bis" in xml
    assert "Teilzahlung" not in xml and "<InstdAmt Ccy=\"CHF\">400.00</InstdAmt>" in xml
    assert fields_of(web, 111)["Zahlbetrag"] == "CHF800.50"            # offener Rest
    x = st.get("/api/invoices/111").json()
    assert x["parts_done"] == 1 and x["status"] == "open"

    # Aufteilung anpassen: dritten Schein entfernen (z. B. bereits bezahlt), exportierte Rate bleibt
    plan = [{"amount": p["amount"], "date": p["date"], "qr": p["qr"]} for p in x["parts"][:2]]
    r = st.patch("/api/invoices/111", json={"installments": plan})
    assert r.status_code == 200, r.text
    x = r.json()
    assert not x["plan_auto"] and len(x["parts"]) == 2 and x["effective"]["amount"] == "800.00"
    assert x["parts"][1]["reference"] == m.qrr("502")
    # exportierte Rate darf nicht verändert werden
    bad = [{**plan[0], "amount": "1.00"}, plan[1]]
    assert st.patch("/api/invoices/111", json={"installments": bad}).status_code == 400

    # zweite Rate exportieren -> Rechnung erledigt
    ex2 = st.post("/api/exports", json={"items": ["111:2"]}).json()
    assert m.qrr("502") in st.get(f"/api/exports/{ex2['id']}/xml").text
    assert 111 not in ids(st)
    f = fields_of(web, 111)
    assert f["Zahlbetrag"] == "CHF800.00" and f["Zahlungsdatum"] == plan[1]["date"]

    # ein anderes Dokument mit demselben Einzahlungsschein wäre ein Duplikat (Historie pro Schein)
    from qr2pain.web.app import engine
    idx = engine.dup_index()
    from qr2pain.web.service import paid_key
    from decimal import Decimal
    assert idx[paid_key("CH4431999123000889012", m.qrr("502"), Decimal("400.00"))]

    st.post(f"/api/exports/{ex2['id']}/revert")
    st.post(f"/api/exports/{ex['id']}/revert")
    st.patch("/api/invoices/111", json={"installments": None})
    x = st.get("/api/invoices/111").json()
    assert x["plan_auto"] and len(x["parts"]) == 3 and x["parts_done"] == 0


def test_rescan_after_update(web):
    """Einträge aus älteren Versionen (nur ein QR-Code gelesen) werden beim nächsten Sync einmal neu gelesen."""
    from qr2pain.web.app import engine
    st = web["client"]("stephan", "geheim")
    engine.store.x("UPDATE invoices SET qr_extra='[]', scan_v=1 WHERE doc_id=111")
    assert st.get("/api/invoices/111").json()["qr_count"] == 1
    sync(st)
    x = st.get("/api/invoices/111").json()
    assert x["qr_count"] == 3 and x["plan_auto"]
    assert engine.store.one("SELECT MIN(scan_v) v FROM invoices WHERE status='open'")["v"] == 2
