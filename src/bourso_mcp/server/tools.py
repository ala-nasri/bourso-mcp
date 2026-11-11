"""Logique des outils MCP (spec §7) : validation, appels au store et au runner, réponses JSON.

Aucun import MCP ici : `app.py` enregistre ces méthodes auprès du SDK. Les annotations sont
évaluées à l'exécution par le SDK (pas de `from __future__ import annotations`).
"""

import re
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, get_args

from bourso_mcp.config import Settings
from bourso_mcp.source.models import TxType
from bourso_mcp.store.export import export_csv
from bourso_mcp.store.money import cents_to_str, euros_to_cents
from bourso_mcp.store.queries import GroupBy, Sort, TransactionFilter, search, summarize
from bourso_mcp.store.repository import Repository
from bourso_mcp.store.statements import (
    StatementFileMissing,
    StatementNotFound,
    StatementUnreadable,
    list_statements,
    read_statement,
)
from bourso_mcp.sync.runner import BackgroundSync

TOOL_NAMES = (
    "list_accounts",
    "search_transactions",
    "summarize_transactions",
    "export_transactions",
    "list_statements",
    "read_statement",
    "sync_now",
    "sync_status",
    "submit_2fa_code",
)

MAX_LIMIT = 500
SORTS = get_args(Sort)
GROUP_BYS = get_args(GroupBy)
TX_TYPES = get_args(TxType)
SCOPES = ("all", "transactions", "statements")
DATE_FIELDS = ("operation", "debit")
DIRECTIONS = ("debit", "credit")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PERIOD_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_AUTH_STATUSES = ("auth_required", "auth_failed")


class ToolInputError(ValueError):
    """Paramètre invalide : le message, en français, nomme le paramètre et les valeurs permises."""


def _choice(param: str, value: str, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise ToolInputError(
            f"{param} : « {value} » n'est pas une valeur permise ({', '.join(allowed)})."
        )
    return value


def _parse_date(param: str, value: str | None) -> date | None:
    if value is None:
        return None
    if _DATE_RE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ToolInputError(f"{param} : « {value} » n'est pas une date valide (format AAAA-MM-JJ).")


def _parse_euros(param: str, value: float | str | None) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, bool) or not Decimal(str(value)).is_finite():
            raise InvalidOperation
        cents = euros_to_cents(value)
    except InvalidOperation:
        raise ToolInputError(
            f"{param} : « {value} » n'est pas un montant en euros (nombre positif, ex. 12.50)."
        ) from None
    if cents < 0:
        raise ToolInputError(
            f"{param} : doit être positif ({value}) ; le sens se filtre avec direction "
            "(debit ou credit)."
        )
    return cents


def parse_filter(
    *,
    account_ids: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_field: str = "operation",
    amount_min: float | str | None = None,
    amount_max: float | str | None = None,
    direction: str | None = None,
    text: str | None = None,
    types: list[str] | None = None,
    category: str | None = None,
    card: str | None = None,
    include_pending: bool = True,
) -> TransactionFilter:
    start, end = _parse_date("date_from", date_from), _parse_date("date_to", date_to)
    if start and end and start > end:
        raise ToolInputError(f"date_from : {start} est postérieure à date_to ({end}).")
    low, high = _parse_euros("amount_min", amount_min), _parse_euros("amount_max", amount_max)
    if low is not None and high is not None and low > high:
        raise ToolInputError(f"amount_min : {amount_min} dépasse amount_max ({amount_max}).")
    if direction is not None:
        _choice("direction", direction, DIRECTIONS)
    tx_types = tuple(_choice("types", t, TX_TYPES) for t in types) if types else None
    return TransactionFilter(
        account_ids=tuple(account_ids) if account_ids else None,
        date_from=start,
        date_to=end,
        date_field=_choice("date_field", date_field, DATE_FIELDS),  # type: ignore[arg-type]
        amount_min_cents=low,
        amount_max_cents=high,
        direction=direction,  # type: ignore[arg-type]
        text=text or None,
        types=tx_types,  # type: ignore[arg-type]
        category=category or None,
        card=card or None,
        include_pending=include_pending,
    )


def _period(param: str, value: str | None) -> str | None:
    if value is not None and not _PERIOD_RE.fullmatch(value):
        raise ToolInputError(f"{param} : « {value} » n'est pas une période valide (AAAA-MM).")
    return value


