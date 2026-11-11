from __future__ import annotations

import base64
import json
import logging
import socket
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from woob.capabilities.bank import Account as WoobAccount
from woob.capabilities.bank import Transaction as WoobTransaction
from woob.capabilities.bank.base import AccountOwnership
from woob.capabilities.bill import Document, DocumentTypes, Subscription
from woob.exceptions import (
    ActionNeeded,
    AppValidation,
    AppValidationExpired,
    BrowserIncorrectPassword,
    BrowserUnavailable,
    OTPSentType,
    SentOTPQuestion,
)
from woob.tools.config.iconfig import ConfigError
from woob.tools.value import ValueTransient

from bourso_mcp.credentials import Credentials
from bourso_mcp.source import woob_source
from bourso_mcp.source.errors import (
    AuthFailed,
    NetworkError,
    SourceBroken,
    TwoFactorExpired,
    TwoFactorRequired,
)
from bourso_mcp.source.models import StatementRef
from bourso_mcp.source.woob_source import (
    ALLOWED_BACKEND_CALLS,
    VENDOR_WOOB,
    WoobBoursoSource,
    build_backend,
)
from bourso_mcp.source.woob_storage import EncryptedWoobStorage

CREDS = Credentials("24681357", "motdepassefictif")
CHECKING_ID = "00012345678"
JOINT_ID = "00087654321"


def sms():
    return SentOTPQuestion("code", medium_type=OTPSentType.SMS, message="Code SMS")


def email():
    return SentOTPQuestion("email_code", medium_type=OTPSentType.EMAIL, message="Code e-mail")


def woob_account(id, type=WoobAccount.TYPE_CHECKING, ownership=AccountOwnership.OWNER):
    account = WoobAccount(id=id)
    account.type = type
    account.label = f"Compte fictif {id[-3:]}"
    account.ownership = ownership
    account.balance = Decimal("100.00")
    return account


def woob_tx(day, amount="-1.00", raw="CB FICTIF"):
    tx = WoobTransaction()
    tx.date = day
    tx.amount = Decimal(amount)
    tx.raw = raw
    tx.type = WoobTransaction.TYPE_CARD
    return tx


def woob_document(id, day, type=DocumentTypes.STATEMENT):
    doc = Document(id=id)
    doc.date = day
    doc.type = type
    doc.format = "pdf"
    doc.url = f"https://example.invalid/{id}"
    return doc


class FakeBackend:
    """Backend woob factice : enregistre chaque appel ; `login_steps` fixe ce que lèvent les
    appels successifs à iter_accounts (ouverture de session), avant que la liste soit rendue."""

    def __init__(self, login_steps=(), accounts=None, history=None, coming=None, documents=None):
        fields = ("code", "email_code", "resume", "request_information")
        self.config = {name: ValueTransient(name) for name in fields}
        self.login_steps = list(login_steps)
        self.accounts = accounts if accounts is not None else [
            woob_account(CHECKING_ID),
            woob_account(JOINT_ID, ownership=AccountOwnership.CO_OWNER),
            woob_account("00011112222", type=WoobAccount.TYPE_SAVINGS),
        ]
        self.history = history or {}
        self.coming = coming or {}
        self.documents = documents or {}
        self.calls: list[str] = []
        self.answers: list[dict] = []  # réponses 2FA visibles à chaque ouverture de session
        self.history_reads = 0

    def _record(self, name):
        self.calls.append(name)

    def iter_accounts(self):
        self._record("iter_accounts")
        self.answers.append({k: v.get() for k, v in self.config.items()})
        if self.login_steps:
            raise self.login_steps.pop(0)
        return iter(self.accounts)

    def iter_history(self, account):
        self._record("iter_history")
        for tx in self.history.get(account.id, []):
            self.history_reads += 1
            yield tx

    def iter_coming(self, account):
        self._record("iter_coming")
        return iter(self.coming.get(account.id, []))

    def iter_subscription(self):
        self._record("iter_subscription")
        return iter([Subscription(id=account_id) for account_id in self.documents])

    def iter_documents(self, subscription):
        self._record("iter_documents")
        return iter(self.documents[subscription.id])

    def download_document(self, document):
        self._record("download_document")
        return f"%PDF-1.4 {document.id}".encode()

    def dump_state(self):
        self._record("dump_state")  # persistance de l'état de session (stockage local)


