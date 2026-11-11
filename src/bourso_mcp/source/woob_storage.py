"""Stockage woob chiffré : l'état de session (cookies, état 2FA) dans un seul fichier DPAPI.

Même interface et même sémantique que `StandardStorage` de woob (note j du README), mais l'état
est un dict sérialisé en JSON puis chiffré, écrit de façon atomique.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from woob.tools.config.iconfig import ConfigError
from woob.tools.storage import IStorage

from bourso_mcp.source import dpapi
from bourso_mcp.source.dpapi import DpapiError

log = logging.getLogger("bourso_mcp.source")


class EncryptedWoobStorage(IStorage):
    def __init__(
        self,
        path: Path,
        protect: Callable[[bytes], bytes] = dpapi.protect,
        unprotect: Callable[[bytes], bytes] = dpapi.unprotect,
    ) -> None:
        self.path = path
        self._protect = protect
        self._unprotect = unprotect
        self._values: dict[str, Any] = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:  # droits, fichier verrouillé (antivirus)...
            return self._unreadable(type(exc).__name__)
        try:
            values = json.loads(self._unprotect(blob))
        except (DpapiError, ValueError) as exc:
            return self._unreadable(type(exc).__name__)
        if not isinstance(values, dict):
            return self._unreadable(type(values).__name__)
        return values

    @staticmethod
    def _unreadable(reason: str) -> dict[str, Any]:
        # Jamais de contenu dans le journal : seulement le type d'erreur ou de valeur.
        log.warning("état de session woob illisible (%s) : ignoré, la 2FA sera redemandée", reason)
        return {}

    def load(self, what: str, name: str, default: Mapping[str, Any] = {}) -> None:
        current = self._values.setdefault(what, {}).get(name, {})
        self._values[what][name] = deepcopy(dict(default))
        self._values[what][name].update(current)

    def save(self, what: str, name: str) -> None:
        data = self._protect(json.dumps(self._values).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, what: str, name: str, *path: str, **kwargs: Any) -> Any:
        keys = (what, name, *path)
        node: Any = self._values
        for key in keys[:-1]:
            try:
                node = node[key]
            except KeyError:
                if "default" in kwargs:
                    return kwargs["default"]
                raise ConfigError() from None
            except TypeError:
                raise ConfigError() from None
        try:
            return node[keys[-1]]
        except KeyError:
            return kwargs.get("default")

    def set(self, what: str, name: str, *args: Any) -> None:
        *keys, value = (what, name, *args)
        node: Any = self._values
        for key in keys[:-1]:
            try:
                node = node.setdefault(key, {})
            except AttributeError:
                raise ConfigError() from None
        node[keys[-1]] = value

    def delete(self, what: str, name: str, *path: str) -> None:
        keys = (what, name, *path)
        node: Any = self._values
        for key in keys[:-1]:
            try:
                node = node[key]
            except KeyError:
                return
            except TypeError:
                raise ConfigError() from None
        node.pop(keys[-1], None)
