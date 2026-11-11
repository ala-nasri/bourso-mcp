from __future__ import annotations

import sys

import pytest

from bourso_mcp.source.dpapi import DpapiError, protect, unprotect

win32 = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI")


@win32
def test_dpapi_roundtrip():
    blob = protect(b"x")
    assert blob != b"x"
    assert unprotect(blob) == b"x"


@win32
def test_dpapi_roundtrip_larger_and_empty_payloads():
    payload = bytes(range(256)) * 40
    assert unprotect(protect(payload)) == payload
    assert unprotect(protect(b"")) == b""


@win32
def test_dpapi_rejects_tampered_blob():
    blob = bytearray(protect(b"x"))
    blob[-1] ^= 0xFF
    with pytest.raises(DpapiError):
        unprotect(bytes(blob))


@win32
def test_dpapi_rejects_garbage():
    with pytest.raises(DpapiError):
        unprotect(b"pas un blob dpapi")
