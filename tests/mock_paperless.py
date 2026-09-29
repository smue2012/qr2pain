"""Simulierter paperless-ngx-Server für Tests (python tests/mock_paperless.py 8765)."""
import io
import json
import os
import time
import re
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cairosvg
from qrbill import QRBill

T = date.today()


def make_pdf(**kw) -> bytes:
    bill = QRBill(language="de", **kw)
    svg = io.StringIO()
    bill.as_svg(svg, full_page=True)
    return cairosvg.svg2pdf(bytestring=svg.getvalue().encode())


def qrr(base: str) -> str:
    t, c = [0, 9, 4, 6, 8, 2, 7, 1, 3, 5], 0
    base = base.zfill(26)
    for ch in base:
        c = t[(c + int(ch)) % 10]
    return base + str((10 - c) % 10)


def cred(name, street, nr, pcode, city):
    return {"name": name, "street": street, "house_num": nr, "pcode": pcode, "city": city, "country": "CH"}


# id: (Titel, Korrespondent, Fällig in Tagen, PDF-Parameter | None, Betrag im Zusatzfeld)
SPEC = {
    101: ("Rechnung 2026-4471", 1, 12, dict(account="CH4431999123000889012", amount="1949.75",
          creditor=cred("Robert Schneider AG", "Rue du Lac", "1268", "2501", "Biel"),
          reference_number="210000000003139471430009017", additional_information="Auftrag vom 15.09.2026",
          billing_information="//S1/10/10201409/11/260915/20/1400.000-53/30/106017086"), None),
    102: ("Lizenzverlängerung", 2, 3, dict(account="CH5800791123000889012", currency="EUR", amount="250.00",
          creditor=cred("Müller & Söhne GmbH", "Seestrasse", "5", "6300", "Zug"),
          reference_number="RF18539007547034"), None),
    103: ("Mitgliederbeitrag 2027", 3, 25, dict(account="CH5800791123000889012",
          creditor=cred("Verein Beispiel", "Pilatusstrasse", "7", "6003", "Luzern"),
          additional_information="Mitgliederbeitrag 2027"), "CHF120.00"),
    104: ("Stromrechnung Q3", 4, -4, dict(account="CH4431999123000889012", amount="842.30",
          creditor=cred("CKW AG", "Täschmattstrasse", "4", "6015", "Luzern"),
          reference_number=qrr("412")), None),
    105: ("Hosting Oktober", 5, 8, dict(account="CH5800791123000889012", amount="389.00",
          creditor=cred("Hostpoint AG", "Neue Jonastrasse", "60", "8640", "Rapperswil"),
          additional_information="Kundennr. 88231 / Okt"), None),
    106: ("Hosting Oktober (Kopie)", 5, 8, dict(account="CH5800791123000889012", amount="389.00",
          creditor=cred("Hostpoint AG", "Neue Jonastrasse", "60", "8640", "Rapperswil"),
          additional_information="Kundennr. 88231 / Okt"), None),
    107: ("Scan unleserlich", None, 15, None, None),
    108: ("Büromaterial", 6, 30, dict(account="CH4431999123000889012", amount="1213.40",
          creditor=cred("Lyreco Switzerland AG", "Industriestrasse", "22", "8305", "Dietlikon"),
          reference_number=qrr("123")), None),
    109: ("Telefonie 3CX", 7, 20, dict(account="CH5800791123000889012", amount="96.90",
          creditor=cred("Peoplefone AG", "Seestrasse", "92", "8942", "Oberrieden"),
          reference_number="RF18539007547034"), None),
    110: ("Leasing Drucker", 8, 45, dict(account="CH4431999123000889012", amount="318.60",
          creditor=cred("Grenke AG", "Ruessenstrasse", "6", "6340", "Baar"),
          reference_number=qrr("221")), None),
}
DOCS = {}
for i, (title, c, due, kw, amt) in SPEC.items():
    DOCS[i] = {
        "id": i, "title": title, "correspondent": c, "tags": [1] + ([20] if i == 109 else []),
        "storage_path": 1 if i == 110 else None,
        "created": (T - timedelta(days=30 - (due if due > 0 else 0) // 2)).isoformat(),
        "modified": f"{T.isoformat()}T08:00:00+02:00", "archive_serial_number": 2000 + i,
        "custom_fields": [{"field": 11, "value": (T + timedelta(days=due)).isoformat()}]
        + ([{"field": 12, "value": amt}] if amt else []),
        "_pdf": make_pdf(**kw) if kw else cairosvg.svg2pdf(
            bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" width="595" height="842">'
                       b'<text x="60" y="80" font-size="20">Rechnung (Scan ohne QR-Code)</text></svg>'),
    }
TAGS = {1: "QR zu zahlen", 20: "Firma B"}
TAG_OWNERS: dict = {}
STORAGE_PATHS = {1: "Firma B/Rechnungen"}
CORR = {1: "Robert Schneider AG", 2: "Müller & Söhne", 3: "Verein Beispiel", 4: "CKW", 5: "Hostpoint",
        6: "Lyreco", 7: "Peoplefone", 8: "Grenke"}
FIELDS = {11: "Fällig am", 12: "Betrag", 13: "Zahlbetrag", 14: "Zahlungsdatum"}
FIELD_TYPES = {11: "date", 12: "monetary", 13: "monetary", 14: "date"}
USERS = {"stephan": ("geheim", "tok-stephan"), "buchhaltung": ("geheim2", "tok-bh")}
SUPERUSERS = {"tok-stephan"}
# Benutzer "buchhaltung" sieht nur diese Dokumente (Objektrechte wie in paperless)
VISIBLE = {"tok-bh": {101, 102, 103, 104, 105}}


def can_see(tok, doc_id):
    return tok not in VISIBLE or doc_id in VISIBLE[tok]
LOG = {"patch": [], "notes": []}


def pub(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Api-Version", "9")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        tok = (self.headers.get("Authorization") or "").removeprefix("Token ")
        if tok not in {t for _, t in USERS.values()}:
            self._send(401, {"detail": "Invalid token"})
            return False
        self.tok = tok
        return True

    def _body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if "json" in (self.headers.get("Content-Type") or ""):
            return json.loads(raw or b"{}")
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    def _list(self, items):
        return self._send(200, {"count": len(items), "next": None, "results": items})

    def do_GET(self):
        if not self._auth():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/api/":
            return self._send(200, {})
        if p == "/api/tags/":
            n = q.get("name__iexact", "").lower()
            return self._list([{"id": k, "name": v} for k, v in TAGS.items() if not n or v.lower() == n])
        if p == "/api/custom_fields/":
            n = q.get("name__iexact", "").lower()
            return self._list([{"id": k, "name": v, "data_type": FIELD_TYPES[k]}
                               for k, v in FIELDS.items() if not n or v.lower() == n])
        if p == "/api/storage_paths/":
            return self._list([{"id": k, "name": v} for k, v in STORAGE_PATHS.items()])
        if p == "/api/correspondents/":
            return self._list([{"id": k, "name": v} for k, v in CORR.items()])
        if p == "/api/profile/":
            return self._send(200, {"username": "stephan"})
        if p == "/api/ui_settings/":
            return self._send(200, {"user": {"is_superuser": self.tok in SUPERUSERS}})
        if p == "/api/documents/":
            LOG.setdefault("doclist", 0)
            LOG["doclist"] += 1
            inc = set(map(int, q.get("tags__id__all", "").split(","))) if q.get("tags__id__all") else set()
            exc = set(map(int, q.get("tags__id__none", "").split(","))) if q.get("tags__id__none") else set()
            ids = set(map(int, q["id__in"].split(","))) if q.get("id__in") else None
            res = [pub(d) for d in DOCS.values() if inc <= set(d["tags"]) and not exc & set(d["tags"])
                   and can_see(self.tok, d["id"]) and (ids is None or d["id"] in ids)]
            if q.get("fields") == "id":
                res = [{"id": d["id"]} for d in res]
            return self._send(200, {"count": len(res), "next": None, "all": [d["id"] for d in res],
                                    "results": res[:int(q.get("page_size", 25))]})
        m = re.fullmatch(r"/api/documents/(\d+)/(download|preview)?/?", p)
        if m and int(m.group(1)) in DOCS and can_see(self.tok, int(m.group(1))):
            d = DOCS[int(m.group(1))]
            if m.group(2):
                time.sleep(float(os.environ.get("MOCK_DELAY", "0")))   # Netzwerk-/Speicherlatenz simulieren
                return self._send(200, d["_pdf"], "application/pdf")
            return self._send(200, pub(d))
        self._send(404, {"detail": "Not found"})

    def do_POST(self):
        u = urlparse(self.path).path
        if u == "/api/token/":
            b = self._body()
            user = USERS.get(b.get("username"))
            if not user or user[0] != b.get("password"):
                return self._send(400, {"non_field_errors": ["Unable to log in"]})
            return self._send(200, {"token": user[1]})
        if not self._auth():
            return
        if u == "/api/tags/":
            b = self._body()
            i = max(TAGS) + 1
            TAGS[i] = b["name"]
            TAG_OWNERS[i] = b["owner"] if "owner" in b else self.tok   # wie paperless: sonst Ersteller
            return self._send(201, {"id": i, "name": b["name"], "owner": TAG_OWNERS[i]})
        m = re.fullmatch(r"/api/documents/(\d+)/notes/", u)
        if m and not can_see(self.tok, int(m.group(1))):
            return self._send(404, {"detail": "Not found"})
        if m:
            LOG["notes"].append((int(m.group(1)), self._body()["note"]))
            return self._send(200, [])
        self._send(404, {})

    def do_PATCH(self):
        if not self._auth():
            return
        m = re.fullmatch(r"/api/documents/(\d+)/", urlparse(self.path).path)
        if not can_see(self.tok, int(m.group(1))):
            return self._send(404, {"detail": "Not found"})
        b = self._body()
        if "tags" in b:
            DOCS[int(m.group(1))]["tags"] = b["tags"]
            LOG["patch"].append((int(m.group(1)), b["tags"]))
        if "custom_fields" in b:
            DOCS[int(m.group(1))]["custom_fields"] = b["custom_fields"]
        self._send(200, pub(DOCS[int(m.group(1))]))


def serve(port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    s = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print("mock paperless on", s.server_address, flush=True)
    threading.Event().wait()
