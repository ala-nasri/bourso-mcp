"""Ligne de commande `bourso-mcp` : login, sync, status."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from bourso_mcp.cli import wiring
from bourso_mcp.config import Settings, load_settings
from bourso_mcp.source.errors import AuthFailed, SourceError, TwoFactorExpired, TwoFactorRequired
from bourso_mcp.store.repository import Repository
from bourso_mcp.sync.service import (
    SyncAlreadyRunning,
    SyncRunResult,
    SyncService,
    SyncTooSoon,
    TwoFactorHandler,
)
from bourso_mcp.sync.twofactor import interactive_handler, non_interactive_handler

EXIT_OK, EXIT_FAILED, EXIT_AUTH, EXIT_REFUSED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
README_HINT = "Voir la section « Configuration » du README."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}s" if n >= 2 else f"{n} {word}"


def _local(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")


def _summary(result: SyncRunResult) -> str:
    tx = _plural(result.added_transactions, "transaction")
    st = _plural(result.added_statements, "relevé")
    return f"{result.status} : {tx} et {st} ajoutés"


def _report(result: SyncRunResult) -> int:
    status = result.status
    if status == "ok":
        print(_summary(result))
        return EXIT_OK
    if status == "partial":
        print(_summary(result))
        for outcome in result.accounts:
            if not outcome.ok:
                print(f"{outcome.account_id} : {outcome.error_type}")
        return EXIT_FAILED
    if status == "auth_failed":
        print(f"auth_failed : {result.error_message}")
        print(README_HINT)
        return EXIT_AUTH
    if status == "auth_required":
        print(f"auth_required : {result.error_type}, {result.error_message}")
        print("Lance `bourso-mcp login` pour valider l'authentification forte.")
        return EXIT_AUTH
    print(f"{status} : {result.error_type}, {result.error_message}")
    return EXIT_FAILED


def _refused(exc: SyncTooSoon | SyncAlreadyRunning) -> int:
    if isinstance(exc, SyncTooSoon):
        print(
            f"Dernière synchronisation réussie le {_local(exc.last_success_at)} : "
            "relance avec --force pour passer outre."
        )
    else:
        print("Une synchronisation est déjà en cours.")
    return EXIT_REFUSED


def _dry_run(service: SyncService, handler: TwoFactorHandler) -> int:
    try:
        lines = service.dry_run(handler)
    except SyncAlreadyRunning as exc:
        return _refused(exc)
    except (AuthFailed, TwoFactorRequired, TwoFactorExpired) as exc:
        print(str(exc))
        return EXIT_AUTH
    except SourceError as exc:
        print(f"{type(exc).__name__} : {exc}")
        return EXIT_FAILED
    for line in lines:
        print(
            f"{line.account_id} : {line.label} ({line.type}) : {line.transactions} opérations, "
            f"{line.pending} en attente, {line.statements} relevés"
        )
    return EXIT_OK


def _run(
    service: SyncService, scope, trigger, handler: TwoFactorHandler, *, force: bool
) -> int:
    try:
        result = service.run(scope, trigger, handler, force=force)
    except (SyncTooSoon, SyncAlreadyRunning) as exc:
        return _refused(exc)
    return _report(result)


def _cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    service = wiring.build_service(settings, wiring.source_factory(settings))
    return _run(service, "all", "cli", interactive_handler(), force=True)


def _cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    service = wiring.build_service(settings, wiring.source_factory(settings))
    handler = non_interactive_handler if args.non_interactive else interactive_handler()
    if args.dry_run:
        return _dry_run(service, handler)
    trigger = "schedule" if args.non_interactive else "cli"
    return _run(service, args.scope, trigger, handler, force=args.force)


def _cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    repo = Repository(settings.db_path)
    current = repo.current_run()
    if current is None:
        print("En cours : aucune synchronisation")
    else:
        method = f", méthode {current.twofa_method}" if current.twofa_method else ""
        print(f"En cours : {current.status}{method}, démarrée le {_local(current.started_at)}")
    last = repo.last_finished_run()
    if last is None:
        print("Dernier passage : aucun")
    else:
        error = f", erreur {last.error_type}" if last.error_type else ""
        print(
            f"Dernier passage : {last.status} le {_local(last.finished_at)}, "
            f"{_plural(last.added_transactions, 'transaction')} et "
            f"{_plural(last.added_statements, 'relevé')} ajoutés{error}"
        )
    success = repo.last_success_at()
    print(f"Dernière réussite : {_local(success) if success else 'aucune'}")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bourso-mcp", description="Synchronisation BoursoBank vers SQLite et PDF."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser(
        "login", help="ouvre la session (2FA) et lance une première synchronisation"
    )
    login.set_defaults(func=_cmd_login)
    sync = sub.add_parser("sync", help="synchronise comptes, opérations et relevés")
    sync.add_argument("--scope", choices=["all", "transactions", "statements"], default="all")
    sync.add_argument("--force", action="store_true", help="ignore la limite d'une par heure")
    sync.add_argument(
        "--non-interactive", action="store_true", help="mode planifié : jamais de saisie"
    )
    sync.add_argument("--dry-run", action="store_true", help="compte par compte, sans écriture")
    sync.set_defaults(func=_cmd_sync)
    status = sub.add_parser("status", help="affiche l'état des synchronisations")
    status.set_defaults(func=_cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    logging.getLogger("woob").setLevel(logging.WARNING)
    args = _parser().parse_args(argv)
    settings = load_settings()
    try:
        return args.func(settings, args)
    except KeyboardInterrupt:
        print("Interrompu.")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
