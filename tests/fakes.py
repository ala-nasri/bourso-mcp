"""Faux en mémoire pour les tests : source bancaire (Protocole `BankSource`) et keyring."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from keyring.backend import KeyringBackend
from keyring.credentials import SimpleCredential

from bourso_mcp.source.errors import NetworkError, SourceError, TwoFactorRequired
from bourso_mcp.source.models import Account, StatementRef, Transaction, TwoFactorMethod


class FakeBankSource:
    def __init__(
        self,
        accounts: list[Account],
        history: dict[str, list[Transaction]] | None = None,
        pending: dict[str, list[Transaction]] | None = None,
        statements: dict[str, list[tuple[StatementRef, bytes]]] | None = None,
        two_factor: TwoFactorMethod | None = None,
        expected_code: str = "123456",
        login_error: SourceError | None = None,
        account_errors: dict[str, SourceError] | None = None,
        network_failures: int = 0,
        then_methods: list[TwoFactorMethod] | None = None,
        list_accounts_network_failures: int = 0,
        submit_error: SourceError | None = None,
    ) -> None:
        self.accounts = accounts
        self.history = history or {}
        self.pending = pending or {}
        self.statements = statements or {}
        self.two_factor = two_factor
        self.expected_code = expected_code
        self.login_error = login_error
        self.account_errors = account_errors or {}
        self.network_failures = network_failures
        self.then_methods = list(then_methods or [])
        self.list_accounts_network_failures = list_accounts_network_failures
        self.submit_error = submit_error
        self.calls: list[tuple] = []
        self._login_calls = 0

    def login(self) -> None:
        self.calls.append(("login",))
        self._login_calls += 1
        if self._login_calls <= self.network_failures:
            raise NetworkError("réseau indisponible")
        if self.login_error is not None:
            raise self.login_error
        if self.two_factor is not None:
            raise TwoFactorRequired(self.two_factor)

    def submit_2fa(self, code: str | None) -> None:
        self.calls.append(("submit_2fa", code))
        if self.submit_error is not None:
            raise self.submit_error
        method = self.two_factor
        if method is None:
            return
        if (method == "app" and code is None) or code == self.expected_code:
            self.two_factor = None
            if self.then_methods:  # 2FA enchaînée : une autre méthode est demandée ensuite
                self.two_factor = self.then_methods.pop(0)
                raise TwoFactorRequired(self.two_factor)
            return
        raise TwoFactorRequired(method)

    def list_accounts(self) -> list[Account]:
        self.calls.append(("list_accounts",))
        if self.list_accounts_network_failures > 0:
            self.list_accounts_network_failures -= 1
            raise NetworkError("réseau indisponible")
        return list(self.accounts)

    def iter_transactions(self, account_id: str, since: date | None) -> Iterator[Transaction]:
        self.calls.append(("iter_transactions", account_id, since))
        if account_id in self.account_errors:
            raise self.account_errors[account_id]
        for tx in self.history.get(account_id, []):
            if since is None or tx.debit_date >= since:
                yield tx

    def iter_pending(self, account_id: str) -> Iterator[Transaction]:
        self.calls.append(("iter_pending", account_id))
        yield from self.pending.get(account_id, [])

    def list_statements(self, account_id: str) -> list[StatementRef]:
        self.calls.append(("list_statements", account_id))
        return [ref for ref, _ in self.statements.get(account_id, [])]

    def download_statement(self, ref: StatementRef) -> bytes:
        self.calls.append(("download_statement", ref.id))
        for ref2, data in self.statements.get(ref.account_id, []):
            if ref2.id == ref.id:
                return data
        raise KeyError(ref.id)


class InMemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)

    def get_credential(self, service, username):
        if username is not None:
            pw = self.get_password(service, username)
            return SimpleCredential(username, pw) if pw is not None else None
        for (svc, user), pw in self.store.items():
            if svc == service:
                return SimpleCredential(user, pw)
        return None
