from __future__ import annotations

import base64
import logging
import sys

import pytest
from woob.tools.storage import IStorage

from bourso_mcp.source import dpapi
from bourso_mcp.source.dpapi import DpapiError
from bourso_mcp.source.woob_storage import EncryptedWoobStorage

win32 = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI")


def fake_protect(data: bytes) -> bytes:
    return b"P:" + base64.b64encode(data)


def fake_unprotect(blob: bytes) -> bytes:
    if not blob.startswith(b"P:"):
        raise DpapiError("blob inconnu")
    return base64.b64decode(blob[2:])


def storage(path):
    return EncryptedWoobStorage(path, protect=fake_protect, unprotect=fake_unprotect)


def test_storage_is_a_woob_storage(tmp_path):
    assert isinstance(storage(tmp_path / "state.bin"), IStorage)


def test_storage_roundtrip(tmp_path):
    path = tmp_path / "state.bin"
    first = storage(path)
    first.load("backends", "boursorama", {})
    first.set("backends", "boursorama", "browser_state", {"cookies": "abc", "twofa_count": 0})
    first.save("backends", "boursorama")

    second = storage(path)
    second.load("backends", "boursorama", {})
    assert second.get("backends", "boursorama", "browser_state", default={}) == {
        "cookies": "abc",
        "twofa_count": 0,
    }


def test_missing_file_is_empty_state_and_nothing_written(tmp_path):
    path = tmp_path / "state.bin"
    s = storage(path)
    s.load("backends", "boursorama", {})
    assert s.get("backends", "boursorama", "browser_state", default={}) == {}
    assert not path.exists()


def test_load_merges_default_like_woob_standard_storage(tmp_path):
    path = tmp_path / "state.bin"
    first = storage(path)
    first.set("backends", "boursorama", "browser_state", {"url": "u"})
    first.save("backends", "boursorama")

    second = storage(path)
    second.load("backends", "boursorama", {"extra": 1})
    assert second.get("backends", "boursorama", "extra") == 1
    assert second.get("backends", "boursorama", "browser_state") == {"url": "u"}


def test_get_set_delete_follow_woob_paths(tmp_path):
    s = storage(tmp_path / "state.bin")
    s.set("backends", "boursorama", "a", "b", 42)
    assert s.get("backends", "boursorama", "a", "b") == 42
    assert s.get("backends", "boursorama", "a", "absent") is None
    assert s.get("backends", "boursorama", "missing", "deep", default="d") == "d"
    s.delete("backends", "boursorama", "a", "b")
    assert s.get("backends", "boursorama", "a", "b", default="gone") == "gone"
    s.delete("backends", "boursorama", "nothing", "here")  # chemin absent : sans effet


def test_save_is_atomic_and_leaves_a_single_file(tmp_path):
    path = tmp_path / "state.bin"
    s = storage(path)
    s.set("backends", "boursorama", "browser_state", {"cookies": "v1"})
    s.save("backends", "boursorama")
    s.set("backends", "boursorama", "browser_state", {"cookies": "v2"})
    s.save("backends", "boursorama")
    assert [p.name for p in tmp_path.iterdir()] == ["state.bin"]
    assert path.read_bytes().startswith(b"P:")
    assert storage(path).get("backends", "boursorama", "browser_state") == {"cookies": "v2"}


def test_save_creates_missing_woob_dir(tmp_path):
    path = tmp_path / "woob" / "state.bin"
    s = storage(path)
    s.set("backends", "boursorama", "k", "v")
    s.save("backends", "boursorama")
    assert path.is_file()


@win32
def test_state_file_is_not_plaintext(tmp_path):
    path = tmp_path / "state.bin"
    s = EncryptedWoobStorage(path)
    s.load("backends", "boursorama", {})
    s.set("backends", "boursorama", "browser_state", {"cookies": "SESSIONID=abc"})
    s.save("backends", "boursorama")
    data = path.read_bytes()
    assert b"SESSIONID" not in data
    assert b"cookies" not in data
    assert dpapi.unprotect(data)  # c'est bien un blob DPAPI de l'utilisateur courant
    reloaded = EncryptedWoobStorage(path)
    assert reloaded.get("backends", "boursorama", "browser_state") == {"cookies": "SESSIONID=abc"}


def _broken_unprotect(blob: bytes) -> bytes:
    raise DpapiError("blob altéré")


@pytest.mark.parametrize(
    "unprotect",
    [_broken_unprotect, lambda blob: b"{pas du json SESSIONID", lambda blob: b"[1, 2]"],
    ids=["dpapi", "json", "not-a-dict"],
)
def test_unreadable_state_starts_fresh(tmp_path, caplog, unprotect):
    path = tmp_path / "state.bin"
    path.write_bytes(b"P:SESSIONID=secret-cookie")
    with caplog.at_level(logging.DEBUG):
        s = EncryptedWoobStorage(path, protect=fake_protect, unprotect=unprotect)
        s.load("backends", "boursorama", {})
    assert s.get("backends", "boursorama", "browser_state", default={}) == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "SESSIONID" not in caplog.text
    assert "secret" not in caplog.text
