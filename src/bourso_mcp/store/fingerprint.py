"""Empreintes stables des transactions (rang pour les opérations identiques)."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Sequence

from bourso_mcp.source.models import Transaction


def compute_fingerprints(scope_key: str, txs: Sequence[Transaction]) -> list[str]:
    seen: Counter[str] = Counter()
    out: list[str] = []
    for tx in txs:
        base = f"{scope_key}|{tx.operation_date.isoformat()}|{tx.amount_cents}|{tx.raw_label}"
        rank = seen[base]
        seen[base] += 1
        out.append(hashlib.sha256(f"{base}|{rank}".encode()).hexdigest())
    return out
