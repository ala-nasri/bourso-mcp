"""Réglages non secrets : dossier de données et lecture des variables d'environnement."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

_USER_ENV_KEY = "Environment"
_SYSTEM_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"


@dataclass(frozen=True)
class Settings:
    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / "bourso.db"

    @property
    def statements_dir(self) -> Path:
        return self.home / "statements"

    @property
    def exports_dir(self) -> Path:
        return self.home / "exports"

    @property
    def woob_dir(self) -> Path:
        return self.home / "woob"

    @property
    def lock_path(self) -> Path:
        return self.home / "sync.lock"


def windows_persistent_env(name: str) -> str | None:
    """Variable persistante Windows : clé utilisateur (HKCU), puis clé système (HKLM)."""
    if sys.platform != "win32":
        return None
    import winreg

    for root, sub_key in (
        (winreg.HKEY_CURRENT_USER, _USER_ENV_KEY),
        (winreg.HKEY_LOCAL_MACHINE, _SYSTEM_ENV_KEY),
    ):
        try:
            with winreg.OpenKey(root, sub_key) as key:
                value, kind = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if not isinstance(value, str):
            continue
        if kind == winreg.REG_EXPAND_SZ:
            value = winreg.ExpandEnvironmentStrings(value)
        return value
    return None


def env_lookup(
    name: str,
    env: Mapping[str, str] | None = None,
    persistent: Callable[[str], str | None] | None = None,
) -> str | None:
    """Environnement du processus, puis variables persistantes ; vide = absent."""
    if env is None:
        env = os.environ
    if persistent is None:
        persistent = windows_persistent_env  # résolu à l'appel : remplaçable par les tests
    return env.get(name) or persistent(name) or None


def load_settings(
    env: Mapping[str, str] | None = None,
    persistent: Callable[[str], str | None] | None = None,
) -> Settings:
    home_value = env_lookup("BOURSO_MCP_HOME", env, persistent)
    if home_value:
        home = Path(home_value)
    else:
        local = env_lookup("LOCALAPPDATA", env, persistent)
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        home = base / "bourso-mcp"
    settings = Settings(home=home)
    for directory in (settings.statements_dir, settings.exports_dir, settings.woob_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings
