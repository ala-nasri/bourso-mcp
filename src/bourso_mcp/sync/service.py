"""Service de synchronisation : un passage complet sur les comptes, opérations et relevés."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypeVar

from filelock import FileLock, Timeout

from bourso_mcp.config import Settings
from bourso_mcp.source.base import BankSource
from bourso_mcp.source.errors import (
    AuthFailed,
    NetworkError,
    SourceError,
    TwoFactorExpired,
    TwoFactorRequired,
)
from bourso_mcp.source.models import Account, StatementRef, TwoFactorMethod
from bourso_mcp.store.repository import Repository, RunRow, RunStatus, Scope, Trigger

TwoFactorHandler = Callable[[TwoFactorMethod], str | None]
OVERLAP = timedelta(days=10)
RATE_LIMIT = timedelta(hours=1)
RETRY_DELAYS = (2, 8, 30)
MAX_2FA_STEPS = 3

log = logging.getLogger("bourso_mcp.sync")
T = TypeVar("T")


class SyncAlreadyRunning(Exception):
    def __init__(self, run: RunRow | None) -> None:
        super().__init__("une synchronisation est déjà en cours")
        self.run = run


class SyncTooSoon(Exception):
    def __init__(self, last_success_at: datetime) -> None:
        super().__init__("dernière synchronisation réussie il y a moins d'une heure")
        self.last_success_at = last_success_at


class TwoFactorCodeRejected(SourceError):
    """Code 2FA refusé par la source lors de `submit_2fa`."""


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
    if isinstance(exc, TwoFactorRequired | TwoFactorExpired | TwoFactorCodeRejected):
        return "auth_required", type(exc).__name__, str(exc)
    if isinstance(exc, AuthFailed):
        return "auth_failed", type(exc).__name__, str(exc)
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

    def check_can_start(self, force: bool) -> None:
        """Lève SyncAlreadyRunning ou SyncTooSoon ; ne crée pas de source."""
        self._acquire_lock().release()
        if force:
            return
        last = self._repo.last_success_at()
        if last is not None and self._now() - last < RATE_LIMIT:
            raise SyncTooSoon(last)

    def _acquire_lock(self) -> FileLock:
        self._settings.home.mkdir(parents=True, exist_ok=True)
        lock = FileLock(self._settings.lock_path, timeout=0)
        try:
            lock.acquire()
        except Timeout:
            raise SyncAlreadyRunning(self._repo.current_run()) from None
        return lock

    def run(
        self, scope: Scope, trigger: Trigger, two_factor: TwoFactorHandler, *, force: bool = False
    ) -> SyncRunResult:
        self.check_can_start(force)
        lock = self._acquire_lock()
        try:
            self._repo.mark_interrupted_runs(self._now())
            run_id = self._repo.start_run(trigger, scope, self._now())
            log.info("synchro %d démarrée (portée %s)", run_id, scope)
            try:
                result = self._run(run_id, scope, two_factor)
            except Exception as exc:  # un bogue ne doit pas laisser la synchro « running »
                self._repo.finish_run(run_id, "error", self._now(), 0, 0, type(exc).__name__, None)
                log.info("synchro %d : statut error (%s)", run_id, type(exc).__name__)
                raise
            log.info(
                "synchro %d terminée : statut %s, %d opérations, %d relevés ajoutés",
                run_id,
                result.status,
                result.added_transactions,
                result.added_statements,
            )
            return result
        finally:
            lock.release()

    def dry_run(self, two_factor: TwoFactorHandler) -> list[DryRunLine]:
        """Même connexion que `run` (2FA, nouvelles tentatives), mais rien n'est écrit."""
        lock = self._acquire_lock()
        try:
            source = self._login(None, two_factor)
            return [self._dry_line(source, a) for a in source.list_accounts()]
        finally:
            lock.release()

    def _dry_line(self, source: BankSource, account: Account) -> DryRunLine:
        since = self._since(account.id)
        txs = self._retry(lambda: list(source.iter_transactions(account.id, since)))
        pending = self._retry(lambda: list(source.iter_pending(account.id)))
        statements = self._retry(lambda: source.list_statements(account.id))
        return DryRunLine(
            account_id=account.id,
            label=account.label,
            type=account.type,
            transactions=len(txs),
            pending=len(pending),
            statements=len(statements),
        )

    def _run(self, run_id: int, scope: Scope, two_factor: TwoFactorHandler) -> SyncRunResult:
        try:
            source = self._login(run_id, two_factor)
            accounts = source.list_accounts()
        except SourceError as exc:
            status, error_type, message = _status_for_error(exc)
            return self._finish(run_id, status, [], error_type, message)
        self._repo.upsert_accounts(accounts, self._now())
        outcomes = [self._sync_account(source, a, scope) for a in accounts]
        status, error_type, message = self._final_status(outcomes)
        return self._finish(run_id, status, outcomes, error_type, message)

    def _retry(self, call: Callable[[], T]) -> T:
        """Retente sur NetworkError seulement, avec les attentes de RETRY_DELAYS."""
        for delay in RETRY_DELAYS:
            try:
                return call()
            except NetworkError:
                log.info("erreur réseau, nouvelle tentative dans %ss", delay)
                self._sleep(delay)
        return call()

    def _set_status(self, run_id: int | None, status: RunStatus, method: str | None = None) -> None:
        if run_id is not None:  # None : essai à blanc, rien n'est écrit
            self._repo.set_run_status(run_id, status, method)

    def _login(self, run_id: int | None, two_factor: TwoFactorHandler) -> BankSource:
        source = self._source_factory()
        try:
            self._retry(source.login)
        except TwoFactorRequired as required:
            self._two_factor_steps(source, run_id, required.method, two_factor)
        return source

    def _two_factor_steps(
        self,
        source: BankSource,
        run_id: int | None,
        method: TwoFactorMethod,
        two_factor: TwoFactorHandler,
    ) -> None:
        used: list[TwoFactorMethod] = []
        while True:
            if method in used or len(used) >= MAX_2FA_STEPS:
                raise TwoFactorRequired(method)
            used.append(method)
            self._set_status(run_id, "awaiting_2fa", method)
            log.info("authentification forte demandée (%s)", method)
            code = two_factor(method)
            try:
                source.submit_2fa(code)  # jamais de nouvelle tentative ici
            except TwoFactorRequired as chained:
                method = chained.method
                continue
            except AuthFailed:
                raise TwoFactorCodeRejected("code 2FA refusé, relancer la connexion") from None
            self._set_status(run_id, "running")
            return

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
        txs = self._retry(lambda: list(source.iter_transactions(account_id, since)))
        added = self._repo.upsert_transactions(account_id, txs, self._now())
        pending = self._retry(lambda: list(source.iter_pending(account_id)))
        self._repo.replace_pending(account_id, pending, self._now())
        return added

    def _statements_step(self, source: BankSource, account_id: str) -> int:
        home = self._settings.home
        added = 0
        for ref in self._retry(lambda: source.list_statements(account_id)):
            if not self._repo.statement_needs_download(ref.id, home):
                continue
            data = self._retry(lambda ref=ref: source.download_statement(ref))
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
