from __future__ import annotations

from datetime import datetime, timedelta, timezone

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.credentials import SimpleCredential

from bourso_mcp.config import load_settings
from bourso_mcp.store.repository import Repository


class InMemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)

    def get_credential(self, service, username):
        if username is not None:
            pw = self.get_password(service, username)
            return SimpleCredential(username, pw) if pw is not None else None
        for (svc, user), pw in self.store.items():
            if svc == service:
                return SimpleCredential(user, pw)
        return None


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    monkeypatch.setattr("bourso_mcp.config.windows_persistent_env", lambda name: None)
    monkeypatch.delenv("BOURSO_MCP_HOME", raising=False)
    previous = keyring.get_keyring()
    keyring.set_keyring(InMemoryKeyring())
    yield
    keyring.set_keyring(previous)


@pytest.fixture
def settings(tmp_path):
    return load_settings({"BOURSO_MCP_HOME": str(tmp_path)})


@pytest.fixture
def repo(settings):
    return Repository(settings.db_path)


@pytest.fixture
def now():
    return datetime(2026, 11, 10, 7, 0, tzinfo=timezone(timedelta(hours=1)))
