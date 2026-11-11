"""Synchro en tâche de fond pour le serveur MCP : `start` rend la main aussitôt."""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from bourso_mcp.source.errors import TwoFactorExpired
from bourso_mcp.source.models import TwoFactorMethod
from bourso_mcp.store.repository import Repository, RunRow, Scope
from bourso_mcp.sync.service import (
    SyncAlreadyRunning,
    SyncRunResult,
    SyncService,
    SyncTooSoon,
)

log = logging.getLogger("bourso_mcp.sync")


@dataclass(frozen=True)
class StartResult:
    started: bool
    reason: Literal["started", "already_running", "too_soon"]
    last_success_at: datetime | None


def _run_dict(run: RunRow | None) -> dict[str, Any] | None:
    if run is None:
        return None
    out: dict[str, Any] = {}
    for field in dataclasses.fields(run):
        value = getattr(run, field.name)
        out[field.name] = value.isoformat() if isinstance(value, datetime) else value
    return out


class BackgroundSync:
    def __init__(
        self, service: SyncService, repo: Repository, *, code_timeout: float = 600.0
    ) -> None:
        self._service = service
        self._repo = repo
        self._code_timeout = code_timeout
        self._start_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._result: SyncRunResult | None = None
        self._codes: queue.Queue[str] = queue.Queue()
        self._code_lock = threading.Lock()
        self._waiting: TwoFactorMethod | None = None

    def start(self, scope: Scope, force: bool = False) -> StartResult:
        with self._start_lock:
            if self._worker is not None and self._worker.is_alive():
                return StartResult(False, "already_running", self._repo.last_success_at())
            try:
                self._service.check_can_start(force)
            except SyncTooSoon as exc:
                return StartResult(False, "too_soon", exc.last_success_at)
            except SyncAlreadyRunning:
                return StartResult(False, "already_running", self._repo.last_success_at())
            self._result = None
            worker = threading.Thread(
                target=self._work, args=(scope, force), name="bourso-sync", daemon=True
            )
            self._worker = worker
            worker.start()
            return StartResult(True, "started", self._repo.last_success_at())

    def submit_code(self, code: str) -> bool:
        cleaned = "".join(code.split())
        if not cleaned:
            return False
        with self._code_lock:
            if self._waiting is None:
                return False
            self._codes.put(cleaned)
            return True

    def status(self) -> dict[str, Any]:
        last_success = self._repo.last_success_at()
        return {
            "current": _run_dict(self._repo.current_run()),
            "last": _run_dict(self._repo.last_finished_run()),
            "last_success_at": None if last_success is None else last_success.isoformat(),
        }

    def wait(self, timeout: float) -> SyncRunResult | None:
        worker = self._worker
        if worker is None:
            return self._result
        worker.join(timeout)
        if worker.is_alive():
            return None
        return self._result

    def _work(self, scope: Scope, force: bool) -> None:
        try:
            self._result = self._service.run(scope, "mcp", self._two_factor, force=force)
        except (SyncAlreadyRunning, SyncTooSoon) as exc:
            log.info("synchro non lancée : %s", type(exc).__name__)
        except BaseException as exc:  # noqa: BLE001 - le passage est déjà clos ; rien ne sort du thread
            log.error("synchro en tâche de fond interrompue (%s)", type(exc).__name__)

    def _two_factor(self, method: TwoFactorMethod) -> str | None:
        if method == "app":
            return None  # la source attend elle-même la validation dans l'appli
        with self._code_lock:
            while True:
                try:
                    self._codes.get_nowait()  # codes périmés d'un passage précédent
                except queue.Empty:
                    break
            self._waiting = method
        try:
            return self._codes.get(timeout=self._code_timeout)
        except queue.Empty:
            raise TwoFactorExpired("aucun code 2FA reçu à temps") from None
        finally:
            with self._code_lock:
                self._waiting = None
