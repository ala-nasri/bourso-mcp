"""Accès au store. Une connexion par appel (sûr entre threads)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from bourso_mcp.source.models import Account, StatementRef, Transaction
from bourso_mcp.store.fingerprint import compute_fingerprints
from bourso_mcp.store.schema import connect, init_db
from bourso_mcp.store.text import normalize_text

Trigger = Literal["mcp", "cli", "schedule"]
Scope = Literal["all", "transactions", "statements"]
RunStatus = Literal[
    "running", "awaiting_2fa", "ok", "partial", "auth_required", "auth_failed", "error"
]

_TX_INSERT = """
INSERT INTO transactions (account_id, fingerprint, operation_date, debit_date, value_date,
    raw_label, label, search_text, amount_cents, type, category, card, pending, synced_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class RunRow:
    id: int
    started_at: datetime
    finished_at: datetime | None
    trigger: Trigger
    scope: Scope
    status: RunStatus
    twofa_method: str | None
    added_transactions: int
    added_statements: int
    error_type: str | None
    error_message: str | None


def _iso(d: date | datetime | None) -> str | None:
    return None if d is None else d.isoformat()


def _run_from_row(r: sqlite3.Row) -> RunRow:
    return RunRow(
        id=r["id"],
        started_at=datetime.fromisoformat(r["started_at"]),
        finished_at=datetime.fromisoformat(r["finished_at"]) if r["finished_at"] else None,
        trigger=r["trigger"],
        scope=r["scope"],
        status=r["status"],
        twofa_method=r["twofa_method"],
        added_transactions=r["added_transactions"],
        added_statements=r["added_statements"],
        error_type=r["error_type"],
        error_message=r["error_message"],
    )


def _search_text(tx: Transaction) -> str:
    return normalize_text(tx.raw_label + " " + tx.label)


def _tx_params(account_id: str, fp: str, tx: Transaction, pending: int, synced_at: datetime):
    return (
        account_id,
        fp,
        _iso(tx.operation_date),
        _iso(tx.debit_date),
        _iso(tx.value_date),
        tx.raw_label,
        tx.label,
        _search_text(tx),
        tx.amount_cents,
        tx.type,
        tx.category,
        tx.card,
        pending,
        _iso(synced_at),
    )


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        init_db(db_path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            with conn:  # commit ou rollback
                yield conn
        finally:
            conn.close()

    def upsert_accounts(self, accounts: Sequence[Account], balance_at: datetime) -> None:
        with self._conn() as c:
            for a in accounts:
                c.execute(
                    """INSERT INTO accounts (id, label, type, ownership, parent_id, iban,
                           currency, balance_cents, balance_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET label=excluded.label, type=excluded.type,
                           ownership=excluded.ownership, parent_id=excluded.parent_id,
                           iban=excluded.iban, currency=excluded.currency,
                           balance_cents=excluded.balance_cents, balance_at=excluded.balance_at""",
                    (
                        a.id,
                        a.label,
                        a.type,
                        a.ownership,
                        a.parent_id,
                        a.iban,
                        a.currency,
                        a.balance_cents,
                        _iso(balance_at),
                    ),
                )

    def list_accounts(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def upsert_transactions(
        self, account_id: str, txs: Sequence[Transaction], synced_at: datetime
    ) -> int:
        fps = compute_fingerprints(account_id, txs)
        inserted = 0
        with self._conn() as c:
            for fp, tx in zip(fps, txs, strict=True):
                row = c.execute(
                    "SELECT id FROM transactions WHERE account_id = ? AND fingerprint = ?",
                    (account_id, fp),
                ).fetchone()
                if row is None:
                    c.execute(_TX_INSERT, _tx_params(account_id, fp, tx, 0, synced_at))
                    inserted += 1
                else:
                    c.execute(
                        """UPDATE transactions SET label=?, search_text=?, value_date=?,
                               category=?, card=?, type=?, pending=0, synced_at=? WHERE id=?""",
                        (
                            tx.label,
                            _search_text(tx),
                            _iso(tx.value_date),
                            tx.category,
                            tx.card,
                            tx.type,
                            _iso(synced_at),
                            row["id"],
                        ),
                    )
        return inserted

    def replace_pending(
        self, account_id: str, txs: Sequence[Transaction], synced_at: datetime
    ) -> None:
        fps = compute_fingerprints(f"{account_id}|pending", txs)
        with self._conn() as c:
            c.execute(
                "DELETE FROM transactions WHERE account_id = ? AND pending = 1", (account_id,)
            )
            for fp, tx in zip(fps, txs, strict=True):
                c.execute(_TX_INSERT, _tx_params(account_id, fp, tx, 1, synced_at))

    def max_debit_date(self, account_id: str) -> date | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(debit_date) AS d FROM transactions"
                " WHERE account_id = ? AND pending = 0",
                (account_id,),
            ).fetchone()
        return date.fromisoformat(row["d"]) if row["d"] else None

    def statement_needs_download(self, statement_id: str, home: Path) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT file_path FROM statements WHERE id = ?", (statement_id,)
            ).fetchone()
        return row is None or not (home / row["file_path"]).is_file()

    def save_statement(
        self, ref: StatementRef, rel_path: str, sha256: str, downloaded_at: datetime
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO statements
                       (id, account_id, period, date, doc_type, file_path, sha256, downloaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref.id,
                    ref.account_id,
                    ref.period,
                    _iso(ref.date),
                    ref.doc_type,
                    rel_path,
                    sha256,
                    _iso(downloaded_at),
                ),
            )

    def start_run(self, trigger: Trigger, scope: Scope, started_at: datetime) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO sync_runs (started_at, trigger, scope, status)"
                " VALUES (?, ?, ?, 'running')",
                (_iso(started_at), trigger, scope),
            )
            return int(cur.lastrowid)

    def set_run_status(
        self, run_id: int, status: RunStatus, twofa_method: str | None = None
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sync_runs SET status = ?, twofa_method = ? WHERE id = ?",
                (status, twofa_method, run_id),
            )

    def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        finished_at: datetime,
        added_transactions: int,
        added_statements: int,
        error_type: str | None,
        error_message: str | None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE sync_runs SET status=?, finished_at=?, added_transactions=?,
                       added_statements=?, error_type=?, error_message=? WHERE id=?""",
                (
                    status,
                    _iso(finished_at),
                    added_transactions,
                    added_statements,
                    error_type,
                    error_message,
                    run_id,
                ),
            )

    def _one_run(self, where: str) -> RunRow | None:
        with self._conn() as c:
            row = c.execute(
                f"SELECT * FROM sync_runs WHERE {where} ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _run_from_row(row) if row else None

    def current_run(self) -> RunRow | None:
        return self._one_run("status IN ('running', 'awaiting_2fa')")

    def last_finished_run(self) -> RunRow | None:
        return self._one_run("finished_at IS NOT NULL")

    def last_success_at(self) -> datetime | None:
        run = self._one_run("status IN ('ok', 'partial') AND finished_at IS NOT NULL")
        return run.finished_at if run else None

    def mark_interrupted_runs(self, at: datetime) -> int:
        with self._conn() as c:
            cur = c.execute(
                """UPDATE sync_runs SET status='error', error_message='interrompu',
                       finished_at=? WHERE status IN ('running', 'awaiting_2fa')""",
                (_iso(at),),
            )
            return cur.rowcount
