"""Recherche et agrégats sur les transactions (spec §7.1)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from bourso_mcp.source.models import TxType
from bourso_mcp.store.schema import connect
from bourso_mcp.store.text import normalize_text

Sort = Literal["date_desc", "date_asc", "amount_asc", "amount_desc"]
GroupBy = Literal["month", "category", "label", "type", "account"]

_DATE_COLUMNS = {"operation": "operation_date", "debit": "debit_date"}
_NO_CATEGORY = "(sans catégorie)"


@dataclass(frozen=True)
class TransactionFilter:
    account_ids: tuple[str, ...] | None = None
    date_from: date | None = None
    date_to: date | None = None
    date_field: Literal["operation", "debit"] = "operation"
    amount_min_cents: int | None = None  # comparé à abs(amount_cents)
    amount_max_cents: int | None = None
    direction: Literal["debit", "credit"] | None = None
    text: str | None = None
    types: tuple[TxType, ...] | None = None  # None => tous sauf "card_summary"
    category: str | None = None
    card: str | None = None  # fin du numéro masqué
    include_pending: bool = True


@dataclass(frozen=True)
class Totals:
    count: int
    debits_cents: int  # <= 0
    credits_cents: int


@dataclass(frozen=True)
class Group:
    key: str
    totals: Totals


@dataclass(frozen=True)
class SearchResult:
    items: list[sqlite3.Row]
    totals: Totals


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join("?" for _ in values) + ")"


def build_where(f: TransactionFilter) -> tuple[str, list[object]]:
    """Clause WHERE (sans le mot-clé) et paramètres liés, pour la table `transactions`."""
    date_col = _DATE_COLUMNS[f.date_field]
    clauses: list[str] = []
    params: list[object] = []

    if f.account_ids is not None:
        clauses.append(f"account_id IN {_in(f.account_ids)}")
        params.extend(f.account_ids)
    if f.date_from is not None:
        clauses.append(f"{date_col} >= ?")
        params.append(f.date_from.isoformat())
    if f.date_to is not None:
        clauses.append(f"{date_col} <= ?")
        params.append(f.date_to.isoformat())
    if f.amount_min_cents is not None:
        clauses.append("abs(amount_cents) >= ?")
        params.append(f.amount_min_cents)
    if f.amount_max_cents is not None:
        clauses.append("abs(amount_cents) <= ?")
        params.append(f.amount_max_cents)
    if f.direction == "debit":
        clauses.append("amount_cents < 0")
    elif f.direction == "credit":
        clauses.append("amount_cents > 0")
    if f.text:
        clauses.append("search_text LIKE '%' || ? || '%' ESCAPE '\\'")
        params.append(_escape_like(normalize_text(f.text)))
    if f.types is None:
        clauses.append("type <> ?")
        params.append("card_summary")
    else:
        clauses.append(f"type IN {_in(f.types)}")
        params.extend(f.types)
    if f.category is not None:
        clauses.append("category = ?")
        params.append(f.category)
    if f.card:
        clauses.append("card LIKE '%' || ? ESCAPE '\\'")
        params.append(_escape_like(f.card))
    if not f.include_pending:
        clauses.append("pending = 0")

    return " AND ".join(clauses) if clauses else "1=1", params


_SORTS = {
    "date_desc": ("{d} DESC, id DESC"),
    "date_asc": ("{d} ASC, id ASC"),
    "amount_asc": ("amount_cents ASC, id ASC"),
    "amount_desc": ("amount_cents DESC, id DESC"),
}

_TOTALS_SQL = """
    COUNT(*) AS n,
    COALESCE(SUM(CASE WHEN amount_cents < 0 THEN amount_cents ELSE 0 END), 0) AS debits,
    COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0) AS credits
"""


def _totals(row: sqlite3.Row) -> Totals:
    return Totals(row["n"], row["debits"], row["credits"])


def search(
    db_path: Path,
    f: TransactionFilter,
    sort: Sort = "date_desc",
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    where, params = build_where(f)
    order = _SORTS[sort].format(d=_DATE_COLUMNS[f.date_field])
    conn = connect(db_path)
    try:
        totals = _totals(
            conn.execute(f"SELECT {_TOTALS_SQL} FROM transactions WHERE {where}", params).fetchone()
        )
        items = conn.execute(
            f"SELECT * FROM transactions WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    finally:
        conn.close()
    return SearchResult(items, totals)


def summarize(
    db_path: Path, f: TransactionFilter, group_by: GroupBy, limit: int = 50
) -> list[Group]:
    keys = {
        "month": f"substr({_DATE_COLUMNS[f.date_field]}, 1, 7)",
        "category": f"coalesce(category, '{_NO_CATEGORY}')",
        "label": "label",
        "type": "type",
        "account": "account_id",
    }
    key_expr = keys[group_by]
    where, params = build_where(f)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"""SELECT {key_expr} AS k, {_TOTALS_SQL}
                FROM transactions WHERE {where}
                GROUP BY k
                ORDER BY abs(debits + credits) DESC, k ASC
                LIMIT ?""",
            [*params, limit],
        ).fetchall()
    finally:
        conn.close()
    return [Group(r["k"], _totals(r)) for r in rows]
