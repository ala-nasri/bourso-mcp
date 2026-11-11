from __future__ import annotations

from datetime import date

from bourso_mcp.source.models import Account, Transaction


def make_tx(
    op="2026-10-01",
    amount=-1250,
    raw="CB CAFE",
    label=None,
    type="card",
    debit=None,
    category=None,
    card=None,
    value=None,
) -> Transaction:
    return Transaction(
        operation_date=date.fromisoformat(op),
        debit_date=date.fromisoformat(debit or op),
        value_date=date.fromisoformat(value) if value else None,
        raw_label=raw,
        label=label if label is not None else raw,
        amount_cents=amount,
        type=type,
        category=category,
        card=card,
    )


def make_account(
    id="cc1", type="checking", ownership="owner", parent_id=None, balance_cents=100000
) -> Account:
    return Account(
        id=id,
        label=f"Compte {id}",
        type=type,
        ownership=ownership,
        parent_id=parent_id,
        iban=None,
        currency="EUR",
        balance_cents=balance_cents,
    )
