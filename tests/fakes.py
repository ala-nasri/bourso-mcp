"""Source bancaire en mémoire, conforme au Protocole `BankSource`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

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
        method = self.two_factor
        if method is None:
            return
        if (method == "app" and code is None) or code == self.expected_code:
            self.two_factor = None
            return
        raise TwoFactorRequired(method)

    def list_accounts(self) -> list[Account]:
        self.calls.append(("list_accounts",))
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
