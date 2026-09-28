"""FastAPI-Webfrontend für qr2pain.

Start:  QR2PAIN_CONFIG=/etc/qr2pain/config.toml uvicorn qr2pain.web.app:app --host 127.0.0.1 --port 8010
(nur EIN Worker – Sessions und Sync-Status liegen im Speicher)
"""
from __future__ import annotations

import io
import os
import secrets
import threading
import time
import tomllib
from collections import OrderedDict
from pathlib import Path

import pypdfium2 as pdfium
import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..paperless import Paperless, PaperlessError, obtain_token
from ..qrscan import PDFIUM_LOCK
from .service import Engine
from .store import Store

STATIC = Path(__file__).parent / "static"
COOKIE = "qr2pain_session"


def load_config() -> dict:
    path = Path(os.environ.get("QR2PAIN_CONFIG", "config.toml"))
    return tomllib.loads(path.read_text(encoding="utf-8"))


CFG = load_config()
WEB = CFG.get("web", {})
PCFG = CFG["paperless"]
store = Store(Path(WEB.get("data_dir", os.environ.get("QR2PAIN_DATA", "data"))) / "qr2pain.sqlite")
engine = Engine(CFG, store)
SESSION_SECONDS = int(float(WEB.get("session_hours", 8)) * 3600)
ALLOWED = {u.lower() for u in WEB.get("allowed_users", [])}
sessions: dict[str, dict] = {}

app = FastAPI(title="qr2pain", docs_url=None, redoc_url=None, openapi_url=None,
              root_path=WEB.get("root_path", ""))
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ============================================================ Sicherheit