def make_source(backend, tmp_path, built=None):
    def factory(params, storage):
        if built is not None:
            built.append((params, storage))
        return backend

    return WoobBoursoSource(CREDS, tmp_path, backend_factory=factory)


# ---------------------------------------------------------------- construction


def test_constructing_source_creates_no_backend(tmp_path):
    built = []
    make_source(FakeBackend(), tmp_path, built)
    assert built == []
    assert list(tmp_path.iterdir()) == []


def test_backend_receives_credentials_interactive_flag_and_encrypted_storage(tmp_path):
    built = []
    make_source(FakeBackend(), tmp_path, built).login()
    assert len(built) == 1
    params, storage = built[0]
    assert params["login"] == "24681357"
    assert params["password"] == "motdepassefictif"
    assert params["request_information"] is not None  # note (b) : 2FA interactive
    assert isinstance(storage, EncryptedWoobStorage)
    assert storage.path == tmp_path / "state.bin"


def test_source_repr_hides_login_and_password(tmp_path):
    source = make_source(FakeBackend(), tmp_path)
    source.login()
    for text in (repr(source), repr(vars(source))):
        assert CREDS.login not in text
        assert CREDS.password not in text


# ---------------------------------------------------------------- session et 2FA


def test_login_caches_accounts_and_saves_state(tmp_path):
    backend = FakeBackend()
    source = make_source(backend, tmp_path)
    source.login()
    accounts = source.list_accounts()
    assert [(a.id, a.type, a.ownership) for a in accounts] == [
        (CHECKING_ID, "checking", "owner"),
        (JOINT_ID, "checking", "co_owner"),
    ]
    assert backend.calls == ["iter_accounts", "dump_state"]
    source.list_accounts()
    assert backend.calls.count("iter_accounts") == 1  # list_accounts lit le cache


def test_login_sms_then_submit_injects_code_and_clears_it(tmp_path):
    backend = FakeBackend(login_steps=[sms()])
    source = make_source(backend, tmp_path)
    with pytest.raises(TwoFactorRequired) as info:
        source.login()
    assert info.value.method == "sms"
    assert "dump_state" not in backend.calls

    source.submit_2fa("123456")
    assert backend.answers[-1]["code"] == "123456"
    assert backend.config["code"].get() is None  # jamais rejoué à une connexion suivante
    assert [a.id for a in source.list_accounts()] == [CHECKING_ID, JOINT_ID]
    assert backend.calls[-1] == "dump_state"


def test_login_email_two_factor(tmp_path):
    backend = FakeBackend(login_steps=[email()])
    source = make_source(backend, tmp_path)
    with pytest.raises(TwoFactorRequired) as info:
        source.login()
    assert info.value.method == "email"
    source.submit_2fa("654321")
    assert backend.answers[-1]["email_code"] == "654321"
    assert backend.config["email_code"].get() is None


def test_wrong_code_is_auth_failed_and_cleared(tmp_path):
    wrong = BrowserIncorrectPassword("Code incorrect", bad_fields=["code"])
    backend = FakeBackend(login_steps=[sms(), wrong])
    source = make_source(backend, tmp_path)
    with pytest.raises(TwoFactorRequired):
        source.login()
    with pytest.raises(AuthFailed):
        source.submit_2fa("000000")
    assert backend.config["code"].get() is None
    assert "dump_state" not in backend.calls
    with pytest.raises(SourceBroken):  # plus rien en attente : il faut relancer login()
        source.submit_2fa("000000")


