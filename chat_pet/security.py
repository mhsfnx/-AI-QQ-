from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


class CredentialError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True) if os.name == "nt" else None
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

if _crypt32 is not None:
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
if _kernel32 is not None:
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), buffer), buffer


def protect_secret(secret: str) -> str:
    """Encrypt a secret with Windows DPAPI for the current Windows user."""
    if not secret:
        return ""
    if os.name != "nt":
        raise CredentialError("API 密钥加密仅支持 Windows。")

    # Some managed desktop/sandbox accounts have no loaded DPAPI user profile.
    # Try current-user scope first, then local-machine scope that still never
    # stores the key as plaintext.
    for scope, prefix in ((0x1, "dpapi-user:"), (0x5, "dpapi-machine:")):
        data_in, _buffer_in = _blob(secret.encode("utf-8"))
        data_out = _DataBlob()
        if not _crypt32.CryptProtectData(
            ctypes.byref(data_in),
            "大肥鱼桌宠",
            None,
            None,
            None,
            scope,
            ctypes.byref(data_out),
        ):
            continue
        try:
            encrypted = ctypes.string_at(data_out.pbData, data_out.cbData)
            return prefix + base64.b64encode(encrypted).decode("ascii")
        finally:
            _kernel32.LocalFree(data_out.pbData)
    raise CredentialError(f"无法加密 API 密钥（Windows 错误 {ctypes.get_last_error()}）。")


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(("dpapi:", "dpapi-user:", "dpapi-machine:")):
        raise CredentialError("API 密钥不是受支持的加密格式。")
    if os.name != "nt":
        raise CredentialError("当前系统无法解密 Windows DPAPI 数据。")

    try:
        encoded = next(
            value[len(prefix) :]
            for prefix in ("dpapi-user:", "dpapi-machine:", "dpapi:")
            if value.startswith(prefix)
        )
        encrypted = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialError("API 密钥数据已损坏。") from exc
    data_in, _buffer_in = _blob(encrypted)
    data_out = _DataBlob()
    flags = 0x1
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(data_in),
        None,
        None,
        None,
        None,
        flags,
        ctypes.byref(data_out),
    ):
        raise CredentialError("无法解密 API 密钥，可能不是由当前 Windows 用户保存。")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData).decode("utf-8")
    finally:
        _kernel32.LocalFree(data_out.pbData)
