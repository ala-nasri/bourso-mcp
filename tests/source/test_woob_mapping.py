from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
import requests
from woob.capabilities.bank import Account as WoobAccount
from woob.capabilities.bank import Transaction as WoobTransaction
from woob.capabilities.bank.base import AccountOwnership
from woob.capabilities.base import NotAvailable
from woob.capabilities.bill import Document, DocumentTypes
from woob.exceptions import (
    ActionNeeded,
    AppValidation,
    AppValidationCancelled,
    AppValidationExpired,
    AuthMethodNotImplemented,
    BrowserIncorrectPassword,
    BrowserPasswordExpired,
    BrowserUnavailable,
    BrowserUserBanned,
    NeedInteractiveFor2FA,
    OfflineOTPQuestion,
    OTPSentType,
    ParseError,
    SentOTPQuestion,
)
from woob.tools.value import Value

from bourso_mcp.source.errors import (
    AuthFailed,
    AuthMethodUnsupported,
    NetworkError,
    SourceBroken,
    TwoFactorExpired,
    TwoFactorRequired,
)
from bourso_mcp.source.models import Account, StatementRef, Transaction
from bourso_mcp.source.woob_mapping import (
    to_account,
    to_statement_ref,
    to_transaction,
    translate_error,
)


def woob_account(id="00012345678", type=WoobAccount.TYPE_CHECKING, **fields):
    account = WoobAccount(id=id)
    account.type = type
    account.label = fields.pop("label", "Compte fictif")
    for name, value in fields.items():
        setattr(account, name, value)
    return account


def woob_tx(amount="-12.50", day=date(2026, 10, 3), **fields):
    tx = WoobTransaction()
    tx.date = day
    tx.amount = Decimal(amount)
    for name, value in fields.items():
        setattr(tx, name, value)
    return tx


def test_to_account_checking_joint():
    a = woob_account(
        ownership=AccountOwnership.CO_OWNER, balance=Decimal("1234.56"), currency="EUR"
    )
    assert to_account(a) == Account(
        id="00012345678",
        label="Compte fictif",
        type="checking",
        ownership="co_owner",
        parent_id=None,
        iban=None,
        currency="EUR",
        balance_cents=123456,
    )


@pytest.mark.parametrize(
    "ownership, expected",
    [
        (AccountOwnership.OWNER, "owner"),
        (AccountOwnership.CO_OWNER, "co_owner"),
        (AccountOwnership.ATTORNEY, "attorney"),
        (NotAvailable, "owner"),
        (None, "owner"),
    ],
)
def test_to_account_ownership(ownership, expected):
    assert to_account(woob_account(ownership=ownership)).ownership == expected


def test_to_account_defaults_when_fields_are_empty():
    account = to_account(woob_account())  # solde, devise, IBAN et parent non chargés
    assert account.currency == "EUR"
    assert account.balance_cents is None
    assert account.iban is None
    assert account.parent_id is None
    assert account.ownership == "owner"


def test_to_account_ignores_savings():
    assert to_account(woob_account(type=WoobAccount.TYPE_SAVINGS)) is None
    assert to_account(woob_account(type=WoobAccount.TYPE_MARKET)) is None
    assert to_account(woob_account(type=WoobAccount.TYPE_UNKNOWN)) is None


def test_to_account_card_has_parent():
    parent = woob_account(id="00012345678")
    card = woob_account(
        id="XXXXXXXXXXXX1234",
        type=WoobAccount.TYPE_CARD,
        parent=parent,
        balance=Decimal("-45.10"),
    )
    account = to_account(card)
    assert account.type == "card"
    assert account.parent_id == "00012345678"
    assert account.balance_cents == -4510


def test_to_transaction_dates_and_amount():
    tx = to_transaction(woob_tx(amount="-12.505", rdate=NotAvailable, raw="CB CAFE FICTIF"))
    assert tx.operation_date == date(2026, 10, 3)
    assert tx.debit_date == date(2026, 10, 3)
    assert tx.value_date is None
    assert tx.amount_cents == -1251


def test_to_transaction_full():
    tx = to_transaction(
        woob_tx(
            amount="2000.005",
            day=datetime(2026, 10, 5, 0, 0),
            rdate=date(2026, 10, 2),
            vdate=date(2026, 10, 6),
            raw="VIR SEPA FICTIF",
            label="Virement fictif",
            type=WoobTransaction.TYPE_TRANSFER,
            category="Revenus",
            card="",
        )
    )
    assert tx == Transaction(
        operation_date=date(2026, 10, 2),
        debit_date=date(2026, 10, 5),
        value_date=date(2026, 10, 6),
        raw_label="VIR SEPA FICTIF",
        label="Virement fictif",
        amount_cents=200001,
        type="transfer",
        category="Revenus",
        card=None,
    )


def test_to_transaction_labels_fall_back_on_each_other():
    only_raw = to_transaction(woob_tx(raw="PRLV FICTIF"))
    assert (only_raw.raw_label, only_raw.label) == ("PRLV FICTIF", "PRLV FICTIF")
    only_label = to_transaction(woob_tx(label="Prélèvement fictif", raw=NotAvailable))
    assert (only_label.raw_label, only_label.label) == ("Prélèvement fictif", "Prélèvement fictif")


def test_to_transaction_without_date_is_source_broken():
    tx = WoobTransaction()
    tx.amount = Decimal(1)
    with pytest.raises(SourceBroken):
        to_transaction(tx)