def test_chained_sms_then_email(tmp_path):
    backend = FakeBackend(login_steps=[sms(), email()])
    source = make_source(backend, tmp_path)
    with pytest.raises(TwoFactorRequired):
        source.login()
    with pytest.raises(TwoFactorRequired) as info:
        source.submit_2fa("111111")
    assert info.value.method == "email"
    assert backend.config["code"].get() is None
    source.submit_2fa("222222")
    assert backend.answers[-1]["email_code"] == "222222"
    assert backend.answers[-1]["code"] is None
    assert backend.calls.count("iter_accounts") == 3


def test_app_validation_resumes(tmp_path):
    backend = FakeBackend(login_steps=[AppValidation(message="Valider dans l'appli")])
    source = make_source(backend, tmp_path)
    with pytest.raises(TwoFactorRequired) as info:
        source.login()
    assert info.value.method == "app"
    source.submit_2fa(None)
    assert backend.answers[-1]["resume"] is True
    assert backend.config["resume"].get() is None
    assert len(source.list_accounts()) == 2


def test_app_validation_expired(tmp_path):
    backend = FakeBackend(login_steps=[AppValidation(message="Valider"), AppValidationExpired()])
    source = make_source(backend, tmp_path)
    with pytest.raises(TwoFactorRequired):
        source.login()
    with pytest.raises(TwoFactorExpired):
        source.submit_2fa(None)
    assert backend.config["resume"].get() is None


def test_submit_without_pending_two_factor_is_a_programming_error(tmp_path):
    backend = FakeBackend()
    source = make_source(backend, tmp_path)
    with pytest.raises(SourceBroken):
        source.submit_2fa("123456")
    source.login()
    with pytest.raises(SourceBroken):
        source.submit_2fa("123456")


@pytest.mark.parametrize("code", [None, ""])
def test_empty_sms_code_is_refused_without_backend_call(tmp_path, code):
    backend = FakeBackend(login_steps=[sms()])
    source = make_source(backend, tmp_path)
    with pytest.raises(TwoFactorRequired):
        source.login()
    with pytest.raises(AuthFailed):
        source.submit_2fa(code)
    assert backend.calls == ["iter_accounts"]


def test_login_errors_are_translated(tmp_path):
    source = make_source(FakeBackend(login_steps=[BrowserIncorrectPassword("refusé")]), tmp_path)
    with pytest.raises(AuthFailed):
        source.login()


@pytest.mark.parametrize(
    "exc_type, expected",
    [
        (BrowserUnavailable, NetworkError),
        (BrowserIncorrectPassword, AuthFailed),
        (ActionNeeded, SourceBroken),
    ],
)
def test_credentials_never_appear_in_translated_message(tmp_path, exc_type, expected):
    leak = exc_type(f"erreur avec {CREDS.password} et {CREDS.login}")
    source = make_source(FakeBackend(login_steps=[leak]), tmp_path)
    with pytest.raises(expected) as info:
        source.login()
    assert CREDS.password not in str(info.value)
    assert CREDS.login not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.mark.parametrize(
    "failure",
    [OSError("disque plein"), AttributeError("état SESSIONID"), ConfigError("état SESSIONID")],
    ids=["oserror", "attributeerror", "woob-configerror"],
)
def test_state_save_failure_is_a_warning_not_a_failure(tmp_path, caplog, failure):
    class Failing(FakeBackend):
        def dump_state(self):
            self._record("dump_state")
            raise failure

    source = make_source(Failing(), tmp_path)
    with caplog.at_level(logging.WARNING, logger="bourso_mcp.source"):
        source.login()
    assert len(source.list_accounts()) == 2
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert type(failure).__name__ in caplog.text
    assert "disque plein" not in caplog.text
    assert "SESSIONID" not in caplog.text


def test_non_woob_exception_is_reraised_as_is(tmp_path):
    source = make_source(FakeBackend(login_steps=[ZeroDivisionError("bogue local")]), tmp_path)
    with pytest.raises(ZeroDivisionError):
        source.login()


# ---------------------------------------------------------------- opérations


