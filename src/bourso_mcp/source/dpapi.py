"""Chiffrement DPAPI de Windows (CryptProtectData / CryptUnprotectData de crypt32, via ctypes).

Portée utilisateur, entropie propre à l'application, jamais d'interface graphique : un blob
n'est lisible que par le même utilisateur Windows, avec la même entropie.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_ENTROPY = b"bourso-mcp"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DpapiError(Exception):
    """Chiffrement ou déchiffrement DPAPI impossible.

    Au déchiffrement : blob altéré, venu d'un autre utilisateur ou d'une autre entropie.
    """


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _api(name: str):
    if sys.platform != "win32":
        raise DpapiError("DPAPI n'existe que sous Windows")
    function = getattr(ctypes.WinDLL("crypt32", use_last_error=True), name)
    blob_p = ctypes.POINTER(_Blob)
    function.argtypes = [
        blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_p
    ]
    function.restype = wintypes.BOOL
    return function


def _transform(name: str, data: bytes) -> bytes:
    function = _api(name)
    local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    data_in, _keep_data = _blob(data)
    entropy, _keep_entropy = _blob(_ENTROPY)
    data_out = _Blob()
    ok = function(
        ctypes.byref(data_in), None, ctypes.byref(entropy), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out),
    )
    if not ok:
        raise DpapiError(f"{name} a échoué (erreur Windows {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData) if data_out.cbData else b""
    finally:
        if data_out.pbData:
            local_free(ctypes.cast(data_out.pbData, ctypes.c_void_p))


def protect(data: bytes) -> bytes:
    return _transform("CryptProtectData", data)


def unprotect(blob: bytes) -> bytes:
    """DpapiError si le blob est altéré, ou vient d'un autre utilisateur ou d'une autre entropie."""
    return _transform("CryptUnprotectData", blob)
