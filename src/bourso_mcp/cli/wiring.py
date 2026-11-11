"""Assemblage du service de synchronisation réel (source woob, base SQLite)."""

from __future__ import annotations

from collections.abc import Callable

from bourso_mcp.config import Settings
from bourso_mcp.credentials import load_credentials, read_windows_credential
from bourso_mcp.source.base import BankSource
from bourso_mcp.source.woob_source import WoobBoursoSource
from bourso_mcp.store.repository import Repository
from bourso_mcp.sync.service import SyncService


def source_factory(
    settings: Settings,
    reader: Callable[[str], tuple[str, str] | None] = read_windows_credential,
) -> Callable[[], BankSource]:
    """Fabrique paresseuse : les identifiants ne sont lus qu'à la création de la source.

    Une CredentialsError remonte à SyncService, qui la range en `auth_failed`.
    """

    def factory() -> BankSource:
        credentials = load_credentials(reader)
        return WoobBoursoSource(credentials, settings.woob_dir)

    return factory


def build_service(
    settings: Settings, factory: Callable[[], BankSource] | None = None
) -> SyncService:
    return SyncService(factory or source_factory(settings), Repository(settings.db_path), settings)
