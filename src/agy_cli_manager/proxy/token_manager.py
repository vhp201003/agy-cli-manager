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
from datetime import datetime, timezone
import threading

from agy_cli_manager.proxy.credential_provider import get_credential_provider, CredentialProvider

logger = logging.getLogger("AgyProxy.TokenManager")

GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _resolve_oauth_credentials(manager_root: Path) -> tuple[str | None, str | None]:
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET")
    if cid and csec:
        return cid, csec

    for env_path in (manager_root / ".env", Path.cwd() / ".env"):
        if env_path.is_file():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "GOOGLE_CLIENT_ID" and not cid:
                        cid = v
                    elif k == "GOOGLE_CLIENT_SECRET" and not csec:
                        csec = v
            except Exception:
                pass

    if not cid or not csec:
        cfg_file = manager_root / "config.json"
        if cfg_file.is_file():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                cid = cid or cfg.get("google_client_id")
                csec = csec or cfg.get("google_client_secret")
            except Exception:
                pass

    return cid, csec


class TokenManager:
    def __init__(self, manager_root: Path | None = None) -> None:
        self._lock = threading.RLock()
        self.manager_root = (
            manager_root or Path.home() / ".agy-cli-manager"
        )
        self.provider: CredentialProvider = get_credential_provider(self.manager_root)
        self.state_file = self.manager_root / "state.json"
        self._accounts: dict[str, dict] = {}
        self._cooldowns: dict[str, float] = {}
        self._current_active_account: str | None = None
        self.reload_accounts()

    def reload_accounts(self) -> None:
        with self._lock:
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

        for name in account_names:
            data = self.provider.read_account(name)
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

        active_blob = self.provider.read_account(active) if active else None
        if not active_blob and hasattr(self.provider, "read_blob"):
            active_blob = self.provider.read_blob("gemini:antigravity")
        if active and active_blob and "token" in active_blob and "access_token" in active_blob["token"]:
            tok = active_blob["token"]
            if active in self._accounts:
                target_acc = self._accounts[active]
                blob_email = active_blob.get("email") or tok.get("email")
                id_token_jwt = active_blob.get("id_token") or tok.get("id_token")
                if not blob_email and id_token_jwt and "." in id_token_jwt:
                    try:
                        b64_payload = id_token_jwt.split(".")[1] + "=="
                        jwt_payload = json.loads(base64.urlsafe_b64decode(b64_payload.encode()).decode("utf-8", errors="ignore"))
                        blob_email = jwt_payload.get("email")
                    except Exception:
                        pass

                if not blob_email or not target_acc.get("email") or blob_email.lower() == target_acc["email"].lower():
                    target_acc["access_token"] = tok["access_token"]

        if self._current_active_account is None and self._accounts:
            self._current_active_account = next(iter(self._accounts))

    def refresh_account_token(self, account_name: str) -> tuple[bool, str]:
        with self._lock:
            acc = self._accounts.get(account_name)
            if not acc:
                return False, f"Account '{account_name}' not found in pool"

            env_cid, env_csec = _resolve_oauth_credentials(self.manager_root)
            client_id = acc.get("client_id") or env_cid
            client_secret = acc.get("client_secret") or env_csec
            if not client_id or not client_secret:
                return False, "OAuth client credentials not configured (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)"

            refresh_token = acc.get("refresh_token")
            if not refresh_token:
                return False, f"No refresh_token for account '{account_name}'"

            payload = urllib.parse.urlencode({
                "client_id": client_id,
                "client_secret": client_secret,
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
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            new_access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 3600))
            if not new_access_token:
                return False, "Google response missing access_token"

            acc["access_token"] = new_access_token
            acc["expires_at"] = time.time() + expires_in
            acc["last_refreshed"] = time.time()
            self.clear_cooldown(account_name)

            blob = self.provider.read_account(account_name) or {}
            tok = blob.setdefault("token", {})
            tok["access_token"] = new_access_token
            tok["expiry_timestamp"] = acc["expires_at"]
            tok["expiry"] = datetime.fromtimestamp(acc["expires_at"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            tok["refresh_token"] = refresh_token
            self.provider.write_account(account_name, blob)

            # Synchronize active credential if this account is active
            if account_name == self._current_active_account:
                self.provider.sync_active(account_name, blob)

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

    def get_token_by_session(self, session_key: str | None = None, model_name: str | None = None) -> tuple[str, str]:
        with self._lock:
            if not self._accounts:
                self.reload_accounts()

            if not self._accounts:
                data = self.provider.read_account("acc1")
                if data and "token" in data and "access_token" in data["token"]:
                    return "default", data["token"]["access_token"]
                raise RuntimeError("No accounts available in pool.")

            now = time.time()
            cooldown_ok = [
                name
                for name in self._accounts
                if self._cooldowns.get(name, 0) <= now
            ]
            if not cooldown_ok:
                oldest = min(self._cooldowns.items(), key=lambda x: x[1])[0]
                cooldown_ok = [oldest]

            model_str = (model_name or "").lower()
            is_claude_or_gpt = any(k in model_str for k in ("claude", "sonnet", "opus", "gpt", "o1", "o3"))
            target_family = "third_party" if is_claude_or_gpt else "gemini"

            alive_pool = []
            for name in cooldown_ok:
                acc = self._accounts[name]
                quota = acc.get("quota")
                if not quota:
                    alive_pool.append(name)
                    continue
                family_quota = quota.get(target_family, {})
                weekly = family_quota.get("weekly", {})
                five_h = family_quota.get("5h", {})
                weekly_pct = weekly.get("percent", 100.0)
                five_h_pct = five_h.get("percent", 100.0)
                if weekly.get("disabled") or (weekly_pct <= 5.0 and weekly.get("reset_time")):
                    continue
                if five_h.get("disabled") or (five_h_pct <= 5.0 and five_h.get("reset_time")):
                    continue
                alive_pool.append(name)

            if not alive_pool:
                alive_pool = cooldown_ok

            if not session_key:
                if self._current_active_account in alive_pool:
                    chosen_name = self._current_active_account
                else:
                    chosen_name = alive_pool[0]
                    self._current_active_account = chosen_name
            else:
                import hashlib
                h = int(hashlib.md5(session_key.encode("utf-8")).hexdigest(), 16)
                chosen_name = alive_pool[h % len(alive_pool)]

            acc_data = self._accounts[chosen_name]
            needs_refresh = (
                acc_data.get("expires_at", 0) <= time.time() + 60
                and bool(acc_data.get("refresh_token"))
            )

        if needs_refresh:
            self.refresh_account_token(chosen_name)

        with self._lock:
            return chosen_name, self._accounts[chosen_name]["access_token"]

    def get_token_for_request(self) -> tuple[str, str]:
        return self.get_token_by_session(None)

    def mark_429(self, account_name: str, cooldown_seconds: int = 600) -> None:
        with self._lock:
            self._cooldowns[account_name] = time.time() + cooldown_seconds

    def clear_cooldown(self, account_name: str) -> None:
        with self._lock:
            self._cooldowns.pop(account_name, None)

    def fetch_account_quota(self, account_name: str, _retry: bool = True) -> dict | None:
        acc = self._accounts.get(account_name)
        if not acc:
            return None

        expires_at = acc.get("expires_at", 0)
        if expires_at and time.time() >= expires_at - 180:
            logger.info(f"Token for {account_name} is near expiry; refreshing before quota fetch...")
            self.refresh_account_token(account_name)

        token = acc.get("access_token")
        if not token:
            return None

        req = urllib.request.Request(
            "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
            data=json.dumps({"project": "aicode-consumers"}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            },
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=8) as resp:
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
                desc_name = (group.get("description") or "").lower()
                combined_name = f"{disp_name} {desc_name}"

                target_key = "gemini" if "gemini" in combined_name else "third_party"

                buckets = group.get("buckets", [])
                if not isinstance(buckets, list):
                    continue

                for b in buckets:
                    if not isinstance(b, dict):
                        continue
                    w = b.get("window")
                    if w not in ("5h", "weekly"):
                        continue

                    rem = b.get("remainingFraction")
                    pct = round(rem * 100, 2) if rem is not None else 100.0
                    is_disabled = bool(b.get("disabled", False))
                    bucket_id = str(b.get("bucketId") or "").lower()

                    existing_bucket = parsed_quota[target_key][w]
                    should_update = (
                        existing_bucket["reset_time"] is None
                        or (rem is not None and rem < existing_bucket["fraction"])
                        or (is_disabled and not existing_bucket.get("disabled", False))
                    )

                    if should_update:
                        parsed_quota[target_key][w] = {
                            "percent": pct,
                            "fraction": rem if rem is not None else 1.0,
                            "disabled": is_disabled,
                            "reset_time": b.get("resetTime") or existing_bucket.get("reset_time"),
                            "desc": b.get("description", "") or bucket_id,
                        }

            acc["quota"] = parsed_quota
            return parsed_quota
        except urllib.error.HTTPError as e:
            if e.code == 401 and _retry:
                logger.warning(f"Quota fetch 401 for {account_name}, attempting refresh...")
                ok, _ = self.refresh_account_token(account_name)
                if ok:
                    return self.fetch_account_quota(account_name, _retry=False)
            logger.debug(f"Could not fetch quota for {account_name}: {e}")
            return acc.get("quota")
        except Exception as e:
            logger.debug(f"Could not fetch quota for {account_name}: {e}")
            return acc.get("quota")

    def warmup_account(self, account_name: str, model: str | None = None) -> tuple[bool, str]:
        acc = self._accounts.get(account_name)
        if not acc:
            return False, "Account not found"
        token = acc.get("access_token")
        if not token:
            return False, "Missing access token"

        models_to_warm = [model] if model else ["gemini-2.5-flash", "claude-sonnet-4-6"]
        success_models = []
        errors = []

        # Pre-check: skip warming up models whose family is already exhausted on this account
        q = acc.get("quota") or {}
        gemini_exhausted = False
        claude_exhausted = False
        if q:
            g_w = q.get("gemini", {}).get("weekly", {})
            g_5 = q.get("gemini", {}).get("5h", {})
            gemini_exhausted = bool(g_w.get("disabled") or (g_w.get("percent", 100) <= 5.0 and g_w.get("reset_time")) or g_5.get("disabled") or (g_5.get("percent", 100) <= 5.0 and g_5.get("reset_time")))

            c_w = q.get("third_party", {}).get("weekly", {})
            c_5 = q.get("third_party", {}).get("5h", {})
            claude_exhausted = bool(c_w.get("disabled") or (c_w.get("percent", 100) <= 5.0 and c_w.get("reset_time")) or c_5.get("disabled") or (c_5.get("percent", 100) <= 5.0 and c_5.get("reset_time")))

        for m in models_to_warm:
            is_gem = "gemini" in m.lower()
            if is_gem and gemini_exhausted:
                logger.info(f"Skipping warmup for '{account_name}' on {m} (Gemini quota exhausted)")
                continue
            if not is_gem and claude_exhausted:
                logger.info(f"Skipping warmup for '{account_name}' on {m} (Claude/GPT quota exhausted)")
                continue

            payload = {
                "project": "aicode-consumers",
                "model": m,
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
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=12) as resp:
                    _ = resp.read()
                success_models.append(m)
                logger.info(f"Warmup successful for account '{account_name}' on {m}")
            except urllib.error.HTTPError as err:
                err_body = err.read().decode("utf-8", "ignore")
                logger.warning(f"Warmup HTTP {err.code} for '{account_name}' on {m}: {err_body[:100]}")
                if err.code == 429:
                    # Warmup probe 429 confirms model family is exhausted; don't disable the entire account
                    errors.append(f"{m}: 429 Quota Exhausted")
                else:
                    errors.append(f"{m}: HTTP {err.code}")
            except Exception as exc:
                logger.error(f"Warmup exception for '{account_name}' on {m}: {exc}")
                errors.append(f"{m}: {exc}")

        if success_models:
            return True, f"Triggered 5h window for {', '.join(success_models)}"
        return False, "; ".join(errors) or "Warmup failed"

    def warmup_all_accounts(self, model: str | None = None) -> dict[str, dict]:
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
        with self._lock:
            now = time.time()
            res = {}
            for name, acc in list(self._accounts.items()):
                cd = self._cooldowns.get(name, 0)
                expires_at = acc.get("expires_at", 0)
                res[name] = {
                    "name": name,
                    "email": acc.get("email", name),
                    "display_name": acc.get("display_name", name),
                    "is_active": cd <= now,
                    "is_current_runner": (name == self._current_active_account),
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