def test_iter_transactions_stops_before_since(tmp_path):
    history = [
        woob_tx(date(2026, 10, 20)),
        woob_tx(date(2026, 10, 10)),
        woob_tx(date(2026, 10, 1)),  # première opération plus ancienne que since : arrêt
        woob_tx(date(2026, 9, 20)),
        woob_tx(date(2026, 9, 10)),
    ]
    backend = FakeBackend(history={CHECKING_ID: history})
    source = make_source(backend, tmp_path)
    source.login()
    txs = list(source.iter_transactions(CHECKING_ID, date(2026, 10, 10)))
    assert [t.debit_date for t in txs] == [date(2026, 10, 20), date(2026, 10, 10)]
    assert backend.history_reads == 3  # aucune lecture (requête de détail) au-delà


def test_iter_transactions_without_since_reads_everything(tmp_path):
    history = [woob_tx(date(2026, 10, 20)), woob_tx(date(2021, 1, 4))]
    source = make_source(FakeBackend(history={CHECKING_ID: history}), tmp_path)
    source.login()
    assert len(list(source.iter_transactions(CHECKING_ID, None))) == 2


def test_iter_transactions_translates_woob_errors(tmp_path):
    class Broken(FakeBackend):
        def iter_history(self, account):
            self._record("iter_history")
            yield woob_tx(date(2026, 10, 20))
            raise BrowserUnavailable("site en maintenance")

    source = make_source(Broken(), tmp_path)
    source.login()
    with pytest.raises(NetworkError):
        list(source.iter_transactions(CHECKING_ID, None))


def test_iter_pending_uses_iter_coming(tmp_path):
    backend = FakeBackend(coming={JOINT_ID: [woob_tx(date(2026, 11, 12), raw="PRLV A VENIR")]})
    source = make_source(backend, tmp_path)
    source.login()
    pending = list(source.iter_pending(JOINT_ID))
    assert [t.raw_label for t in pending] == ["PRLV A VENIR"]
    assert "iter_coming" in backend.calls
    assert "iter_history" not in backend.calls


def test_unknown_account_is_source_broken(tmp_path):
    source = make_source(FakeBackend(), tmp_path)
    source.login()
    with pytest.raises(SourceBroken):
        list(source.iter_transactions("inconnu", None))


# ---------------------------------------------------------------- relevés


def documents_for_checking():
    return {
        CHECKING_ID: [
            Document(id=f"{CHECKING_ID}_RIB"),  # RIB : sans date, ignoré
            woob_document(f"{CHECKING_ID}_20261030", date(2026, 10, 30)),
            woob_document(f"{CHECKING_ID}_20260930", date(2026, 9, 30)),
            woob_document(f"{CHECKING_ID}_frais", date(2026, 9, 28), type=DocumentTypes.NOTICE),
        ]
    }


def test_list_statements_links_subscription_to_account(tmp_path):
    backend = FakeBackend(documents=documents_for_checking())
    source = make_source(backend, tmp_path)
    source.login()
    refs = source.list_statements(CHECKING_ID)
    assert [(r.id, r.period, r.doc_type) for r in refs] == [
        (f"{CHECKING_ID}_20261030", "2026-10", "statement"),
        (f"{CHECKING_ID}_20260930", "2026-09", "statement"),
        (f"{CHECKING_ID}_frais", "2026-09", "other"),
    ]
    assert all(r.account_id == CHECKING_ID for r in refs)
    assert source.list_statements(JOINT_ID) == []  # pas de subscription : aucun relevé
    assert backend.calls.count("iter_subscription") == 1


def test_download_statement_returns_bytes(tmp_path):
    backend = FakeBackend(documents=documents_for_checking())
    source = make_source(backend, tmp_path)
    source.login()
    ref = source.list_statements(CHECKING_ID)[0]
    assert source.download_statement(ref) == f"%PDF-1.4 {CHECKING_ID}_20261030".encode()


def test_download_statement_without_handle_is_source_broken(tmp_path):
    source = make_source(FakeBackend(), tmp_path)
    source.login()
    ref = StatementRef("x_20260930", CHECKING_ID, date(2026, 9, 30), "2026-09", "statement")
    with pytest.raises(SourceBroken):
        source.download_statement(ref)


