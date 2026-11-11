"""Gestionnaires de code 2FA : saisie au terminal, ou refus immédiat en mode planifié."""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

from bourso_mcp.source.errors import TwoFactorExpired
from bourso_mcp.source.models import TwoFactorMethod
from bourso_mcp.sync.service import TwoFactorHandler

_PROMPTS: dict[str, str] = {"sms": "Code reçu par SMS : ", "email": "Code reçu par e-mail : "}


def interactive_handler(
    prompt: Callable[[str], str] = input, out: Callable[[str], None] = print
) -> TwoFactorHandler:
    """Pas de délai : l'utilisateur est au terminal (Ctrl+C interrompt la synchro)."""

    def handler(method: TwoFactorMethod) -> str | None:
        if method == "app":
            out("Valide la connexion dans l'appli BoursoBank…")
            return None
        return "".join(prompt(_PROMPTS[method]).split())

    return handler


def non_interactive_handler(method: TwoFactorMethod) -> NoReturn:
    raise TwoFactorExpired(f"authentification forte requise ({method}) en mode non interactif")
