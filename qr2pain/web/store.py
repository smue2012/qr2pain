"""SQLite-Datenhaltung: Rechnungen (mit Korrekturen), Exporte, Protokoll."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    doc_id        INTEGER PRIMARY KEY,
    title         TEXT,
    correspondent TEXT,
    created       TEXT,            -- Dokumentdatum (YYYY-MM-DD)
    modified      TEXT,            -- paperless-Änderungszeitpunkt (für Sync)
    asn           INTEGER,
    due_date      TEXT,            -- aus paperless-Datumsfeld
    pl_amount     TEXT,            -- Betrag aus paperless-Monetary-Feld
    qr_raw        TEXT,            -- Original-Payload des Swiss QR Codes
    scan_error    TEXT,            -- QR-Code nicht lesbar
    overrides     TEXT NOT NULL DEFAULT '{}',
    held          INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open',   -- open | exported
    export_id     INTEGER,
    synced_at     TEXT
);
CREATE TABLE IF NOT EXISTS exports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id      TEXT,
    created_at  TEXT,
    created_by  TEXT,
    filename    TEXT,
    xml         BLOB,
    reverted_at TEXT,
    reverted_by TEXT
);
CREATE TABLE IF NOT EXISTS export_items (
    export_id  INTEGER REFERENCES exports(id),
    doc_id     INTEGER,
    creditor   TEXT,
    iban       TEXT,
    reference  TEXT,
    currency   TEXT,
    amount     TEXT,
    exec_date  TEXT,
    part       INTEGER NOT NULL DEFAULT 0   -- 0 = ganze Rechnung, 1..n = Rate
);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    user    TEXT,
    doc_id  INTEGER,
    action  TEXT,
    detail  TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,             -- Bezeichnung, z. B. "Firma B – CHF"
    name        TEXT NOT NULL,             -- Kontoinhaber (Debtor)
    iban        TEXT NOT NULL,
    bic         TEXT NOT NULL DEFAULT '',
    street      TEXT NOT NULL DEFAULT '',
    building    TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    town        TEXT NOT NULL DEFAULT '',
    country     TEXT NOT NULL DEFAULT 'CH',
    currency    TEXT NOT NULL DEFAULT '',  -- '' = alle Währungen
    rules       TEXT NOT NULL DEFAULT '{}',-- {"tags": [...], "correspondents": [...], "storage_paths": [...]}
    is_default  INTEGER NOT NULL DEFAULT 0,
    sort        INTEGER NOT NULL DEFAULT 100,
    active      INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS balances (
    account_id  INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    amount      TEXT NOT NULL,
    as_of       TEXT NOT NULL,              -- Datum der Erfassung
    updated_by  TEXT,
    PRIMARY KEY (account_id, currency)
);
CREATE INDEX IF NOT EXISTS ix_items_doc ON export_items(doc_id);
CREATE INDEX IF NOT EXISTS ix_audit_doc ON audit(doc_id);
"""


def now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.gen = 0          # Änderungszähler: jede Schreiboperation erhöht ihn (für Caches)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(export_items)").fetchall()}
        if "part" not in cols:  # Version 1.0 -> Raten
            self.db.execute("ALTER TABLE export_items ADD COLUMN part INTEGER NOT NULL DEFAULT 0")
        # Mehrere Belastungskonten (Datenbanken aus der Entwicklungszeit nachrüsten)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(exports)").fetchall()}
        if "account_id" not in cols:
            self.db.execute("ALTER TABLE exports ADD COLUMN account_id INTEGER")
            self.db.execute("ALTER TABLE exports ADD COLUMN debtor TEXT")   # Kontodaten zum Zeitpunkt des Exports
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(invoices)").fetchall()}
        if "tags" not in cols:
            self.db.execute("ALTER TABLE invoices ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
            self.db.execute("ALTER TABLE invoices ADD COLUMN storage_path TEXT")

    # ------------------------------------------------------------ generisch
    def q(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def x(self, sql: str, *args) -> int:
        with self.lock:
            self.gen += 1
            return self.db.execute(sql, args).lastrowid

    def tx(self):
        """Kontextmanager für eine Transaktion."""
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                store.db.execute("BEGIN")
                return store

            def __exit__(self, exc, *_):
                store.db.execute("ROLLBACK" if exc else "COMMIT")
                store.gen += 1
                store.lock.release()

        return _Tx()

    # ------------------------------------------------------------ Rechnungen
    # ------------------------------------------------------------ Konten
    def accounts(self, active_only: bool = False) -> list[dict]:
        rows = self.q("SELECT * FROM accounts" + (" WHERE active=1" if active_only else "") + " ORDER BY sort, id")
        for r in rows:
            r["rules"] = json.loads(r["rules"] or "{}")
        return rows

    def invoice(self, doc_id: int) -> dict | None:
        r = self.one("SELECT * FROM invoices WHERE doc_id=?", doc_id)
        if r:
            r["overrides"] = json.loads(r["overrides"] or "{}")
            r["tags"] = json.loads(r.get("tags") or "[]")
        return r

    def invoices(self, where: str = "1=1", *args) -> list[dict]:
        rows = self.q(f"SELECT * FROM invoices WHERE {where} ORDER BY COALESCE(due_date, created), doc_id", *args)
        for r in rows:
            r["overrides"] = json.loads(r["overrides"] or "{}")
            r["tags"] = json.loads(r.get("tags") or "[]")
        return rows

    def upsert_invoice(self, doc_id: int, **fields) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        upd = ", ".join(f"{k}=excluded.{k}" for k in fields)
        self.x(f"INSERT INTO invoices (doc_id, {cols}) VALUES (?, {marks}) "
               f"ON CONFLICT(doc_id) DO UPDATE SET {upd}", doc_id, *fields.values())

    def set_overrides(self, doc_id: int, ov: dict) -> None:
        self.x("UPDATE invoices SET overrides=? WHERE doc_id=?", json.dumps(ov), doc_id)

    def audit(self, user: str, doc_id: int | None, action: str, detail: str = "") -> None:
        self.x("INSERT INTO audit (ts, user, doc_id, action, detail) VALUES (?,?,?,?,?)",
               now(), user, doc_id, action, detail)