def _clamp_limit(limit: int) -> int:
    return max(1, min(MAX_LIMIT, limit))


def _totals(debits: int, credits: int) -> dict[str, str]:
    return {
        "total_debits": cents_to_str(debits),
        "total_credits": cents_to_str(credits),
        "net": cents_to_str(debits + credits),
    }


def _item(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "operation_date": row["operation_date"],
        "debit_date": row["debit_date"],
        "value_date": row["value_date"],
        "label": row["label"],
        "raw_label": row["raw_label"],
        "amount": cents_to_str(row["amount_cents"]),
        "type": row["type"],
        "category": row["category"],
        "card": row["card"],
        "pending": bool(row["pending"]),
    }


class Tools:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        runner: BackgroundSync,
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._runner = runner
        self._now = now

    def _sync_block(self) -> dict[str, Any]:
        current, last = self._repo.current_run(), self._repo.last_finished_run()
        success = self._repo.last_success_at()
        awaiting = current is not None and current.status == "awaiting_2fa"
        failed = last is not None and last.status in _AUTH_STATUSES
        return {
            "last_success_at": success.isoformat() if success else None,
            "last_status": last.status if last else None,
            "auth_required": awaiting or failed,
        }

    # --- lecture -------------------------------------------------------------------------

    def list_accounts(self) -> dict[str, Any]:
        accounts = [
            {
                "id": r["id"],
                "label": r["label"],
                "type": r["type"],
                "ownership": r["ownership"],
                "parent_id": r["parent_id"],
                "currency": r["currency"],
                "balance": None if r["balance_cents"] is None else cents_to_str(r["balance_cents"]),
                "balance_at": r["balance_at"],
            }
            for r in self._repo.list_accounts()
        ]
        return {"accounts": accounts, "sync": self._sync_block()}

    def search_transactions(
        self,
        account_ids: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        date_field: str = "operation",
        amount_min: float | str | None = None,
        amount_max: float | str | None = None,
        direction: str | None = None,
        text: str | None = None,
        types: list[str] | None = None,
        category: str | None = None,
        card: str | None = None,
        include_pending: bool = True,
        sort: str = "date_desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        f = parse_filter(
            account_ids=account_ids, date_from=date_from, date_to=date_to, date_field=date_field,
            amount_min=amount_min, amount_max=amount_max, direction=direction, text=text,
            types=types, category=category, card=card, include_pending=include_pending,
        )  # fmt: skip
        _choice("sort", sort, SORTS)
        if offset < 0:
            raise ToolInputError(f"offset : doit être supérieur ou égal à 0 (reçu {offset}).")
        limit = _clamp_limit(limit)
        result = search(self._settings.db_path, f, sort, limit, offset)  # type: ignore[arg-type]
        t = result.totals
        return {
            "items": [_item(r) for r in result.items],
            "total_count": t.count,
            **_totals(t.debits_cents, t.credits_cents),
            "limit": limit,
            "offset": offset,
            "sync": self._sync_block(),
        }

    def summarize_transactions(
        self,
        group_by: str = "month",
        account_ids: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        date_field: str = "operation",
        amount_min: float | str | None = None,
        amount_max: float | str | None = None,
        direction: str | None = None,
        text: str | None = None,
        types: list[str] | None = None,
        category: str | None = None,
        card: str | None = None,
        include_pending: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        _choice("group_by", group_by, GROUP_BYS)
        f = parse_filter(
            account_ids=account_ids, date_from=date_from, date_to=date_to, date_field=date_field,
            amount_min=amount_min, amount_max=amount_max, direction=direction, text=text,
            types=types, category=category, card=card, include_pending=include_pending,
        )  # fmt: skip
        limit = _clamp_limit(limit)
        groups = summarize(self._settings.db_path, f, group_by, limit)  # type: ignore[arg-type]
        return {
            "group_by": group_by,
            "groups": [
                {
                    "key": g.key,
                    "count": g.totals.count,
                    **_totals(g.totals.debits_cents, g.totals.credits_cents),
                }
                for g in groups
            ],
            "limit": limit,
            "sync": self._sync_block(),
        }

    def export_transactions(
        self,
        account_ids: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        date_field: str = "operation",
        amount_min: float | str | None = None,
        amount_max: float | str | None = None,
        direction: str | None = None,
        text: str | None = None,
        types: list[str] | None = None,
        category: str | None = None,
        card: str | None = None,
        include_pending: bool = True,
    ) -> dict[str, Any]:
        f = parse_filter(
            account_ids=account_ids, date_from=date_from, date_to=date_to, date_field=date_field,
            amount_min=amount_min, amount_max=amount_max, direction=direction, text=text,
            types=types, category=category, card=card, include_pending=include_pending,
        )  # fmt: skip
        path, rows = export_csv(self._settings.db_path, f, self._settings.exports_dir, self._now())
        return {"path": str(path), "rows": rows, "sync": self._sync_block()}

    def list_statements(
        self,
        account_ids: list[str] | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict[str, Any]:
        start, end = _period("period_from", period_from), _period("period_to", period_to)
        if start and end and start > end:
            raise ToolInputError(f"period_from : {start} est postérieure à period_to ({end}).")
        rows = list_statements(
            self._settings.db_path, tuple(account_ids) if account_ids else None, start, end
        )
        return {
            "statements": [
                {
                    "id": r["id"],
                    "account_id": r["account_id"],
                    "period": r["period"],
                    "date": r["date"],
                    "doc_type": r["doc_type"],
                    "path": str(self._settings.home / r["file_path"]),
                }
                for r in rows
            ],
            "sync": self._sync_block(),
        }

    def read_statement(self, statement_id: str) -> dict[str, Any]:
        try:
            doc = read_statement(self._settings.db_path, self._settings.home, statement_id)
        except StatementNotFound:
            raise ToolInputError(
                f"statement_id : aucun relevé ne porte l'identifiant « {statement_id} » "
                "(voir list_statements)."
            ) from None
        except StatementFileMissing:
            raise ToolInputError(
                "Le fichier PDF de ce relevé est absent du disque. Lance "
                'sync_now(scope="statements") pour le télécharger à nouveau.'
            ) from None
        except StatementUnreadable:
            raise ToolInputError(
                "Le PDF de ce relevé est illisible ou corrompu. Lance "
                'sync_now(scope="statements") pour tenter de le récupérer.'
            ) from None
        row = doc.row
        out: dict[str, Any] = {
            "id": row["id"],
            "account_id": row["account_id"],
            "period": row["period"],
            "date": row["date"],
            "doc_type": row["doc_type"],
            "path": str(doc.path),
            "pages": doc.pages,
            "has_text": doc.has_text,
            "text": doc.text,
        }
        if not doc.has_text:
            out["message"] = "Aucun texte extractible : le PDF est probablement une image."
        out["sync"] = self._sync_block()
        return out

    # --- synchro et authentification ------------------------------------------------------

    def sync_now(self, scope: str = "all", force: bool = False) -> dict[str, Any]:
        _choice("scope", scope, SCOPES)
        res = self._runner.start(scope, force)  # type: ignore[arg-type]
        if res.started:
            return {
                "started": True,
                "scope": scope,
                "message": "Synchronisation lancée en tâche de fond ; suis-la avec sync_status. "
                "Si l'authentification forte est demandée, sync_status l'indique "
                "(méthode attendue) : transmets alors le code avec submit_2fa_code.",
            }
        if res.reason == "already_running":
            return {
                "started": False,
                "reason": "already_running",
                "current": self._runner.status()["current"],
                "message": "Une synchronisation est déjà en cours (voir current).",
            }
        return {
            "started": False,
            "reason": "too_soon",
            "last_success_at": res.last_success_at.isoformat() if res.last_success_at else None,
            "message": "La dernière synchronisation réussie date de moins d'une heure ; "
            "relance avec force=true pour passer outre.",
        }

    def sync_status(self) -> dict[str, Any]:
        return {**self._runner.status(), "sync": self._sync_block()}

    def submit_2fa_code(self, code: str) -> dict[str, Any]:
        if self._runner.submit_code(code):
            return {"accepted": True, "message": "Code transmis ; suis la suite avec sync_status."}
        return {
            "accepted": False,
            "reason": "Aucun passage n'attend un code SMS ou e-mail en ce moment (ou le code "
            "vient d'être pris en compte, ou il est en cours de vérification). Vérifie avec "
            "sync_status.",
        }
