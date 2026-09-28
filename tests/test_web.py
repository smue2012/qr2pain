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
