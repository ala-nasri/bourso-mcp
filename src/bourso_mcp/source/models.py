"""Modèle de domaine, indépendant de woob. Montants en centimes entiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

AccountType = Literal["checking", "card"]
Ownership = Literal["owner", "co_owner", "attorney"]
TxType = Literal[
    "card", "card_summary", "transfer", "order", "check", "deposit", "withdrawal", "bank_fee", "other"
]
TwoFactorMethod = Literal["sms", "email", "app"]


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    type: AccountType
    ownership: Ownership
    parent_id: str | None
    iban: str | None
    currency: str
    balance_cents: int | None


@dataclass(frozen=True)
class Transaction:
    operation_date: date
    debit_date: date
    value_date: date | None
    raw_label: str
    label: str
    amount_cents: int
    type: TxType
    category: str | None
    card: str | None


@dataclass(frozen=True)
class StatementRef:
    id: str
    account_id: str
    date: date
    period: str  # "AAAA-MM"
    doc_type: Literal["statement", "other"]
    handle: object = field(default=None, compare=False, repr=False)
