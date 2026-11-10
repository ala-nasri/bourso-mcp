"""Interface `BankSource` : contrat indépendant de woob."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Protocol

from bourso_mcp.source.models import Account, StatementRef, Transaction


class BankSource(Protocol):
    def login(self) -> None:
        """Ouvre la session ; lève TwoFactorRequired(method) si la 2FA est demandée."""
        ...

    def submit_2fa(self, code: str | None) -> None:
        """Transmet le code SMS ou e-mail, ou None pour attendre la validation dans l'appli."""
        ...

    def list_accounts(self) -> list[Account]: ...

    def iter_transactions(self, account_id: str, since: date | None) -> Iterator[Transaction]:
        """Opérations comptabilisées depuis `since` (None : tout)."""
        ...

    def iter_pending(self, account_id: str) -> Iterator[Transaction]:
        """Opérations à venir."""
        ...

    def list_statements(self, account_id: str) -> list[StatementRef]: ...

    def download_statement(self, ref: StatementRef) -> bytes: ...
