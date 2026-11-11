from __future__ import annotations

import pytest

from bourso_mcp.source.errors import TwoFactorExpired
from bourso_mcp.sync.twofactor import interactive_handler, non_interactive_handler


def test_interactive_handler_prompts_and_strips_spaces():
    prompts = []

    def prompt(text):
        prompts.append(text)
        return " 123 456 "

    handler = interactive_handler(prompt, lambda text: None)
    assert handler("sms") == "123456"
    assert handler("email") == "123456"
    assert prompts == ["Code reçu par SMS : ", "Code reçu par e-mail : "]


def test_interactive_handler_app_returns_none_and_prints_instruction():
    printed = []

    def prompt(text):
        raise AssertionError("aucune saisie pour la méthode app")

    handler = interactive_handler(prompt, printed.append)
    assert handler("app") is None
    assert printed == ["Valide la connexion dans l'appli BoursoBank…"]


def test_non_interactive_handler_raises():
    with pytest.raises(TwoFactorExpired):
        non_interactive_handler("sms")
