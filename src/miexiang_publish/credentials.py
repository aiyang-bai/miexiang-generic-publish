from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass


@dataclass(frozen=True)
class CosCredentials:
    secret_id: str
    secret_key: str
    session_token: str | None = None


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _read_generic_credential(target: str) -> str | None:
    if sys.platform != "win32":
        return None
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    credential_ptr = ctypes.POINTER(_CREDENTIALW)()
    read = advapi.CredReadW
    read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    read.restype = wintypes.BOOL
    free = advapi.CredFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    if not read(target, 1, 0, ctypes.byref(credential_ptr)):
        return None
    try:
        credential = credential_ptr.contents
        if credential.CredentialBlobSize == 0:
            return None
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            return raw.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            return raw.decode("utf-8").rstrip("\x00")
    finally:
        free(credential_ptr)


def read_cos_credentials(targets: dict[str, str]) -> CosCredentials | None:
    secret_id = _read_generic_credential(targets["secret_id"])
    secret_key = _read_generic_credential(targets["secret_key"])
    session_token = _read_generic_credential(targets["session_token"]) if targets.get("session_token") else None
    if not secret_id or not secret_key or (targets.get("session_token") and not session_token):
        return None
    return CosCredentials(secret_id=secret_id, secret_key=secret_key, session_token=session_token)
