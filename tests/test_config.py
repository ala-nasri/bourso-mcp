import sys
from pathlib import Path

import pytest

from bourso_mcp import config
from bourso_mcp.config import env_lookup, load_settings
from bourso_mcp.config import (
    windows_persistent_env as real_windows_persistent_env,  # capturé à l'import
)

NONE = lambda name: None


def test_default_home_is_localappdata(tmp_path):
    s = load_settings({"LOCALAPPDATA": str(tmp_path)}, persistent=NONE)
    assert s.home == tmp_path / "bourso-mcp"
    assert s.db_path == tmp_path / "bourso-mcp" / "bourso.db"
    assert s.lock_path == tmp_path / "bourso-mcp" / "sync.lock"


def test_env_overrides_home(tmp_path):
    s = load_settings(
        {"BOURSO_MCP_HOME": str(tmp_path / "x"), "LOCALAPPDATA": "ignored"}, persistent=NONE
    )
    assert s.home == tmp_path / "x"


def test_persistent_env_used_when_process_env_lacks_variable(tmp_path):
    # cas de Claude Desktop sous Windows : environnement du processus réduit
    s = load_settings({}, persistent={"BOURSO_MCP_HOME": str(tmp_path / "p")}.get)
    assert s.home == tmp_path / "p"


def test_process_env_wins_over_persistent(tmp_path):
    s = load_settings(
        {"BOURSO_MCP_HOME": str(tmp_path / "a")}, persistent={"BOURSO_MCP_HOME": "b"}.get
    )
    assert s.home == tmp_path / "a"


def test_empty_value_counts_as_missing():
    assert env_lookup("X", {"X": ""}, persistent={"X": "y"}.get) == "y"


def test_persistent_resolved_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "windows_persistent_env", {"X": "patched"}.get)
    assert env_lookup("X", {}) == "patched"


def test_missing_localappdata_falls_back_to_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert (
        load_settings({}, persistent=NONE).home == tmp_path / "AppData" / "Local" / "bourso-mcp"
    )


def test_load_settings_creates_directories(tmp_path):
    s = load_settings({"BOURSO_MCP_HOME": str(tmp_path)}, persistent=NONE)
    assert s.statements_dir.is_dir() and s.exports_dir.is_dir() and s.woob_dir.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="registre Windows")
def test_windows_persistent_env_prefers_user_then_system(monkeypatch):
    import winreg

    # winreg est entièrement remplacé : on n'écrit ni ne lit jamais le vrai registre.
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    user_key, system_key = Key(), Key()
    values: dict[object, tuple[str, int]] = {}

    def fake_open_key(root, sub_key, *args, **kwargs):
        if root == winreg.HKEY_CURRENT_USER:
            if user_key not in values:
                raise FileNotFoundError
            return user_key
        return system_key

    def fake_query(key, name):
        if key in values and name == "V":
            return values[key]
        raise FileNotFoundError

    monkeypatch.setattr(winreg, "OpenKey", fake_open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", fake_query)

    # HKCU absent : HKLM, valeur REG_EXPAND_SZ développée
    raw = "%SystemRoot%" + chr(92) + "x"
    values[system_key] = (raw, winreg.REG_EXPAND_SZ)
    assert real_windows_persistent_env("V") == winreg.ExpandEnvironmentStrings(raw)
    assert real_windows_persistent_env("ABSENT") is None

    # HKCU présent : il l'emporte
    values[user_key] = ("utilisateur", winreg.REG_SZ)
    assert real_windows_persistent_env("V") == "utilisateur"
