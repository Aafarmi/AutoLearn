"""Protect task URLs at rest with Windows DPAPI CurrentUser."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os


_PREFIX = "dpapi-currentuser:v1:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class UrlProtectionError(RuntimeError):
    """URL protection failed without exposing the URL or OS error details."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_transform(value: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise UrlProtectionError("当前平台无法处理 Windows DPAPI 数据。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_buffer = ctypes.create_string_buffer(value)
    input_blob = _DataBlob(len(value), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = _DataBlob()
    if protect:
        transform = crypt32.CryptProtectData
        transform.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        transform.restype = wintypes.BOOL
        succeeded = transform(
            ctypes.byref(input_blob),
            "AutoLearn task URL",
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        transform = crypt32.CryptUnprotectData
        transform.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        transform.restype = wintypes.BOOL
        description = wintypes.LPWSTR()
        succeeded = transform(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
    if not succeeded:
        raise UrlProtectionError("Windows DPAPI 无法处理任务 URL。")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def protect_page_url(value: str | None) -> str | None:
    if value is None or os.name != "nt":
        return value
    if value.startswith(_PREFIX):
        unprotect_page_url(value)
        return value
    try:
        encrypted = _dpapi_transform(value.encode("utf-8"), protect=True)
    except Exception:
        raise UrlProtectionError("无法安全保存任务 URL。") from None
    return _PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_page_url(value: str | None) -> str | None:
    if value is None or not value.startswith(_PREFIX):
        return value
    if os.name != "nt":
        raise UrlProtectionError("此任务 URL 由 Windows 当前用户保护，无法在当前平台读取。")
    try:
        encrypted = base64.b64decode(value[len(_PREFIX) :], validate=True)
        cleartext = _dpapi_transform(encrypted, protect=False)
        return cleartext.decode("utf-8")
    except Exception:
        raise UrlProtectionError("无法解密任务 URL；数据库内容保持不变。") from None


__all__ = ["UrlProtectionError", "protect_page_url", "unprotect_page_url"]
