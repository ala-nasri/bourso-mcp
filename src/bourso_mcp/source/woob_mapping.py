"""Correspondance woob -> domaine : comptes, opérations, documents et exceptions (README)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import requests
from woob.capabilities.bank import Account as WoobAccount
from woob.capabilities.bank import Transaction as WoobTransaction
from woob.capabilities.bank.base import AccountOwnership
from woob.capabilities.base import empty
from woob.capabilities.bill import Document, DocumentTypes
from woob.exceptions import (
    AppValidation,
    AppValidationCancelled,
    AppValidationExpired,
    AuthMethodNotImplemented,
    BrowserIncorrectPassword,
    BrowserQuestion,
    BrowserUnavailable,
    NeedInteractiveFor2FA,
)

from bourso_mcp.credentials import CMDKEY_HINT
from bourso_mcp.source.errors import (
    AuthFailed,
    AuthMethodUnsupported,
    NetworkError,
    SourceBroken,
    SourceError,
    TwoFactorExpired,
    TwoFactorRequired,
)
from bourso_mcp.source.models import (
    Account,
    AccountType,
    Ownership,
    StatementRef,
    Transaction,
    TwoFactorMethod,
    TxType,
)

_ACCOUNT_TYPES: dict[Any, AccountType] = {
    WoobAccount.TYPE_CHECKING: "checking",
    WoobAccount.TYPE_CARD: "card",
}
_OWNERSHIPS: dict[str, Ownership] = {
    AccountOwnership.OWNER: "owner",
    AccountOwnership.CO_OWNER: "co_owner",
    AccountOwnership.ATTORNEY: "attorney",
}
_TX_TYPES: dict[Any, TxType] = {
    WoobTransaction.TYPE_CARD: "card",
    WoobTransaction.TYPE_DEFERRED_CARD: "card",
    WoobTransaction.TYPE_CARD_SUMMARY: "card_summary",
    WoobTransaction.TYPE_TRANSFER: "transfer",
    WoobTransaction.TYPE_INSTANT: "transfer",
    WoobTransaction.TYPE_ORDER: "order",
    WoobTransaction.TYPE_CHECK: "check",
    WoobTransaction.TYPE_DEPOSIT: "deposit",
    WoobTransaction.TYPE_CASH_DEPOSIT: "deposit",
    WoobTransaction.TYPE_WITHDRAWAL: "withdrawal",
    WoobTransaction.TYPE_BANK: "bank_fee",
}
# Champ de configuration woob qui reçoit la réponse -> méthode du domaine (note b).
_QUESTION_FIELDS: dict[str, TwoFactorMethod] = {"code": "sms", "email_code": "email"}


def _cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _day(value: date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _text(value: Any) -> str | None:
    return None if empty(value) or value == "" else str(value)


def to_account(a: WoobAccount) -> Account | None:
    account_type = _ACCOUNT_TYPES.get(a.type)
    if account_type is None:
        return None  # épargne, bourse, crédit... : hors périmètre
    return Account(
        id=a.id,
        label=_text(a.label) or a.id,
        type=account_type,
        ownership=_OWNERSHIPS.get(a.ownership, "owner"),  # vide : titulaire
        parent_id=None if empty(a.parent) else a.parent.id,
        iban=_text(a.iban),
        currency=_text(a.currency) or "EUR",
        balance_cents=None if empty(a.balance) else _cents(a.balance),
    )


def to_transaction(t: WoobTransaction) -> Transaction:
    if empty(t.date) or empty(t.amount):
        raise SourceBroken("opération woob sans date de débit ou sans montant")
    raw, label = _text(t.raw), _text(t.label)
    return Transaction(
        operation_date=_day(t.date if empty(t.rdate) else t.rdate),
        debit_date=_day(t.date),
        value_date=None if empty(t.vdate) else _day(t.vdate),
        raw_label=raw or label or "",
        label=label or raw or "",
        amount_cents=_cents(t.amount),
        type=_TX_TYPES.get(t.type, "other"),
        category=_text(t.category),
        card=_text(t.card),
    )


def to_statement_ref(account_id: str, d: Document) -> StatementRef:
    """Note (f) : `period` est le mois de `Document.date`, jamais tiré du libellé."""
    if empty(d.date):
        raise SourceBroken("document woob sans date")
    day = _day(d.date)
    return StatementRef(
        id=d.id,
        account_id=account_id,
        date=day,
        period=day.strftime("%Y-%m"),
        doc_type="statement" if d.type == DocumentTypes.STATEMENT else "other",
        handle=d,
    )


def _is_woob(module_name: str) -> bool:
    return module_name == "woob" or module_name.startswith(("woob.", "woob_modules"))


def _raised_by_woob(e: Exception) -> bool:
    """Classe définie par woob, ou exception passée par du code woob (cœur ou module chargé)."""
    if _is_woob(type(e).__module__):
        return True
    tb = e.__traceback__
    while tb is not None:
        if _is_woob(tb.tb_frame.f_globals.get("__name__", "")):
            return True
        tb = tb.tb_next
    return False


def _woob_message(e: Exception) -> str:
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def translate_error(e: Exception) -> SourceError | None:
    """Erreur du domaine pour une exception woob ; None pour une exception non woob (à relancer)."""
    if isinstance(e, BrowserIncorrectPassword):
        if set(e.bad_fields or ()) & set(_QUESTION_FIELDS):
            return AuthFailed(f"code 2FA refusé par BoursoBank ({_woob_message(e)})")
        return AuthFailed(
            f"identifiants refusés par BoursoBank ({_woob_message(e)}) : mettez à jour l'entrée "
            f"avec {CMDKEY_HINT}, puis relancez bourso-mcp login"
        )
    if isinstance(e, AuthMethodNotImplemented):
        return AuthMethodUnsupported(
            f"méthode d'authentification forte non gérée ({_woob_message(e)}) : choisissez le SMS, "
            "l'e-mail ou l'appli dans l'espace BoursoBank"
        )
    if isinstance(e, BrowserQuestion):
        for field in e.fields:
            if field.id in _QUESTION_FIELDS:
                return TwoFactorRequired(_QUESTION_FIELDS[field.id])
        names = ", ".join(str(field.id) for field in e.fields)
        return AuthMethodUnsupported(f"question d'authentification non gérée (champ {names})")
    if isinstance(e, AppValidation):
        return TwoFactorRequired("app")
    if isinstance(e, AppValidationExpired | AppValidationCancelled | NeedInteractiveFor2FA):
        return TwoFactorExpired(f"authentification forte expirée ou annulée ({type(e).__name__})")
    if isinstance(
        e, BrowserUnavailable | requests.exceptions.ConnectionError | requests.exceptions.Timeout
    ):
        return NetworkError(f"BoursoBank injoignable ou indisponible ({type(e).__name__})")
    if _raised_by_woob(e):
        return SourceBroken(_woob_message(e))
    return None
