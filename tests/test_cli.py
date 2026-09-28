"""Kommandozeile end-to-end: QR-Rechnungs-PDFs erzeugen, paperless-API simulieren, qr2pain laufen lassen,
Ergebnis gegen ISO-XSD pain.001.001.09 validieren."""
import io
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cairosvg
import xmlschema
from qrbill import QRBill

HERE = Path(__file__).parent
XSD = HERE / "xsd" / "pain.001.001.09.xsd"
sys.path.insert(0, str(HERE.parent))

from qr2pain.cli import main  # noqa: E402


def make_pdf(**kw) -> bytes:
    bill = QRBill(language="de", **kw)
    svg = io.StringIO()
    bill.as_svg(svg, full_page=True)
    return cairosvg.svg2pdf(bytestring=svg.getvalue().encode())


CRED = {"name": "Robert Schneider AG", "street": "Rue du Lac", "house_num": "1268",
        "pcode": "2501", "city": "Biel", "country": "CH"}
DOCS = {
    # QR-IBAN + QR-Referenz
    11: make_pdf(account="CH4431999123000889012", creditor=CRED, amount="1949.75",
                 reference_number="210000000003139471430009017",
                 additional_information="Auftrag vom 15.09.2026",
                 billing_information="//S1/10/10201409/11/260915/20/1400.000-53/30/106017086"),
    # IBAN + SCOR, EUR
    12: make_pdf(account="CH5800791123000889012", currency="EUR",
                 creditor={"name": "Müller & Söhne GmbH", "street": "Seestrasse", "house_num": "5",
                           "pcode": "6300", "city": "Zug", "country": "CH"},
                 amount="250.00", reference_number="RF18539007547034"),
    # IBAN ohne Referenz, ohne Betrag -> Betrag aus Custom Field
    13: make_pdf(account="CH5800791123000889012",
                 creditor={"name": "Verein Beispiel", "pcode": "6003", "city": "Luzern",
                           "street": "Pilatusstrasse", "house_num": "7", "country": "CH"},
                 additional_information="Mitgliederbeitrag 2027"),
    # Dokument ohne QR-Code
    14: cairosvg.svg2pdf(bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" width="595" height="842">'
                                     b'<text x="50" y="50">Keine QR-Rechnung</text></svg>'),
}
TAGS = {1: "QR zu zahlen"}
state = {"patches": {}, "notes": {}}


def doc_json(i):
    return {"id": i, "title": f"Rechnung {i}", "tags": [1, 99], "archive_serial_number": 1000 + i,
            "custom_fields": [{"field": 5, "value": "CHF120.00"}] if i == 13 else []}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Api-Version", "9")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        assert self.headers["Authorization"] == "Token secret"
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/":
            return self._json({})
        if u.path == "/api/tags/":
            hit = [{"id": k, "name": v} for k, v in TAGS.items() if v.lower() == q["name__iexact"].lower()]
            return self._json({"count": len(hit), "next": None, "results": hit})
        if u.path == "/api/custom_fields/":
            return self._json({"results": [{"id": 5, "name": "Betrag"}]})
        if u.path == "/api/documents/":
            assert q["tags__id__all"] == "1"
            excl = set(map(int, q.get("tags__id__none", "").split(","))) if q.get("tags__id__none") else set()
            ids = sorted(DOCS)
            page = int(q.get("page", 1))
            chunk = ids[(page - 1) * 2: page * 2]  # Paginierung testen (2 pro Seite)
            nxt = f"http://{self.headers['Host']}/api/documents/?page={page + 1}&tags__id__all=1&tags__id__none={','.join(map(str, sorted(excl)))}" \
                if page * 2 < len(ids) else None
            return self._json({"count": len(ids), "next": nxt, "results": [doc_json(i) for i in chunk]})
        m = re.fullmatch(r"/api/documents/(\d+)/download/", u.path)
        if m:
            assert q.get("original") == "true"
            b = DOCS[int(m.group(1))]
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self._json({"detail": "nf"}, 404)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/tags/":
            body = self._body()
            new_id = max(TAGS) + 1
            TAGS[new_id] = body["name"]
            return self._json({"id": new_id, "name": body["name"]}, 201)
        m = re.fullmatch(r"/api/documents/(\d+)/notes/", u.path)
        if m:
            state["notes"].setdefault(int(m.group(1)), []).append(self._body()["note"])
            return self._json([])
        self._json({}, 404)

    def do_PATCH(self):
        m = re.fullmatch(r"/api/documents/(\d+)/", urlparse(self.path).path)
        state["patches"][int(m.group(1))] = self._body()["tags"]
        self._json({})


def test_cli_export(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
    amount_field = "Betrag"
    [paperless]
    url = "http://127.0.0.1:{port}"
    token = "secret"
    [tags]
    pending = "QR zu zahlen"
    exported = "QR exportiert"
    error = "QR Fehler"
    [debtor]
    name = "Muster AG"
    iban = "CH93 0076 2011 6238 5295 7"
    street = "Bahnhofstrasse"
    building = "1"
    postal_code = "6000"
    town = "Luzern"
    """, encoding="utf-8")

    xml_path = tmp_path / "pain001_test.xml"
    rc = main(["-c", str(cfg), "-o", str(xml_path), "-d", "2026-09-29"])
    print("Exit-Code:", rc)

    xsd = xmlschema.XMLSchema(str(XSD))
    xsd.validate(str(xml_path))
    print("XSD-Validierung pain.001.001.09: OK")

    d = xsd.to_dict(str(xml_path))
    tx = [t for p in d["CstmrCdtTrfInitn"]["PmtInf"] for t in p["CdtTrfTxInf"]]
    assert len(tx) == 3, len(tx)
    assert d["CstmrCdtTrfInitn"]["GrpHdr"]["CtrlSum"] == 2319.75
    print("Tags gesetzt:", state["patches"])
    print("Notizen:", {k: v for k, v in state["notes"].items()})
    exp = next(k for k, v in TAGS.items() if v == "QR exportiert")
    err = next(k for k, v in TAGS.items() if v == "QR Fehler")
    for i in (11, 12, 13):
        assert exp in state["patches"][i] and 1 not in state["patches"][i]
    assert err in state["patches"][14] and 1 in state["patches"][14]
    assert rc == 1  # ein Dokument fehlerhaft
    srv.shutdown()