@app.middleware("http")
async def security_headers(request: Request, call_next):
    # CSRF-Schutz: schreibende API-Aufrufe nur mit eigenem Header (setzt kein fremdes Formular)
    if request.method in ("POST", "PATCH", "DELETE") and request.url.path.startswith("/api/") \
            and request.headers.get("X-Requested-With") != "qr2pain":
        return JSONResponse({"detail": "CSRF-Prüfung fehlgeschlagen"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "frame-src 'self'; object-src 'self'; frame-ancestors 'self'")
    return resp


class Session:
    def __init__(self, sid: str, data: dict):
        self.sid, self.user, self.token = sid, data["user"], data["token"]

    def paperless(self) -> Paperless:
        return Paperless(PCFG["url"], self.token, PCFG.get("verify_tls", True),
                         api_version=PCFG.get("api_version") or sessions[self.sid].get("api"))


def session(request: Request) -> Session:
    sid = request.cookies.get(COOKIE)
    data = sessions.get(sid or "")
    if not data or data["exp"] < time.time():
        sessions.pop(sid or "", None)
        raise HTTPException(401, "Nicht angemeldet")
    data["exp"] = time.time() + SESSION_SECONDS  # gleitender Ablauf
    return Session(sid, data)


# ============================================================ Zugriff nach paperless-Rechten

ACCESS_TTL = int(WEB.get("access_cache_seconds", 60))


class Access:
    """Merkt sich pro Session, welche Dokumente der Benutzer in paperless sehen darf.

    Geprüft werden nur Dokumente, die qr2pain kennt, und nur neue IDs seit der letzten
    Prüfung; nach ACCESS_TTL Sekunden wird komplett neu geprüft (Rechte können sich ändern)."""

    def __init__(self):
        self.cache: dict[str, dict] = {}
        self.lock = threading.Lock()

    def visible(self, s: "Session") -> set[int]:
        known = {r["doc_id"] for r in store.q("SELECT doc_id FROM invoices UNION SELECT doc_id FROM export_items")}
        with self.lock:
            c = self.cache.get(s.sid)
            if not c or c["exp"] < time.time():
                c = {"exp": time.time() + ACCESS_TTL, "checked": set(), "visible": set()}
                self.cache[s.sid] = c
            new = sorted(known - c["checked"])
            if new:
                try:
                    c["visible"] |= s.paperless().visible_ids(new)
                except requests.RequestException as e:
                    raise HTTPException(502, f"paperless nicht erreichbar – Berechtigungen nicht prüfbar ({e})") from e
                c["checked"] |= set(new)
            return c["visible"] & known

    def require(self, s: "Session", doc_ids) -> set[int]:
        vis = self.visible(s)
        if any(int(d) not in vis for d in doc_ids):
            raise HTTPException(404, "Nicht gefunden oder keine Berechtigung in paperless")
        return vis

    def drop(self, sid: str) -> None:
        with self.lock:
            self.cache.pop(sid, None)


access = Access()


def _err(e: Exception, code: int = 400):
    detail = e.args[0] if e.args and isinstance(e.args[0], list) else [str(e)]
    raise HTTPException(code, detail)


# ============================================================ Login

class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    if ALLOWED and body.username.lower() not in ALLOWED:
        raise HTTPException(403, "Kein Zugriff auf qr2pain für diesen Benutzer")
    try:
        token = obtain_token(PCFG["url"], body.username, body.password, PCFG.get("verify_tls", True))
        pl = Paperless(PCFG["url"], token, PCFG.get("verify_tls", True))
    except PaperlessError as e:
        raise HTTPException(401, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"paperless nicht erreichbar: {e}") from e
    sid = secrets.token_urlsafe(32)
    sessions[sid] = {"user": body.username, "token": token, "api": pl.api_version,
                     "exp": time.time() + SESSION_SECONDS}
    # "auto": Secure-Flag nur bei HTTPS (direkt oder via Reverse Proxy mit X-Forwarded-Proto)
    sec = WEB.get("secure_cookie", "auto")
    secure = request.url.scheme == "https" if sec == "auto" else bool(sec)
    response.set_cookie(COOKIE, sid, httponly=True, samesite="strict",
                        secure=secure, max_age=SESSION_SECONDS,
                        path=WEB.get("root_path", "") or "/")
    store.audit(body.username, None, "Anmeldung")
    return {"user": body.username}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    sid = request.cookies.get(COOKIE, "")
    sessions.pop(sid, None)
    access.drop(sid)
    response.delete_cookie(COOKIE, path=WEB.get("root_path", "") or "/")
    return {}


@app.get("/api/me")
def me(s: Session = Depends(session)):
    pub = PCFG.get("public_url", PCFG["url"]).rstrip("/")
    return {"user": s.user, "paperless_url": pub,
            "debtor": {"name": CFG["debtor"]["name"], "iban": CFG["debtor"]["iban"]}}


# ============================================================ Rechnungen

def public(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")}


@app.get("/api/invoices")
def invoices(status: str = "open", s: Session = Depends(session)):
    if status not in ("open", "exported", "all"):
        raise HTTPException(400, "status ungültig")
    return [public(e) for e in engine.list(None if status == "all" else status, access.visible(s))]


@app.get("/api/invoices/{doc_id}")
def invoice(doc_id: int, s: Session = Depends(session)):
    access.require(s, [doc_id])
    e = engine.get(doc_id)
    if not e:
        raise HTTPException(404, "Nicht gefunden")
    out = public(e)
    out["audit"] = store.q("SELECT ts, user, action, detail FROM audit WHERE doc_id=? ORDER BY id DESC", doc_id)
    return out


@app.patch("/api/invoices/{doc_id}")
def patch_invoice(doc_id: int, changes: dict, s: Session = Depends(session)):
    access.require(s, [doc_id])
    try:
        return public(engine.update_overrides(s.user, doc_id, changes, s.paperless()))
    except KeyError:
        raise HTTPException(404, "Nicht gefunden")
    except ValueError as e:
        _err(e)


class Hold(BaseModel):
    ids: list[int]
    held: bool


@app.post("/api/invoices/hold")
def hold(body: Hold, s: Session = Depends(session)):
    access.require(s, body.ids)
    engine.set_held(s.user, body.ids, body.held)
    return {}


_pdf_cache: dict[tuple[str, int], tuple[float, bytes, str]] = {}
_pdf_lock = threading.Lock()


def _document(s: Session, doc_id: int) -> tuple[bytes, str]:
    """Archiv-PDF über den Token des Benutzers holen (paperless-Rechte gelten), 5 Min. gecacht."""
    access.require(s, [doc_id])
    key = (s.user, doc_id)
    with _pdf_lock:
        hit = _pdf_cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1], hit[2]
    try:
        data, mime = s.paperless().preview(doc_id)
    except Exception as e:  # noqa: BLE001 – z.B. keine Berechtigung in paperless
        raise HTTPException(502, f"Vorschau nicht verfügbar: {e}") from e
    with _pdf_lock:
        if len(_pdf_cache) > 30:
            _pdf_cache.clear()
        _pdf_cache[key] = (time.time() + 300, data, mime)
    return data, mime


@app.get("/api/invoices/{doc_id}/pdf")
def pdf(doc_id: int, s: Session = Depends(session)):
    data, mime = _document(s, doc_id)
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=300",
                                                    "Content-Disposition": f'inline; filename="{doc_id}.pdf"'})


