"""Erreurs levées par une source bancaire."""

from __future__ import annotations

from bourso_mcp.source.models import TwoFactorMethod


class SourceError(Exception):
    """Erreur de base de la source."""


class TwoFactorRequired(SourceError):
    def __init__(self, method: TwoFactorMethod) -> None:
        super().__init__(f"authentification forte requise ({method})")
        self.method = method


class TwoFactorExpired(SourceError):
    """Le code ou la validation 2FA a expiré."""


class AuthFailed(SourceError):
    """Identifiants refusés."""


class AuthMethodUnsupported(SourceError):
    """Méthode d'authentification forte non gérée (QR code par exemple)."""


class SourceBroken(SourceError):
    """Le site ou woob ne se comporte plus comme prévu."""


class NetworkError(SourceError):
    """Erreur réseau, éventuellement passagère."""
