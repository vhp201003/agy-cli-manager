"""Cross-platform credential provider for Antigravity Proxy.

Supports:
- Windows: Windows Credential Manager via native Advapi32.dll (CredReadW/CredWriteW).
- Linux / macOS: Standard POSIX file store (~/.gemini/antigravity-cli/antigravity-oauth-token
  and per-account folders ~/.agy-cli-manager/accounts/<name>/...), with atomic replacement
  and strict 0o600 permissions.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from typing import Protocol

logger = logging.getLogger("AgyProxy.CredentialProvider")

WINDOWS_CREDENTIAL_PREFIX = "agy-cli-manager:"


class CredentialProvider(Protocol):
    """Abstract protocol for platform-specific credential storage."""

    def read_account(self, name: str) -> dict | None:
        """Read account token and metadata payload."""
        ...

    def write_account(self, name: str, data: dict) -> bool:
        """Persist refreshed account token and metadata payload."""
        ...

    def sync_active(self, name: str, data: dict) -> bool:
        """Synchronize active system token for the currently active account."""
        ...


# ---------------------------------------------------------------------------
# Windows Credential Provider (Advapi32.dll)
# ---------------------------------------------------------------------------

if os.name == "nt":
    import ctypes

    class _WindowsCredential(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint32),
            ("credential_type", ctypes.c_uint32),
            ("target_name", ctypes.c_wchar_p),
            ("comment", ctypes.c_wchar_p),
            ("last_written", ctypes.c_byte * 8),
            ("credential_blob_size", ctypes.c_uint32),
            ("credential_blob", ctypes.POINTER(ctypes.c_char)),
            ("persist", ctypes.c_uint32),
            ("attribute_count", ctypes.c_uint32),
            ("attributes", ctypes.c_void_p),
            ("target_alias", ctypes.c_wchar_p),
            ("user_name", ctypes.c_wchar_p),
        ]

    _ADVAPI32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    _ADVAPI32.CredReadW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ADVAPI32.CredReadW.restype = ctypes.c_bool

    _ADVAPI32.CredWriteW.argtypes = [
        ctypes.POINTER(_WindowsCredential),
        ctypes.c_uint32,
    ]
    _ADVAPI32.CredWriteW.restype = ctypes.c_bool
    _ADVAPI32.CredFree.argtypes = [ctypes.c_void_p]
    _ADVAPI32.CredFree.restype = ctypes.c_bool


class WindowsCredentialProvider:
    """Windows Credential Manager implementation."""

    def read_blob(self, target: str) -> dict | None:
        if os.name != "nt":
            return None
        credential_ptr = ctypes.c_void_p()
        if not _ADVAPI32.CredReadW(target, 1, 0, ctypes.byref(credential_ptr)):
            return None
        try:
            credential = ctypes.cast(
                credential_ptr, ctypes.POINTER(_WindowsCredential)
            ).contents
            blob = ctypes.string_at(
                credential.credential_blob, credential.credential_blob_size
            )
            return json.loads(blob.decode("utf-8", "ignore"))
        except Exception:
            return None
        finally:
            _ADVAPI32.CredFree(credential_ptr)

    def write_blob(self, target: str, data: dict, user_name: str = "antigravity") -> bool:
        if os.name != "nt":
            return False
        try:
            raw_blob = json.dumps(data).encode("utf-8")
            c_blob = ctypes.create_string_buffer(raw_blob)

            cred = _WindowsCredential()
            cred.flags = 0
            cred.credential_type = 1
            cred.target_name = target
            cred.comment = None
            cred.credential_blob_size = len(raw_blob)
            cred.credential_blob = ctypes.cast(c_blob, ctypes.POINTER(ctypes.c_char))
            cred.persist = 2
            cred.attribute_count = 0
            cred.attributes = None
            cred.target_alias = None
            cred.user_name = user_name

            return bool(_ADVAPI32.CredWriteW(ctypes.byref(cred), 0))
        except Exception as e:
            logger.error(f"Failed to write Windows Credential for {target}: {e}")
            return False

    def read_account(self, name: str) -> dict | None:
        return self.read_blob(f"{WINDOWS_CREDENTIAL_PREFIX}{name}")

    def write_account(self, name: str, data: dict) -> bool:
        return self.write_blob(f"{WINDOWS_CREDENTIAL_PREFIX}{name}", data)

    def sync_active(self, name: str, data: dict) -> bool:
        target = "gemini:antigravity"
        active_blob = self.read_blob(target)
        if active_blob:
            tok = active_blob.setdefault("token", {})
            src_tok = data.get("token", {})
            tok["access_token"] = src_tok.get("access_token")
            tok["expiry_timestamp"] = src_tok.get("expiry_timestamp")
            if src_tok.get("refresh_token"):
                tok["refresh_token"] = src_tok.get("refresh_token")
            return self.write_blob(target, active_blob)
        return self.write_blob(target, data)


# ---------------------------------------------------------------------------
# POSIX File Credential Provider (Linux & macOS)
# ---------------------------------------------------------------------------

class PosixFileCredentialProvider:
    """POSIX file-based credential provider with atomic writes & chmod 0o600."""

    def __init__(self, manager_root: Path | None = None) -> None:
        self.manager_root = manager_root or Path.home() / ".agy-cli-manager"
        self.accounts_dir = self.manager_root / "accounts"
        self.live_token_path = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

    def _account_token_path(self, name: str) -> Path:
        return self.accounts_dir / name / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

    def _read_file_safe(self, path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug(f"Could not parse token file at {path}: {e}")
            return None

    def _write_file_atomic(self, path: Path, data: dict) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(data, indent=2).encode("utf-8")
            # Write to temporary file in the same directory, then atomic rename
            with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False) as tf:
                tf.write(content)
                temp_name = tf.name

            # Set POSIX private permissions (user read/write only)
            if hasattr(os, "chmod"):
                try:
                    os.chmod(temp_name, 0o600)
                except OSError:
                    pass

            os.replace(temp_name, str(path))
            return True
        except Exception as exc:
            logger.error(f"Failed atomic write to {path}: {exc}")
            return False

    def read_account(self, name: str) -> dict | None:
        # Check standard account dir
        path = self._account_token_path(name)
        data = self._read_file_safe(path)
        if data:
            return data

        # Fallback: check legacy flat oauth_creds.json or account root
        fallback_path = self.accounts_dir / name / "oauth_creds.json"
        data = self._read_file_safe(fallback_path)
        if data:
            return data

        return None

    def write_account(self, name: str, data: dict) -> bool:
        path = self._account_token_path(name)
        return self._write_file_atomic(path, data)

    def sync_active(self, name: str, data: dict) -> bool:
        ok = self._write_file_atomic(self.live_token_path, data)
        # If Linux Secret Service is available, attempt syncing live slot
        if sys.platform.startswith("linux"):
            try:
                from agy_cli_manager.credential_store import (
                    linux_secret_service_available,
                    write_linux_live_credential,
                )
                if linux_secret_service_available():
                    write_linux_live_credential(json.dumps(data).encode("utf-8"))
            except Exception as e:
                logger.debug(f"Linux secret service sync ignored: {e}")
        return ok


def get_credential_provider(manager_root: Path | None = None) -> CredentialProvider:
    """Factory creating the appropriate credential provider based on OS."""
    if os.name == "nt":
        return WindowsCredentialProvider()
    return PosixFileCredentialProvider(manager_root=manager_root)
