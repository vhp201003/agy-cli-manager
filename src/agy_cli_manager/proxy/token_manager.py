from __future__ import annotations

import ctypes
import json
import logging
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

import base64

logger = logging.getLogger("AgyProxy.TokenManager")

WINDOWS_CREDENTIAL_PREFIX = "agy-cli-manager:"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Public Antigravity OAuth client credentials (read from env or decode)
_DEFAULT_CLIENT_ID = base64.b64decode(b"MTA3MTAwNjA2MDU5MS10bWhzc2luMmgyMWxjcmUyMzV2dG9sb2poNGc0MDNlcC5hcHBzLmdvb2dsZXVzZXJjb250ZW50LmNvbQ==").decode("utf-8")
_DEFAULT_CLIENT_SECRET = base64.b64decode(b"R0NDU1BYLUs1OEZXUjQ4NkxkTEoxbUxCOHNYQzR6NnFEQWY=").decode("utf-8")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", _DEFAULT_CLIENT_ID)
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", _DEFAULT_CLIENT_SECRET)


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


if os.name == "nt":
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


def read_windows_credential_blob(target: str) -> dict | None:
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


def write_windows_credential_blob(target: str, data: dict, user_name: str = "antigravity") -> bool:
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


class TokenManager:
    def __init__(self, manager_root: Path | None = None) -> None:
        self.manager_root = (
            manager_root or Path.home() / ".agy-cli-manager"
        )
        self.state_file = self.manager_root / "state.json"
        self._accounts: dict[str, dict] = {}
        self._cooldowns: dict[str, float] = {}
        self._rr_index = 0
        self.reload_accounts()

    def reload_accounts(self) -> None:
        account_names: list[str] = []
        active = None
        if self.state_file.is_file():
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                account_names = list(state.get("accounts", {}).keys())
                active = state.get("active")
                if active and active in account_names:
                    account_names.remove(active)
                    account_names.insert(0, active)
            except Exception:
                pass

        if not account_names:
            account_names = ["acc3", "acc1", "acc2"]

        for name in account_names:
            target = f"{WINDOWS_CREDENTIAL_PREFIX}{name}"
            data = read_windows_credential_blob(target)
            if data and "token" in data:
                tok = data["token"]
                access_token = tok.get("access_token")
                if access_token:
                    existing = self._accounts.get(name, {})
                    # Preserve newer in-memory token if local file was not updated yet
                    if existing.get("access_token") and existing.get("expires_at", 0) > tok.get("expiry_timestamp", 0):
                        continue

                    # Calculate expiry
                    expires_at = tok.get("expiry_timestamp")
                    if not expires_at:
                        # parse RFC3339 if available or assume 1 hr
                        expires_at = time.time() + 3600

                    email = data.get("email") or tok.get("email")
                    display_name = data.get("name")
                    id_token_jwt = data.get("id_token")
                    if not email and id_token_jwt:
                        try:
                            import base64
                            payload_b64 = id_token_jwt.split(".")[1]
                            rem = len(payload_b64) % 4
                            if rem > 0:
                                payload_b64 += "=" * (4 - rem)
                            jwt_payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                            email = jwt_payload.get("email")
                            if not display_name:
                                display_name = jwt_payload.get("name")
                        except Exception:
                            pass

                    self._accounts[name] = {
                        "name": name,
                        "access_token": access_token,
                        "refresh_token": tok.get("refresh_token"),
                        "expiry": tok.get("expiry"),
                        "expires_at": expires_at,
                        "email": email or name,
                        "display_name": display_name or name,
                        "last_refreshed": existing.get("last_refreshed", 0),
                    }

        active_blob = read_windows_credential_blob("gemini:antigravity")
        if active and active_blob and "token" in active_blob and "access_token" in active_blob["token"]:
            tok = active_blob["token"]
            if active in self._accounts:
                self._accounts[active]["access_token"] = tok["access_token"]
                if tok.get("refresh_token"):
                    self._accounts[active]["refresh_token"] = tok["refresh_token"]

    def refresh_account_token(self, account_name: str) -> tuple[bool, str]:
        acc = self._accounts.get(account_name)
        if not acc:
            return False, f"Account '{account_name}' not found in pool"

        refresh_token = acc.get("refresh_token")
        if not refresh_token:
            return False, f"No refresh_token for account '{account_name}'"

        payload = urllib.parse.urlencode({
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }).encode("utf-8")

        req = urllib.request.Request(
            GOOGLE_OAUTH_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            new_access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 3600))
            if not new_access_token:
                return False, "Google response missing access_token"

            # Update in-memory
            acc["access_token"] = new_access_token
            acc["expires_at"] = time.time() + expires_in
            acc["last_refreshed"] = time.time()
            self.clear_cooldown(account_name)

            # Persist to Windows Credential Manager
            target = f"{WINDOWS_CREDENTIAL_PREFIX}{account_name}"
            blob = read_windows_credential_blob(target) or {}
            tok = blob.setdefault("token", {})
            tok["access_token"] = new_access_token
            tok["expiry_timestamp"] = acc["expires_at"]
            tok["refresh_token"] = refresh_token
            write_windows_credential_blob(target, blob)

            # Also sync active credential if this account is active
            active_blob = read_windows_credential_blob("gemini:antigravity")
            if active_blob and (active_blob.get("email") == acc.get("email") or active_blob.get("name") == account_name):
                tok_act = active_blob.setdefault("token", {})
                tok_act["access_token"] = new_access_token
                tok_act["expiry_timestamp"] = acc["expires_at"]
                write_windows_credential_blob("gemini:antigravity", active_blob)

            logger.info(f"Successfully refreshed OAuth token for '{account_name}' (Expires in {expires_in}s)")
            return True, "Token refreshed successfully"

        except urllib.error.HTTPError as err:
            err_msg = err.read().decode("utf-8", "ignore")
            logger.error(f"Failed to refresh token for '{account_name}': HTTP {err.code} - {err_msg}")
            return False, f"HTTP {err.code}: {err_msg}"
        except Exception as exc:
            logger.error(f"Exception refreshing token for '{account_name}': {exc}")
            return False, str(exc)

    def refresh_all_accounts(self, force: bool = False) -> dict[str, dict]:
        results = {}
        now = time.time()
        for name, acc in list(self._accounts.items()):
            expires_at = acc.get("expires_at", 0)
            # Refresh if forced or expiring in less than 15 minutes (900 seconds)
            if force or (expires_at - now < 900):
                success, msg = self.refresh_account_token(name)
                results[name] = {"success": success, "message": msg}
            else:
                remaining_min = int((expires_at - now) / 60)
                results[name] = {
                    "success": True,
                    "message": f"Token still fresh ({remaining_min}m remaining)",
                }
        return results

    def get_token_for_request(self) -> tuple[str, str]:
        if not self._accounts:
            self.reload_accounts()

        if not self._accounts:
            data = read_windows_credential_blob("gemini:antigravity")
            if data and "token" in data and "access_token" in data["token"]:
                return "default", data["token"]["access_token"]
            raise RuntimeError("No accounts available in pool or Windows Credential Manager.")

        now = time.time()
        available = [
            name
            for name in self._accounts
            if self._cooldowns.get(name, 0) <= now
        ]

        if not available:
            oldest = min(self._cooldowns.items(), key=lambda x: x[1])[0]
            available = [oldest]

        chosen_name = available[self._rr_index % len(available)]
        self._rr_index = (self._rr_index + 1) % len(available)
        return chosen_name, self._accounts[chosen_name]["access_token"]

    def mark_429(self, account_name: str, cooldown_seconds: int = 600) -> None:
        self._cooldowns[account_name] = time.time() + cooldown_seconds

    def clear_cooldown(self, account_name: str) -> None:
        self._cooldowns.pop(account_name, None)

    def fetch_account_quota(self, account_name: str) -> dict | None:
        acc = self._accounts.get(account_name)
        if not acc:
            return None
        token = acc.get("access_token")
        if not token:
            return None

        req = urllib.request.Request(
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
            data=json.dumps({"project": "aicode-consumers"}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            parsed_quota = {
                "gemini": {
                    "5h": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "desc": ""},
                    "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "desc": ""},
                },
                "third_party": {
                    "5h": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "desc": ""},
                    "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "desc": ""},
                },
                "last_synced": time.time(),
            }

            for group in data.get("groups", []):
                disp_name = (group.get("displayName") or "").lower()
                is_gemini = "gemini" in disp_name
                target_key = "gemini" if is_gemini else "third_party"

                for b in group.get("buckets", []):
                    w = b.get("window")
                    rem = b.get("remainingFraction")
                    pct = round(rem * 100, 1) if rem is not None else 100.0
                    if w in ("5h", "weekly"):
                        parsed_quota[target_key][w] = {
                            "percent": pct,
                            "fraction": rem if rem is not None else 1.0,
                            "reset_time": b.get("resetTime"),
                            "desc": b.get("description", ""),
                        }

            acc["quota"] = parsed_quota
            return parsed_quota
        except Exception as e:
            logger.debug(f"Could not fetch quota for {account_name}: {e}")
            return acc.get("quota")

    def warmup_account(self, account_name: str, model: str = "gemini-2.5-flash") -> tuple[bool, str]:
        acc = self._accounts.get(account_name)
        if not acc:
            return False, "Account not found"
        token = acc.get("access_token")
        if not token:
            return False, "Missing access token"

        payload = {
            "project": "aicode-consumers",
            "model": model,
            "request": {
                "contents": [
                    {"parts": [{"text": "ping"}]}
                ],
                "generationConfig": {
                    "maxOutputTokens": 1
                }
            }
        }
        req = urllib.request.Request(
            "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                _ = resp.read()
            logger.info(f"Warmup successful for account '{account_name}' on {model}")
            return True, "Triggered 5h window successfully"
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", "ignore")
            logger.warning(f"Warmup HTTP {err.code} for '{account_name}': {err_body}")
            if err.code == 429:
                self.mark_429(account_name, cooldown_seconds=600)
                return False, "Account is currently 429 / Rate Limited"
            return False, f"HTTP {err.code}: {err_body[:100]}"
        except Exception as exc:
            logger.error(f"Warmup exception for '{account_name}': {exc}")
            return False, str(exc)

    def warmup_all_accounts(self, model: str = "gemini-2.5-flash") -> dict[str, dict]:
        results = {}
        for name in list(self._accounts.keys()):
            ok, msg = self.warmup_account(name, model=model)
            results[name] = {"success": ok, "message": msg}
        # Immediately sync fresh quotas to update resetTime on dashboard
        self.fetch_all_quotas()
        return results

    def fetch_all_quotas(self) -> dict[str, dict]:
        res = {}
        for name in list(self._accounts.keys()):
            res[name] = self.fetch_account_quota(name)
        return res

    def get_status(self) -> dict:
        now = time.time()
        res = {}
        for name, acc in self._accounts.items():
            cd = self._cooldowns.get(name, 0)
            expires_at = acc.get("expires_at", 0)
            res[name] = {
                "name": name,
                "email": acc.get("email", name),
                "display_name": acc.get("display_name", name),
                "is_active": cd <= now,
                "in_cooldown": cd > now,
                "cooldown_remaining_sec": max(0, int(cd - now)),
                "token_expires_in_sec": max(0, int(expires_at - now)),
                "token_expires_in_min": round(max(0, expires_at - now) / 60, 1),
                "token_preview": acc["access_token"][:12] + "..." + acc["access_token"][-6:],
                "has_refresh_token": bool(acc.get("refresh_token")),
                "last_refreshed": acc.get("last_refreshed", 0),
                "quota": acc.get("quota") or {
                    "gemini": {
                        "5h": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "desc": ""},
                        "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "desc": ""},
                    },
                    "third_party": {
                        "5h": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "desc": ""},
                        "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "desc": ""},
                    },
                    "last_synced": 0,
                },
            }
        return res
