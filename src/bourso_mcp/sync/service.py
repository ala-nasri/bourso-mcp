"""Service de synchronisation : un passage complet sur les comptes, opérations et relevés."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from bourso_mcp.config import Settings
from bourso_mcp.source.base import BankSource
from bourso_mcp.source.errors import SourceError
from bourso_mcp.source.models import Account, StatementRef, TwoFactorMethod
from bourso_mcp.store.repository import Repository, RunStatus, Scope, Trigger

TwoFactorHandler = Callable[[TwoFactorMethod], str | None]
OVERLAP = timedelta(days=10)


@dataclass(frozen=True)
class AccountOutcome:
    account_id: str
    ok: bool
    added_transactions: int
    added_statements: int
    error_type: str | None


@dataclass(frozen=True)
class SyncRunResult:
    run_id: int
    status: RunStatus
    added_transactions: int
    added_statements: int
    accounts: list[AccountOutcome]
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True)
class DryRunLine:
    account_id: str
    label: str
    type: str
    transactions: int
    pending: int
    statements: int


def _status_for_error(exc: SourceError) -> tuple[RunStatus, str, str]:
    """Seul endroit où une erreur de la source devient un statut de synchronisation."""
    return "error", type(exc).__name__, str(exc)


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SyncService:
    def __init__(
        self,
        source_factory: Callable[[], BankSource],
        repo: Repository,
        settings: Settings,
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._source_factory = source_factory
        self._repo = repo
        self._settings = settings
        self._now = now
        self._sleep = sleep

    def run(
        self, scope: Scope, trigger: Trigger, two_factor: TwoFactorHandler, *, force: bool = False
    ) -> SyncRunResult:
        run_id = self._repo.start_run(trigger, scope, self._now())
        try:
            return self._run(run_id, scope)
        except Exception as exc:  # un bogue ne doit pas laisser la synchro « running »
            self._repo.finish_run(run_id, "error", self._now(), 0, 0, type(exc).__name__, None)
            raise

    def dry_run(self, two_factor: TwoFactorHandler) -> list[DryRunLine]:
        source = self._source_factory()
        source.login()
        lines = []
        for account in source.list_accounts():
            since = self._since(account.id)
            lines.append(
                DryRunLine(
                    account_id=account.id,
                    label=account.label,
                    type=account.type,
                    transactions=sum(1 for _ in source.iter_transactions(account.id, since)),
                    pending=sum(1 for _ in source.iter_pending(account.id)),
                    statements=len(source.list_statements(account.id)),
                )
            )
        return lines

    def _run(self, run_id: int, scope: Scope) -> SyncRunResult:
        try:
            source = self._login()
            accounts = source.list_accounts()
        except SourceError as exc:
            status, error_type, message = _status_for_error(exc)
            return self._finish(run_id, status, [], error_type, message)
        self._repo.upsert_accounts(accounts, self._now())
        outcomes = [self._sync_account(source, a, scope) for a in accounts]
        status, error_type, message = self._final_status(outcomes)
        return self._finish(run_id, status, outcomes, error_type, message)

    def _login(self) -> BankSource:
        source = self._source_factory()
        source.login()
        return source

    def _since(self, account_id: str) -> date | None:
        last = self._repo.max_debit_date(account_id)
        return None if last is None else last - OVERLAP

    def _sync_account(self, source: BankSource, account: Account, scope: Scope) -> AccountOutcome:
        added_tx = added_st = 0
        try:
            if scope in ("all", "transactions"):
                added_tx = self._transactions_step(source, account.id)
            if scope in ("all", "statements"):
                added_st = self._statements_step(source, account.id)
        except SourceError as exc:
            return AccountOutcome(account.id, False, added_tx, added_st, type(exc).__name__)
        return AccountOutcome(account.id, True, added_tx, added_st, None)

    def _transactions_step(self, source: BankSource, account_id: str) -> int:
        since = self._since(account_id)
        txs = list(source.iter_transactions(account_id, since))
        added = self._repo.upsert_transactions(account_id, txs, self._now())
        self._repo.replace_pending(account_id, list(source.iter_pending(account_id)), self._now())
        return added

    def _statements_step(self, source: BankSource, account_id: str) -> int:
        home = self._settings.home
        added = 0
        for ref in source.list_statements(account_id):
            if not self._repo.statement_needs_download(ref.id, home):
                continue
            data = source.download_statement(ref)
            path = self._store_statement(ref, data)
            self._repo.save_statement(
                ref,
                path.relative_to(home).as_posix(),
                hashlib.sha256(data).hexdigest(),
                self._now(),
            )
            added += 1
        return added

    def _store_statement(self, ref: StatementRef, data: bytes) -> Path:
        directory = self._settings.statements_dir / ref.account_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{ref.period}.pdf"
        n = 2
        while path.exists():
            path = directory / f"{ref.period}-{n}.pdf"
            n += 1
        _write_atomic(path, data)
        return path

    @staticmethod
    def _final_status(
        outcomes: list[AccountOutcome],
    ) -> tuple[RunStatus, str | None, str | None]:
        failed = [o for o in outcomes if not o.ok]
        if not failed:
            return "ok", None, None
        status: RunStatus = "partial" if len(failed) < len(outcomes) else "error"
        message = ", ".join(f"{o.account_id}: {o.error_type}" for o in failed)
        return status, failed[0].error_type, message

    def _finish(
        self,
        run_id: int,
        status: RunStatus,
        outcomes: list[AccountOutcome],
        error_type: str | None,
        error_message: str | None,
    ) -> SyncRunResult:
        added_tx = sum(o.added_transactions for o in outcomes)
        added_st = sum(o.added_statements for o in outcomes)
        self._repo.finish_run(
            run_id, status, self._now(), added_tx, added_st, error_type, error_message
        )
        return SyncRunResult(
            run_id, status, added_tx, added_st, outcomes, error_type, error_message
        )
