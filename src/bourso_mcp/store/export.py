"""Export CSV des transactions filtrées (spec §7.2), pour Excel en français."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from bourso_mcp.store.money import cents_to_fr
from bourso_mcp.store.queries import TransactionFilter, build_where
from bourso_mcp.store.schema import connect

CSV_HEADER = (
    "compte;date_operation;date_debit;libelle;libelle_brut;montant;type;categorie;carte;a_venir"
)


def export_csv(
    db_path: Path, f: TransactionFilter, exports_dir: Path, now: datetime
) -> tuple[Path, int]:
    where, params = build_where(f)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM transactions WHERE {where} ORDER BY operation_date ASC, id ASC",
            params,
        ).fetchall()
    finally:
        conn.close()

    exports_dir.mkdir(parents=True, exist_ok=True)
    path = exports_dir / f"transactions-{now:%Y%m%d-%H%M%S}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(CSV_HEADER.split(";"))
        for r in rows:
            writer.writerow(
                [
                    r["account_id"],
                    r["operation_date"],
                    r["debit_date"],
                    r["label"],
                    r["raw_label"],
                    cents_to_fr(r["amount_cents"]),
                    r["type"],
                    r["category"] or "",
                    r["card"] or "",
                    "oui" if r["pending"] else "non",
                ]
            )
    return path, len(rows)