@pytest.mark.parametrize("content", [None, b"", "texte"])
def test_download_statement_requires_bytes(tmp_path, content):
    class Empty(FakeBackend):
        def download_document(self, document):
            self._record("download_document")
            return content

    backend = Empty(documents=documents_for_checking())
    source = make_source(backend, tmp_path)
    source.login()
    with pytest.raises(SourceBroken):
        source.download_statement(source.list_statements(CHECKING_ID)[0])


# ---------------------------------------------------------------- lecture seule


def test_call_refuses_names_outside_allowlist(tmp_path):
    backend = FakeBackend()
    source = make_source(backend, tmp_path)
    for name in ("init_transfer", "execute_transfer", "new_recipient", "iter_transfer_recipients"):
        with pytest.raises(SourceBroken):
            source._call(name)
    assert backend.calls == []


def test_source_only_calls_allowed_backend_methods(tmp_path):
    backend = FakeBackend(
        login_steps=[sms(), email()],
        history={CHECKING_ID: [woob_tx(date(2026, 10, 20))]},
        coming={CHECKING_ID: [woob_tx(date(2026, 11, 12))]},
        documents=documents_for_checking(),
    )
    source = make_source(backend, tmp_path)
    with pytest.raises(TwoFactorRequired):
        source.login()
    with pytest.raises(TwoFactorRequired):
        source.submit_2fa("111111")
    source.submit_2fa("222222")
    for account in source.list_accounts():
        list(source.iter_transactions(account.id, None))
        list(source.iter_pending(account.id))
        for ref in source.list_statements(account.id):
            source.download_statement(ref)
    bank_calls = [name for name in backend.calls if name != "dump_state"]
    assert set(bank_calls) <= ALLOWED_BACKEND_CALLS
    assert set(bank_calls) == ALLOWED_BACKEND_CALLS  # le flux complet les utilise toutes
    assert backend.calls.count("dump_state") == 1  # seule écriture : l'état local, après la 2FA


def test_allowed_backend_calls_are_read_only():
    assert ALLOWED_BACKEND_CALLS == frozenset(
        {
            "iter_accounts",
            "iter_history",
            "iter_coming",
            "iter_subscription",
            "iter_documents",
            "download_document",
        }
    )


def test_source_module_never_references_write_methods():
    src = Path(woob_source.__file__).read_text(encoding="utf-8").lower()
    assert not any(w in src for w in ("transfer", "recipient"))


# ---------------------------------------------------------------- vrai woob, sans réseau


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tentative d'accès réseau pendant un test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def reversible_storage(path):
    return EncryptedWoobStorage(
        path,
        protect=lambda data: base64.b64encode(data),
        unprotect=lambda blob: base64.b64decode(blob),
    )


def test_real_woob_backend_follows_notes_a_b_j(tmp_path, no_network):
    """Construction réelle (notes a, b) et sauvegarde de l'état dans le stockage (note j),
    sans aucune requête : ni le navigateur ni dump_state ne contactent la banque ici."""
    path = tmp_path / "state.bin"
    params = {"login": "12345678", "password": "motdepassefictif", "request_information": {}}
    backend = build_backend(VENDOR_WOOB / "modules", params, reversible_storage(path))
    assert type(backend).__name__ == "BoursoramaModule"
    assert backend.config["request_information"].get() is not None
    assert {"code", "email_code", "resume"} <= set(backend.config)
    assert backend.browser.is_interactive  # lu à la construction du navigateur (note b)

    backend.dump_state()
    assert [p.name for p in tmp_path.iterdir()] == ["state.bin"]
    state = json.loads(base64.b64decode(path.read_bytes()))
    browser_state = state["backends"]["boursorama"]["browser_state"]
    assert "cookies" in browser_state
    assert "motdepassefictif" not in json.dumps(state)
    reloaded = reversible_storage(path)
    assert reloaded.get("backends", "boursorama", "browser_state") == browser_state
