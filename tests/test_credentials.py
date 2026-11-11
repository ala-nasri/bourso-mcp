from __future__ import annotations

from pathlib import Path

import keyring
import pytest
from keyring.errors import KeyringError

from bourso_mcp import credentials
from bourso_mcp.credentials import (
    CMDKEY_HINT,
    CREDENTIAL_TARGET,
    Credentials,
    CredentialsError,
    load_credentials,
    read_windows_credential,
)
from bourso_mcp.source.errors import AuthFailed


def test_credentials_repr_hides_password():
    text = repr(Credentials("12345678", "secret"))
    assert "secret" not in text
    assert "12345678" in text


def test_read_windows_credential_uses_keyring():
    assert read_windows_credential(CREDENTIAL_TARGET) is None
    keyring.get_keyring().set_password("bourso-mcp", "12345678", "pw")
    assert read_windows_credential(CREDENTIAL_TARGET) == ("12345678", "pw")


def test_load_credentials_from_reader():
    seen = []

    def reader(target):
        seen.append(target)
        return ("12345678", "pw")

    assert load_credentials(reader) == Credentials("12345678", "pw")
    assert seen == ["bourso-mcp"]


def test_load_credentials_reads_keyring_by_default():
    keyring.get_keyring().set_password("bourso-mcp", "87654321", "pw2")
    assert load_credentials() == Credentials("87654321", "pw2")


def test_missing_entry_message_gives_cmdkey():
    with pytest.raises(CredentialsError) as info:
        load_credentials(lambda target: None)
    assert CMDKEY_HINT in str(info.value)
    assert CMDKEY_HINT == "cmdkey /generic:bourso-mcp /user:<identifiant> /pass"


def test_login_must_be_digits():
    with pytest.raises(CredentialsError) as info:
        load_credentials(lambda target: ("12a45", "pw"))
    message = str(info.value)
    assert "12a45" not in message
    assert CMDKEY_HINT in message


@pytest.mark.parametrize("login", ["", "１２３", "123 456", "+12345"])
def test_login_must_be_ascii_digits_only(login):
    with pytest.raises(CredentialsError):
        load_credentials(lambda target: (login, "pw"))


def test_empty_password_rejected():
    with pytest.raises(CredentialsError) as info:
        load_credentials(lambda target: ("12345678", ""))
    assert CMDKEY_HINT in str(info.value)
    assert "12345678" not in str(info.value)


def test_keyring_failure_is_a_credentials_error():
    def reader(target):
        raise KeyringError("accès refusé")

    with pytest.raises(CredentialsError) as info:
        load_credentials(reader)
    assert CMDKEY_HINT in str(info.value)


def test_credentials_error_is_auth_failed():
    assert issubclass(CredentialsError, AuthFailed)


def test_credentials_module_is_read_only():
    src = Path(credentials.__file__).read_text(encoding="utf-8")
    for forbidden in ("set_password", "delete_password", "open(", "setx"):
        assert forbidden not in src
