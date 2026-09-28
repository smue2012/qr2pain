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
    assert sync(st)["new"] == 5
    assert ids(st) == list(range(101, 111))
    # erneuter Sync mit weniger Rechten entfernt nichts
    s = sync(bh)
    assert s["removed"] == 0 and s.get("hidden") == 5
    assert len(ids(st)) == 10
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

    ex = st.post("/api/exports", json={"items": ["108:1", "105", "102"]}).json()
    xml = st.get(f"/api/exports/{ex['id']}/xml").text
    XSD.validate(xml)
    assert "PL108-ASN2108-T1" in xml and "Teilzahlung 1/2" in xml

    L = {x["doc_id"]: x for x in st.get("/api/invoices").json()}
    assert 105 not in L and 102 not in L and L[108]["parts_done"] == 1
    assert any("bereits bezahlt" in e for e in L[106]["errors"])        # Duplikat gegen Historie
    assert st.patch("/api/invoices/108", json={"amount": "1"}).status_code == 400   # gesperrt
    assert "QR exportiert" in tag_names(web, 102) and "QR zu zahlen" in tag_names(web, 108)

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
