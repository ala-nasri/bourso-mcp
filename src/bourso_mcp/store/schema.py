"""Connexion SQLite et schéma (spec §5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    type TEXT NOT NULL,
    ownership TEXT NOT NULL,
    parent_id TEXT,
    iban TEXT,
    currency TEXT NOT NULL,
    balance_cents INTEGER,
    balance_at TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    fingerprint TEXT NOT NULL,
    operation_date TEXT NOT NULL,
    debit_date TEXT NOT NULL,
    value_date TEXT,
    raw_label TEXT NOT NULL,
    label TEXT NOT NULL,
    search_text TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    type TEXT NOT NULL,
    category TEXT,
    card TEXT,
    pending INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_account_operation ON transactions(account_id, operation_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_account_fingerprint ON transactions(account_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_tx_amount ON transactions(amount_cents);
CREATE TABLE IF NOT EXISTS statements (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    period TEXT NOT NULL,
    date TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    downloaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    twofa_method TEXT,
    added_transactions INTEGER NOT NULL DEFAULT 0,
    added_statements INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error_message TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()