@pytest.mark.parametrize(
    "woob_type, expected",
    [
        (WoobTransaction.TYPE_CARD, "card"),
        (WoobTransaction.TYPE_DEFERRED_CARD, "card"),
        (WoobTransaction.TYPE_CARD_SUMMARY, "card_summary"),
        (WoobTransaction.TYPE_TRANSFER, "transfer"),
        (WoobTransaction.TYPE_INSTANT, "transfer"),
        (WoobTransaction.TYPE_ORDER, "order"),
        (WoobTransaction.TYPE_CHECK, "check"),
        (WoobTransaction.TYPE_DEPOSIT, "deposit"),
        (WoobTransaction.TYPE_CASH_DEPOSIT, "deposit"),
        (WoobTransaction.TYPE_WITHDRAWAL, "withdrawal"),
        (WoobTransaction.TYPE_BANK, "bank_fee"),
        (WoobTransaction.TYPE_UNKNOWN, "other"),
        (WoobTransaction.TYPE_PAYBACK, "other"),
        (WoobTransaction.TYPE_LOAN_PAYMENT, "other"),
        (WoobTransaction.TYPE_MARKET_ORDER, "other"),
        (WoobTransaction.TYPE_MARKET_FEE, "other"),
        (WoobTransaction.TYPE_ARBITRAGE, "other"),
        (WoobTransaction.TYPE_PROFIT, "other"),
    ],
)
def test_type_mapping(woob_type, expected):
    assert to_transaction(woob_tx(raw="X", type=woob_type)).type == expected


def woob_document(id="00012345678_20260930", day=date(2026, 9, 30), type=DocumentTypes.STATEMENT):
    doc = Document(id=id)
    doc.date = day
    doc.type = type
    doc.format = "pdf"
    return doc


def test_to_statement_ref_period():
    # Note (f) : period = mois de Document.date ; le relevé de fin septembre porte sur septembre.
    doc = woob_document()
    ref = to_statement_ref("00012345678", doc)
    assert ref == StatementRef(
        id="00012345678_20260930",
        account_id="00012345678",
        date=date(2026, 9, 30),
        period="2026-09",
        doc_type="statement",
    )
    assert ref.handle is doc
    early = to_statement_ref("00012345678", woob_document(day=datetime(2026, 10, 27, 0, 0)))
    assert (early.date, early.period) == (date(2026, 10, 27), "2026-10")


def test_to_statement_ref_other_document_type():
    ref = to_statement_ref("00012345678", woob_document(type=DocumentTypes.NOTICE))
    assert ref.doc_type == "other"


def test_to_statement_ref_without_date_is_source_broken():
    with pytest.raises(SourceBroken):
        to_statement_ref("00012345678", woob_document(day=NotAvailable))


@pytest.mark.parametrize(
    "exc, expected_type",
    [
        (BrowserIncorrectPassword("identifiants refusés"), AuthFailed),
        (BrowserUserBanned("compte bloqué"), AuthFailed),
        (SentOTPQuestion("code", medium_type=OTPSentType.SMS), TwoFactorRequired),
        (SentOTPQuestion("email_code", medium_type=OTPSentType.EMAIL), TwoFactorRequired),
        (AppValidation(message="Valider dans l'appli"), TwoFactorRequired),
        (AppValidationExpired(), TwoFactorExpired),
        (AppValidationCancelled("annulée"), TwoFactorExpired),
        (NeedInteractiveFor2FA(), TwoFactorExpired),
        (AuthMethodNotImplemented("QR code"), AuthMethodUnsupported),
        (OfflineOTPQuestion("otp_inconnu"), AuthMethodUnsupported),
        (BrowserUnavailable("maintenance"), NetworkError),
        (requests.exceptions.ConnectionError("refusée"), NetworkError),
        (requests.exceptions.ReadTimeout("délai"), NetworkError),
        (requests.exceptions.ConnectTimeout("délai"), NetworkError),
        (ActionNeeded("conditions à accepter"), SourceBroken),
        (BrowserPasswordExpired("mot de passe expiré"), SourceBroken),
        (ParseError("page inattendue"), SourceBroken),
        (ValueError("bogue local"), type(None)),
        (KeyError("x"), type(None)),
    ],
)
def test_translate_error(exc, expected_type):
    assert type(translate_error(exc)) is expected_type


@pytest.mark.parametrize(
    "exc, method",
    [
        (SentOTPQuestion("code", medium_type=OTPSentType.SMS), "sms"),
        (SentOTPQuestion("email_code", medium_type=OTPSentType.EMAIL), "email"),
        (AppValidation(message="Valider dans l'appli"), "app"),
    ],
)
def test_translate_error_two_factor_method(exc, method):
    assert translate_error(exc).method == method


def test_translate_error_keeps_woob_message():
    error = translate_error(ActionNeeded("Conditions générales à accepter"))
    assert "Conditions générales à accepter" in str(error)


def test_translate_error_builtin_raised_by_woob_code():
    # Un ValueError levé à l'intérieur de woob (ici, woob.tools.value) vient du code woob.
    value = Value("login", regexp=r"^[0-9]+$")
    with pytest.raises(ValueError) as info:
        value.set("abc")
    error = translate_error(info.value)
    assert isinstance(error, SourceBroken)
    assert "regexp" in str(error)


def test_translate_error_builtin_raised_outside_woob_is_none():
    with pytest.raises(AssertionError) as info:
        raise AssertionError("bogue local")
    assert translate_error(info.value) is None
