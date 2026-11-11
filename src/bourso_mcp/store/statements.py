"""Liste et lecture des relevés PDF (spec §7.2)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from bourso_mcp.store.queries import _in
from bourso_mcp.store.schema import connect


class StatementNotFound(LookupError):
    pass


class StatementFileMissing(FileNotFoundError):
    pass


class StatementUnreadable(ValueError):
    pass


@dataclass(frozen=True)
class StatementText:
    row: sqlite3.Row
    path: Path
    pages: int
    text: str
    has_text: bool


def list_statements(
    db_path: Path,
    account_ids: tuple[str, ...] | None,
    period_from: str | None,
    period_to: str | None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if account_ids is not None:
        clauses.append(f"account_id IN {_in(account_ids)}")
        params.extend(account_ids)
    if period_from is not None:
        clauses.append("period >= ?")
        params.append(period_from)
    if period_to is not None:
        clauses.append("period <= ?")
        params.append(period_to)
    where = " AND ".join(clauses) if clauses else "1=1"
    conn = connect(db_path)
    try:
        return conn.execute(
            f"SELECT * FROM statements WHERE {where} ORDER BY period DESC, account_id, id",
            params,
        ).fetchall()
    finally:
        conn.close()


def read_statement(db_path: Path, home: Path, statement_id: str) -> StatementText:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM statements WHERE id = ?", (statement_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise StatementNotFound(statement_id)
    path = (home / row["file_path"]).resolve()
    if not path.is_relative_to(home.resolve()):
        raise StatementFileMissing("chemin hors du dossier de données")
    if not path.is_file():
        raise StatementFileMissing("fichier du relevé absent")
    try:
        reader = PdfReader(path)
        pages = len(reader.pages)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PyPdfError, ValueError, OSError) as e:
        raise StatementUnreadable("PDF illisible ou corrompu") from e
    return StatementText(row, path, pages, text, bool(text.strip()))