@app.get("/api/invoices/{doc_id}/pages")
def pages(doc_id: int, s: Session = Depends(session)):
    data, mime = _document(s, doc_id)
    if "pdf" not in mime and data[:5] != b"%PDF-":
        return {"pages": 1, "pdf": False}
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(data)
        try:
            return {"pages": len(doc), "pdf": True}
        finally:
            doc.close()


_png_cache: "OrderedDict[tuple[int, int, int], bytes]" = OrderedDict()   # (doc, seite, pdf-größe) -> PNG


@app.get("/api/invoices/{doc_id}/page/{n}.png")
def page_png(doc_id: int, n: int, s: Session = Depends(session)):
    data, mime = _document(s, doc_id)          # prüft auch die Berechtigung
    if "pdf" not in mime and data[:5] != b"%PDF-":
        return Response(data, media_type=mime)
    key = (doc_id, n, len(data))
    png = _png_cache.get(key)
    if png is None:
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument(data)
            try:
                if not 1 <= n <= len(doc):
                    raise HTTPException(404, "Seite nicht vorhanden")
                img = doc[n - 1].render(scale=1.6).to_pil()
            finally:
                doc.close()
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
        png = buf.getvalue()
        _png_cache[key] = png
        while len(_png_cache) > 100:
            _png_cache.popitem(last=False)
    else:
        _png_cache.move_to_end(key)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


# ============================================================ Sync

@app.post("/api/sync")
def sync(s: Session = Depends(session)):
    started = engine.start_sync(s.paperless(), s.user)
    return {"started": started, **engine.sync_state}


@app.get("/api/sync")
def sync_status(s: Session = Depends(session)):
    return engine.sync_state


# ============================================================ Exporte

class ExportReq(BaseModel):
    ids: list[int | str] = []     # 101 = ganze Rechnung, "101:2" = Rate 2
    items: list[str] = []


@app.post("/api/exports")
def create_export(body: ExportReq, s: Session = Depends(session)):
    access.require(s, {str(i).partition(":")[0] for i in [*body.ids, *body.items]})
    try:
        return engine.export(s.paperless(), s.user, [*body.ids, *body.items])
    except ValueError as e:
        _err(e, 409)


@app.get("/api/exports")
def exports(s: Session = Depends(session)):
    """Nur Exporte mit mindestens einer sichtbaren Position; fremde Positionen werden ausgeblendet."""
    vis = access.visible(s)
    items: dict[int, list] = {}
    for it in store.q("SELECT export_id, doc_id, part, creditor, currency, amount, exec_date, reference "
                      "FROM export_items ORDER BY rowid"):
        items.setdefault(it.pop("export_id"), []).append(it)
    out = []
    for r in store.q("SELECT id, msg_id, created_at, created_by, filename, reverted_at, reverted_by "
                     "FROM exports ORDER BY id DESC"):
        its = items.get(r["id"], [])
        mine = [i for i in its if i["doc_id"] in vis]
        if not mine:
            continue
        r["items"] = mine
        r["hidden"] = len(its) - len(mine)      # Positionen ohne Berechtigung
        out.append(r)
    return out


def _require_export(s: Session, export_id: int) -> None:
    ids = [r["doc_id"] for r in store.q("SELECT doc_id FROM export_items WHERE export_id=?", export_id)]
    if not ids:
        raise HTTPException(404, "Nicht gefunden")
    access.require(s, ids)


@app.get("/api/exports/{export_id}/xml")
def export_xml(export_id: int, s: Session = Depends(session)):
    _require_export(s, export_id)
    r = store.one("SELECT filename, xml FROM exports WHERE id=?", export_id)
    if not r:
        raise HTTPException(404, "Nicht gefunden")
    store.audit(s.user, None, "Download", f"Export #{export_id}")
    return Response(r["xml"], media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{r["filename"]}"'})


@app.post("/api/exports/{export_id}/revert")
def revert_export(export_id: int, s: Session = Depends(session)):
    _require_export(s, export_id)
    try:
        return engine.revert(s.paperless(), s.user, export_id)
    except KeyError:
        raise HTTPException(404, "Nicht gefunden")
    except ValueError as e:
        _err(e, 409)


# ============================================================ Auswertungen

@app.get("/api/stats")
def stats(s: Session = Depends(session)):
    return engine.stats(access.visible(s))


# ============================================================ Betrieb

@app.get("/api/health")
def health():
    """Für Docker-Healthcheck/Monitoring: ohne Anmeldung, ohne Daten."""
    store.one("SELECT 1 AS ok")
    return {"status": "ok", "version": __version__}


# ============================================================ Frontend

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
