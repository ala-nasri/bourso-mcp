"""`WoobBoursoSource` : le backend woob `boursorama`, utilisé comme bibliothèque, en lecture seule.

Suit les notes (a), (b), (f) et (j) du README : chargement par `WoobBase`, 2FA interactive
reprise par la configuration du backend, relevés liés aux comptes par la subscription de même id,
état de session dans un stockage chiffré.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

from woob.capabilities.base import empty
from woob.core import WoobBase

from bourso_mcp.credentials import Credentials
from bourso_mcp.source.errors import AuthFailed, SourceBroken, SourceError, TwoFactorRequired
from bourso_mcp.source.models import Account, StatementRef, Transaction, TwoFactorMethod
from bourso_mcp.source.woob_mapping import (
    to_account,
    to_statement_ref,
    to_transaction,
    translate_error,
)
from bourso_mcp.source.woob_storage import EncryptedWoobStorage

VENDOR_WOOB = Path(__file__).resolve().parents[3] / "vendor" / "woob"
MODULE = "boursorama"
STATE_FILE = "state.bin"
# Seules méthodes du backend que la source appelle : comptes, historique, à venir, documents.
ALLOWED_BACKEND_CALLS = frozenset({"iter_accounts", "iter_history", "iter_coming",
                                   "iter_subscription", "iter_documents", "download_document"})
# Champ de configuration woob qui reçoit la réponse à la 2FA (note b, AUTHENTICATION_METHODS).
_ANSWER_FIELDS: dict[TwoFactorMethod, str] = {"sms": "code", "email": "email_code", "app": "resume"}

log = logging.getLogger("bourso_mcp.source")


def build_backend(
    modules_path: Path, params: dict[str, Any], storage: EncryptedWoobStorage
) -> Any:
    """Note (a) : le stockage se passe à `load_backend`, pas à `WoobBase`. Aucune requête ici."""
    woob = WoobBase(modules_path=str(modules_path))
    return woob.load_backend(MODULE, MODULE, params, storage=storage)


class WoobBoursoSource:
    """Respecte `BankSource`. Le backend n'est créé qu'au premier appel : construire la source
    ne lit rien et ne contacte pas la banque."""

    def __init__(
        self,
        credentials: Credentials,
        woob_dir: Path,
        modules_path: Path = VENDOR_WOOB / "modules",
        backend_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._credentials = credentials
        self._woob_dir = woob_dir
        self._backend_factory = backend_factory or partial(build_backend, modules_path)
        self._backend: Any = None
        self._accounts: dict[str, Any] = {}
        self._subscriptions: dict[str, Any] | None = None
        self._pending_2fa: TwoFactorMethod | None = None

    # ------------------------------------------------------------------ backend woob

    def _get_backend(self) -> Any:
        if self._backend is None:
            params = {
                "login": self._credentials.login,
                "password": self._credentials.password,
                "request_information": {},  # non nul avant le navigateur : 2FA interactive (b)
            }
            storage = EncryptedWoobStorage(self._woob_dir / STATE_FILE)
            self._backend = self._backend_factory(params=params, storage=storage)
        return self._backend

    def _call(self, name: str, *args: Any) -> Any:
        if name not in ALLOWED_BACKEND_CALLS:
            raise SourceBroken(f"appel woob non autorisé (lecture seule) : {name}")
        return getattr(self._get_backend(), name)(*args)

    def _scrub(self, text: str) -> str:
        for secret in (self._credentials.password, self._credentials.login):
            if secret:
                text = text.replace(secret, "***")
        return text

    @contextmanager
    def _woob_errors(self) -> Iterator[None]:
        """Traduit les exceptions woob ; une exception non woob est relancée telle quelle."""
        try:
            yield
        except SourceError:
            raise
        except Exception as exc:
            error = translate_error(exc)
            if error is None:
                raise
            if not isinstance(error, TwoFactorRequired):  # message fixe, sans valeur
                error = type(error)(self._scrub(str(error)))
            raise error from None

    def _save_state(self) -> None:
        """Écrit l'état de session (cookies, reconnaissance de l'appareil) dans state.bin.

        `dump_state` ne contacte pas la banque : il sérialise le navigateur dans le stockage local.
        Un échec n'empêche pas la synchro : il redemandera seulement la 2FA plus tôt.
        """
        try:
            self._get_backend().dump_state()
        except Exception as exc:  # noqa: BLE001 - session ouverte : ne jamais échouer ici
            log.warning(
                "état de session woob non sauvegardé (%s) : la 2FA pourra être redemandée",
                type(exc).__name__,
            )

    def _clear_answers(self) -> None:
        """Une réponse 2FA n'est jamais rejouée : le champ revient à sa valeur par défaut."""
        if self._backend is None:
            return
        for name in _ANSWER_FIELDS.values():
            value = self._backend.config.get(name)
            if value is not None:
                value.set(value.default)

    # ------------------------------------------------------------------ session

    def _open_session(self) -> None:
        """Premier vrai appel : ouvre la session (2FA éventuelle) et met les comptes en cache."""
        self._pending_2fa = None
        try:
            with self._woob_errors():
                accounts = list(self._call("iter_accounts"))
        except TwoFactorRequired as required:
            self._pending_2fa = required.method
            raise
        self._accounts = {a.id: a for a in accounts}
        self._save_state()

    def login(self) -> None:
        self._open_session()

    def submit_2fa(self, code: str | None) -> None:
        method = self._pending_2fa
        if method is None:
            raise SourceBroken("submit_2fa sans 2FA en attente : appeler login() d'abord")
        if method != "app" and not code:
            self._pending_2fa = None
            raise AuthFailed("code 2FA vide")
        answer = True if method == "app" else code
        try:
            with self._woob_errors():
                self._get_backend().config[_ANSWER_FIELDS[method]].set(answer)
            self._open_session()  # reprise de l'appel interrompu par la 2FA
        finally:
            self._clear_answers()

    # ------------------------------------------------------------------ lecture

    def _account(self, account_id: str) -> Any:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise SourceBroken(f"compte absent de la session woob : {account_id}") from None

    def list_accounts(self) -> list[Account]:
        accounts = (to_account(a) for a in self._accounts.values())
        return [a for a in accounts if a is not None]

    def iter_transactions(self, account_id: str, since: date | None) -> Iterator[Transaction]:
        account = self._account(account_id)
        with self._woob_errors():
            for item in self._call("iter_history", account):
                tx = to_transaction(item)
                if since is not None and tx.debit_date < since:
                    return  # note (h) : du plus récent au plus ancien, la suite est plus ancienne
                yield tx

    def iter_pending(self, account_id: str) -> Iterator[Transaction]:
        account = self._account(account_id)
        with self._woob_errors():
            for item in self._call("iter_coming", account):
                yield to_transaction(item)

    def list_statements(self, account_id: str) -> list[StatementRef]:
        with self._woob_errors():
            if self._subscriptions is None:
                self._subscriptions = {s.id: s for s in self._call("iter_subscription")}
            subscription = self._subscriptions.get(account_id)  # note (f) : même id que le compte
            if subscription is None:
                return []
            documents = list(self._call("iter_documents", subscription))
        # Le RIB n'a pas de date : il ne peut pas devenir un relevé daté.
        return [to_statement_ref(account_id, d) for d in documents if not empty(d.date)]

    def download_statement(self, ref: StatementRef) -> bytes:
        if ref.handle is None:
            raise SourceBroken(f"relevé {ref.id} sans document woob : relister les relevés")
        with self._woob_errors():
            data = self._call("download_document", ref.handle)
        if not isinstance(data, bytes) or not data:
            raise SourceBroken(f"téléchargement vide ou invalide pour le relevé {ref.id}")
        return data
