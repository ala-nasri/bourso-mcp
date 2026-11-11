"""Identifiants BoursoBank : lecture seule de l'entrée générique du Gestionnaire d'identification.

L'utilisateur crée et met à jour l'entrée lui-même (CMDKEY_HINT). Ce module ne fait que la lire :
il n'écrit rien, ne stocke rien et ne met jamais les valeurs dans un message.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import keyring
from keyring.errors import KeyringError

from bourso_mcp.source.errors import AuthFailed

CREDENTIAL_TARGET = "bourso-mcp"
CMDKEY_HINT = "cmdkey /generic:bourso-mcp /user:<identifiant> /pass"


@dataclass(frozen=True)
class Credentials:
    login: str = field(repr=False)
    password: str = field(repr=False)


class CredentialsError(AuthFailed):
    """Entrée absente, illisible ou mal formée : aucun appel à BoursoBank, pas de nouvel essai."""


def read_windows_credential(target: str) -> tuple[str, str] | None:
    """(identifiant, mot de passe) de l'entrée générique `target`, ou None si elle est absente."""
    credential = keyring.get_credential(target, None)
    if credential is None:
        return None
    return credential.username, credential.password


def _error(problem: str) -> CredentialsError:
    return CredentialsError(
        f"{problem} Créez ou corrigez l'entrée du Gestionnaire d'identification Windows avec : "
        f"{CMDKEY_HINT}"
    )


def load_credentials(
    reader: Callable[[str], tuple[str, str] | None] = read_windows_credential,
) -> Credentials:
    try:
        entry = reader(CREDENTIAL_TARGET)
    except KeyringError as exc:
        raise _error(
            f"Lecture de l'entrée « {CREDENTIAL_TARGET} » impossible ({type(exc).__name__})."
        ) from None
    if entry is None:
        raise _error(f"Entrée « {CREDENTIAL_TARGET} » absente.")
    login, password = entry
    if not login or not re.fullmatch(r"[0-9]+", login):
        raise _error(
            f"L'identifiant de l'entrée « {CREDENTIAL_TARGET} » doit être l'identifiant "
            "BoursoBank, composé uniquement de chiffres."
        )
    if not password:
        raise _error(f"Le mot de passe de l'entrée « {CREDENTIAL_TARGET} » est vide.")
    return Credentials(login, password)
