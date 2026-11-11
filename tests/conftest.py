from __future__ import annotations

from datetime import datetime, timedelta, timezone

import keyring
import pytest

from bourso_mcp.config import load_settings
from bourso_mcp.store.repository import Repository
from tests.fakes import InMemoryKeyring


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
