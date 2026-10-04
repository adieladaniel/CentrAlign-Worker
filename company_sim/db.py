"""SQLite helpers (stdlib sqlite3, one short-lived connection per call)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from . import config

SCHEMA = """
CREATE TABLE vendors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    erp_code TEXT NOT NULL UNIQUE,
    address TEXT NOT NULL,
    email TEXT NOT NULL
);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL UNIQUE,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    tax_rate REAL NOT NULL,
    status TEXT NOT NULL,
    portal_visible INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE invoice_items (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    description TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE emails (
    id INTEGER PRIMARY KEY,
    folder TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    attachment_invoice TEXT,
    in_reply_to INTEGER,
    is_read INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    invoice_number TEXT NOT NULL COLLATE NOCASE,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    UNIQUE (vendor_id, invoice_number)
);
"""
TABLES = ("bills", "emails", "invoice_items", "invoices", "vendors")


def connect() -> sqlite3.Connection:
    """Open a connection with dict-like rows."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Connection that commits on success, rolls back on error, always closes."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    with transaction() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: Sequence[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run a write statement and return lastrowid."""
    with transaction() as conn:
        return conn.execute(sql, params).lastrowid


def recreate_schema(conn: sqlite3.Connection) -> None:
    """Drop and recreate all tables."""
    for table in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)


def is_seeded() -> bool:
    if not config.DB_PATH.exists():
        return False
    row = query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='bills'")
    return row is not None
