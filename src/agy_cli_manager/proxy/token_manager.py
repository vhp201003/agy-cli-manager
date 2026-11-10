from __future__ import annotations

import ctypes
import fnmatch
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import urllib.parse
import urllib.request

import base64
from datetime import datetime, timezone
import threading

from agy_cli_manager.proxy.credential_provider import get_credential_provider, CredentialProvider
from agy_cli_manager.manager import CODE_ASSIST_USER_AGENT

logger = logging.getLogger("AgyProxy.TokenManager")

GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _id_token_claims(id_token: str | None) -> dict:
    """Decode the payload of a Google id_token; {} when absent or malformed."""
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    remainder = len(payload_b64) % 4
    if remainder:
        payload_b64 += "=" * (4 - remainder)
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
    except (ValueError, TypeError):
        return {}


def credential_identity(data: dict) -> tuple[str | None, str | None]:
    """Return (signed_email, label_email) for a stored credential blob.

    The plain ``email`` field is free text that survives from whoever last wrote
    the file, while the id_token carries Google's signature. A file that says
    "account A" while holding account B's token is exactly how a banned account
    stays hidden behind a healthy-looking label, so the two are reported apart.
    """
    inner = data.get("token") if isinstance(data.get("token"), dict) else data
    signed = str(_id_token_claims(data.get("id_token") or inner.get("id_token")).get("email") or "").strip()
    label = str(data.get("email") or inner.get("email") or "").strip()
    return (signed or None, label or None)


_EFFORT_SUFFIXES = ("-extra-low", "-xhigh", "-minimal", "-medium", "-high", "-low", "-max", "-none", "-tiered")


def _strip_effort_suffix(model: str) -> str:
    """Drop trailing thinking-level and '-tiered' suffixes so ids compare by model identity."""
    name = model.strip().lower()
    changed = True
    while changed:
        changed = False
        for suffix in _EFFORT_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                changed = True
                break
    return name


class ModelPoolExhaustedError(RuntimeError):
    def __init__(self, model_name: str, pool: list[str], message: str | None = None) -> None:
        self.model_name = model_name
        self.pool = list(pool)
        super().__init__(
            message or f"All accounts in configured pool for model '{model_name}' ({', '.join(pool) or 'empty'}) are exhausted or unavailable."
        )


def _save_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_file = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, path)
    finally:
        if os.path.exists(tmp_file):
            try:
                os.unlink(tmp_file)
            except OSError:
                pass



def parse_reset_time_epoch(reset_time_val: str | int | float | None) -> float | None:
    if not reset_time_val:
        return None
    if isinstance(reset_time_val, (int, float)):
        return float(reset_time_val)
    if isinstance(reset_time_val, str):
        s = reset_time_val.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            return dt.timestamp()
        except Exception:
            pass
    return None


def format_duration_sec(sec: int | None) -> str | None:
    if sec is None:
        return None
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


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
        self.config_file = self.manager_root / "config.json"
        self._accounts: dict[str, dict] = {}
        self._cooldowns: dict[str, float] = {}
        self._quarantines: dict[str, dict] = {}
        self._model_pools: dict[str, list[str]] = {}
        self._model_catalog: dict[str, dict] = {}
        self._per_account_catalog: dict[str, set[str]] = {}
        self._catalog_ts: float = 0.0
        self._catalog_ttl: float = 600.0
        self._current_active_account: str | None = None
        self.reload_accounts()
        self.reload_model_pools()

    def reload_model_pools(self) -> dict[str, list[str]]:
        with self._lock:
            pools: dict[str, list[str]] = {}
            if self.config_file.is_file():
                try:
                    cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
                    raw_pools = cfg.get("model_pools")
                    if isinstance(raw_pools, dict):
                        for pat, accs in raw_pools.items():
                            pat_str = str(pat).strip()
                            if not pat_str:
                                continue
                            if isinstance(accs, str):
                                acc_list = [a.strip() for a in accs.split(",") if a.strip()]
                            elif isinstance(accs, list):
                                acc_list = [str(a).strip() for a in accs if str(a).strip()]
                            else:
                                continue
                            seen: list[str] = []
                            for a in acc_list:
                                if a not in seen:
                                    seen.append(a)
                            pools[pat_str] = seen
                except Exception as exc:
                    logger.warning(f"Failed to read model_pools from config.json: {exc}")
            self._model_pools = pools
            return dict(self._model_pools)

    def get_model_pools(self) -> dict[str, list[str]]:
        with self._lock:
            return {k: list(v) for k, v in self._model_pools.items()}

    def set_model_pools(self, pools: dict[str, list[str] | str]) -> dict[str, list[str]]:
        with self._lock:
            cleaned: dict[str, list[str]] = {}
            if isinstance(pools, dict):
                for pat, accs in pools.items():
                    pat_str = str(pat).strip()
                    if not pat_str:
                        continue
                    if isinstance(accs, str):
                        acc_list = [a.strip() for a in accs.split(",") if a.strip()]
                    elif isinstance(accs, list):
                        acc_list = [str(a).strip() for a in accs if str(a).strip()]
                    else:
                        continue
                    seen: list[str] = []
                    for a in acc_list:
                        if a not in seen:
                            seen.append(a)
                    if seen:
                        cleaned[pat_str] = seen

            full_cfg = {}
            if self.config_file.is_file():
                try:
                    full_cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
                except Exception:
                    full_cfg = {}
            full_cfg["model_pools"] = cleaned
            _save_json_atomic(self.config_file, full_cfg)
            self._model_pools = cleaned
            return self.get_model_pools()

    def set_model_pool_rule(self, pattern: str, accounts: list[str] | str) -> dict[str, list[str]]:
        with self._lock:
            pat_str = str(pattern).strip()
            if not pat_str:
                raise ValueError("Model pattern cannot be empty")
            current = self.get_model_pools()
            if isinstance(accounts, str):
                acc_list = [a.strip() for a in accounts.split(",") if a.strip()]
            elif isinstance(accounts, list):
                acc_list = [str(a).strip() for a in accounts if str(a).strip()]
            else:
                acc_list = []
            if not acc_list:
                current.pop(pat_str, None)
            else:
                current[pat_str] = acc_list
            return self.set_model_pools(current)

    def delete_model_pool_rule(self, pattern: str) -> dict[str, list[str]]:
        with self._lock:
            pat_str = str(pattern).strip()
            current = self.get_model_pools()
            current.pop(pat_str, None)
            return self.set_model_pools(current)

    def resolve_pool_for_model(self, model_name: str | None) -> tuple[str | None, list[str] | None]:
        with self._lock:
            if not self._model_pools:
                return None, None

            candidates: list[str] = []
            if model_name:
                raw_m = str(model_name).strip().lower()
                if raw_m:
                    candidates.append(raw_m)
                    for sep in ("|", ",", ";"):
                        if sep in raw_m:
                            for part in raw_m.split(sep):
                                p = part.strip()
                                if p and p not in candidates:
                                    candidates.append(p)
                    # A thinking level is a request parameter, not part of the model
                    # identity: `gemini-4-argon-high` must still match a pool rule
                    # declared as `gemini-4-argon`.
                    for cand in list(candidates):
                        base = _strip_effort_suffix(cand)
                        if base != cand and base not in candidates:
                            candidates.append(base)

            # 1. Exact match (case-insensitive)
            for m_norm in candidates:
                for pat, accs in self._model_pools.items():
                    if pat.lower() == m_norm:
                        return pat, list(accs)

            # 2. Specific wildcard match (longest non-wildcard pattern first)
            for m_norm in candidates:
                wildcard_matches: list[tuple[int, str, list[str]]] = []
                for pat, accs in self._model_pools.items():
                    pat_low = pat.lower()
                    if pat_low == "*":
                        continue
                    if ("*" in pat_low or "?" in pat_low or "[" in pat_low) and fnmatch.fnmatch(m_norm, pat_low):
                        specificity = len(pat_low.replace("*", "").replace("?", ""))
                        wildcard_matches.append((specificity, pat, list(accs)))
                if wildcard_matches:
                    wildcard_matches.sort(key=lambda item: (-item[0], item[1]))
                    _, best_pat, best_accs = wildcard_matches[0]
                    return best_pat, best_accs

            # 3. Catch-all '*' rule
            for pat, accs in self._model_pools.items():
                if pat.strip() == "*":
                    return pat, list(accs)

            return None, None

    def _is_account_disabled(self, account_name: str) -> bool:
        if not self.state_file.is_file():
            return False
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            meta = (state.get("accounts") or {}).get(account_name)
        except Exception:
            return False
        return isinstance(meta, dict) and not meta.get("enabled", True)

    def reload_accounts(self) -> None:
        with self._lock:
            account_names: list[str] = []
            active = None
            disabled: set[str] = set()
            if self.state_file.is_file():
                try:
                    state = json.loads(self.state_file.read_text(encoding="utf-8"))
                    accounts_state = state.get("accounts") or {}
                    account_names = list(accounts_state.keys())
                    disabled = {
                        name
                        for name, meta in accounts_state.items()
                        if isinstance(meta, dict) and not meta.get("enabled", True)
                    }
                    active = state.get("active")
                    if active and active in account_names:
                        account_names.remove(active)
                        account_names.insert(0, active)
                except Exception:
                    pass

            for name in account_names:
                if name in disabled:
                    # A disabled account must not stay routable just because it
                    # was loaded before it was disabled.
                    self._accounts.pop(name, None)
                    continue
                data = self.provider.read_account(name)
                if not data:
                    fallback_tok_path = self.manager_root / "accounts" / name / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                    if fallback_tok_path.is_file():
                        try:
                            data = json.loads(fallback_tok_path.read_text(encoding="utf-8"))
                        except Exception:
                            data = None
                if data and "token" in data:
                    tok = data["token"]
                    access_token = tok.get("access_token")
                    if access_token:
                        existing = self._accounts.get(name, {})
                        # Preserve newer in-memory token if local file was not updated yet
                        if existing.get("access_token") and existing.get("expires_at", 0) > (tok.get("expiry_timestamp") or 0):
                            continue

                        # Calculate expiry
                        expires_at = tok.get("expiry_timestamp")
                        if not expires_at:
                            # parse RFC3339 if available or assume 1 hr
                            expires_at = time.time() + 3600

                        signed_email, label_email = credential_identity(data)
                        claims = _id_token_claims(data.get("id_token") or tok.get("id_token"))
                        email = signed_email or label_email or tok.get("email")
                        display_name = data.get("name") or claims.get("name")
                        if signed_email and label_email and signed_email.lower() != label_email.lower():
                            logger.warning(
                                "Account '%s' credential says %s but its id_token belongs to %s; "
                                "the slot is serving the latter",
                                name,
                                label_email,
                                signed_email,
                            )

                        recorded_email = (
                            (accounts_state.get(name) or {}).get("identity") or {}
                        ).get("email")
                        if (
                            recorded_email
                            and email
                            and recorded_email.lower() != email.lower()
                        ):
                            logger.warning(
                                "Account slot '%s' holds credential for %s but state records %s; "
                                "requests routed here use the stored account",
                                name,
                                email,
                                recorded_email,
                            )

                        self._accounts[name] = {
                            "name": name,
                            "access_token": access_token,
                            "refresh_token": tok.get("refresh_token"),
                            "expiry": tok.get("expiry"),
                            "expires_at": expires_at,
                            "email": email or name,
                            "label_email": label_email,
                            "display_name": display_name or name,
                            "recorded_email": recorded_email,
                            "last_refreshed": existing.get("last_refreshed", 0),
                            "is_gcp_tos": bool(data.get("is_gcp_tos") or claims.get("is_gcp_tos") or tok.get("is_gcp_tos")),
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

    def hot_reload_account(self, account_name: str, fetch_quota: bool = False) -> bool:
        with self._lock:
            if self._is_account_disabled(account_name):
                self._accounts.pop(account_name, None)
                return False
            data = self.provider.read_account(account_name)
            if not data:
                fallback_tok_path = (
                    self.manager_root / "accounts" / account_name / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                )
                if fallback_tok_path.is_file():
                    try:
                        data = json.loads(fallback_tok_path.read_text(encoding="utf-8"))
                    except Exception:
                        data = None
            if not data or not isinstance(data.get("token"), dict):
                return False
            tok = data["token"]
            access_token = str(tok.get("access_token") or "").strip()
            if not access_token:
                return False
            expires_at = tok.get("expiry_timestamp") or (time.time() + 3600)
            signed_email, label_email = credential_identity(data)
            email = signed_email or label_email or tok.get("email") or account_name
            display_name = (
                data.get("name")
                or _id_token_claims(data.get("id_token") or tok.get("id_token")).get("name")
                or tok.get("name")
                or account_name
            )
            existing = self._accounts.get(account_name, {})
            self._accounts[account_name] = {
                "name": account_name,
                "access_token": access_token,
                "refresh_token": tok.get("refresh_token"),
                "expiry": tok.get("expiry"),
                "expires_at": float(expires_at),
                "email": email,
                "label_email": label_email,
                "display_name": display_name,
                "last_refreshed": existing.get("last_refreshed", 0),
                "is_gcp_tos": bool(data.get("is_gcp_tos") or _id_token_claims(data.get("id_token") or tok.get("id_token")).get("is_gcp_tos") or tok.get("is_gcp_tos")),
            }
            if self._current_active_account is None:
                self._current_active_account = account_name
        if fetch_quota:
            try:
                self.fetch_account_quota(account_name)
            except Exception:
                pass
        return True

    def set_current_active_account(self, account_name: str | None) -> None:
        with self._lock:
            self._current_active_account = account_name

    def remove_account(self, account_name: str) -> bool:
        with self._lock:
            existed = account_name in self._accounts
            self._accounts.pop(account_name, None)
            self._cooldowns.pop(account_name, None)
            self._quarantines.pop(account_name, None)
            if hasattr(self, "_quotas") and isinstance(getattr(self, "_quotas"), dict):
                getattr(self, "_quotas").pop(account_name, None)
            if self._current_active_account == account_name:
                self._current_active_account = next(iter(self._accounts), None)
            if self._model_pools:
                updated_pools: dict[str, list[str]] = {}
                changed = False
                for pat, accs in self._model_pools.items():
                    if account_name in accs:
                        changed = True
                        remaining = [a for a in accs if a != account_name]
                        if remaining:
                            updated_pools[pat] = remaining
                    else:
                        updated_pools[pat] = list(accs)
                if changed:
                    self.set_model_pools(updated_pools)
            return existed

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
            new_id_token = data.get("id_token")
            expires_in = int(data.get("expires_in", 3600))
            if not new_access_token:
                return False, "Google response missing access_token"

            claims = _id_token_claims(new_id_token) if new_id_token else {}
            jwt_email = str(claims.get("email") or "").strip()
            jwt_name = str(claims.get("name") or "").strip()

            with self._lock:
                acc["access_token"] = new_access_token
                acc["expires_at"] = time.time() + expires_in
                acc["last_refreshed"] = time.time()
                if jwt_email:
                    acc["email"] = jwt_email
                    acc["label_email"] = jwt_email
                if jwt_name:
                    acc["display_name"] = jwt_name
                self._cooldowns.pop(account_name, None)

            blob = self.provider.read_account(account_name) or {}
            tok = blob.setdefault("token", {})
            tok["access_token"] = new_access_token
            if new_id_token:
                tok["id_token"] = new_id_token
                blob["id_token"] = new_id_token
            if jwt_email:
                tok["email"] = jwt_email
                blob["email"] = jwt_email
            if jwt_name:
                blob["name"] = jwt_name
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

    def quarantine_account(self, account_name: str, reason: str = "auth_failure", duration_seconds: int = 1800) -> None:
        with self._lock:
            now = time.time()
            self._quarantines[account_name] = {
                "quarantined_until": now + duration_seconds,
                "reason": reason,
                "quarantined_at": now,
            }
            logger.warning(f"Account '{account_name}' quarantined for {duration_seconds}s (reason: {reason})")

    def clear_quarantine(self, account_name: str) -> None:
        with self._lock:
            self._quarantines.pop(account_name, None)
            logger.info(f"Account '{account_name}' released from quarantine")

    def is_quarantined(self, account_name: str) -> bool:
        with self._lock:
            q = self._quarantines.get(account_name)
            if not q:
                return False
            if time.time() >= q.get("quarantined_until", 0):
                self._quarantines.pop(account_name, None)
                return False
            return True

    def get_quarantines(self) -> dict[str, dict]:
        with self._lock:
            now = time.time()
            res = {}
            for k, v in list(self._quarantines.items()):
                if now < v.get("quarantined_until", 0):
                    res[k] = {
                        **v,
                        "remaining_sec": max(0, int(v["quarantined_until"] - now)),
                    }
                else:
                    self._quarantines.pop(k, None)
            return res

    def calculate_account_ttl(self, account_name: str, target_family: str = "gemini", now: float | None = None) -> tuple[int, float | None]:
        if now is None:
            now = time.time()
        with self._lock:
            acc = self._accounts.get(account_name)
            if not acc:
                return (3, None)
            quota = acc.get("quota") or {}
            five_h = quota.get(target_family, {}).get("5h", {})
            weekly = quota.get(target_family, {}).get("weekly", {})
            reset_time_raw = five_h.get("reset_time")
            reset_epoch = parse_reset_time_epoch(reset_time_raw)
            rem_fraction = float(five_h.get("fraction", 1.0) if five_h.get("fraction") is not None else 1.0)
            rem_percent = float(five_h.get("percent", 100.0) if five_h.get("percent") is not None else 100.0)
            is_disabled = bool(five_h.get("disabled") or weekly.get("disabled"))
            if is_disabled:
                return (3, None)

            if reset_epoch is not None and reset_epoch <= now:
                return (2, 0.0)

            # Fresh reserve (>= 99.5% full) or already exhausted (<= 5.0%)
            if rem_fraction >= 0.995 or rem_percent >= 99.5 or rem_percent <= 5.0:
                return (3, None)

            if reset_epoch is not None and reset_epoch > now:
                ttl = reset_epoch - now
                return (1, ttl)
            else:
                return (3, None)

    def declared_models(self) -> list[str]:
        """Models an operator explicitly routed via model_pools.

        A pool entry is a deliberate statement that the model is served by a
        specific account set, so it stays usable even when no account currently
        advertises it (upstream answers honestly, usually 404).
        """
        with self._lock:
            return list(self._model_pools)

    def quarantine_for_tos(self, account_name: str, body: bytes | str = b"") -> bool:
        """Quarantine an account when upstream reports a Terms-of-Service block.

        A ToS violation is account-level and permanent; retrying every 30 minutes
        just burns requests, so the account is parked for a long window.
        """
        blob = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)
        if "TOS_VIOLATION" in blob or "violation of Terms of Service" in blob:
            self.quarantine_account(account_name, reason="tos_violation", duration_seconds=30 * 86400)
            return True
        return False

    def get_model_catalog(self, force: bool = False) -> dict[str, dict]:
        """Return the union of upstream-advertised models across loaded accounts.

        The upstream catalog is per-account and changes without notice (an id can
        disappear between two days), so it is fetched rather than hardcoded.
        """
        with self._lock:
            fresh = time.time() - self._catalog_ts < self._catalog_ttl
            if self._model_catalog and fresh and not force:
                return dict(self._model_catalog)

            merged: dict[str, dict] = {}
            per_account: dict[str, set[str]] = {}
            for name in list(self._accounts):
                token = self._accounts[name].get("access_token")
                if not token:
                    continue
                advertised = self._fetch_available_models(token)
                if advertised:
                    per_account[name] = set(advertised)
                for mid, info in advertised.items():
                    merged.setdefault(mid, info)
            if merged:
                self._model_catalog = merged
                self._per_account_catalog = per_account
                self._catalog_ts = time.time()
            return dict(self._model_catalog)

    def _accounts_advertising(self, model_name: str | None, pool: list[str]) -> list[str]:
        """Subset of `pool` whose catalog lists the requested model id."""
        if not model_name:
            return []
        self.get_model_catalog()
        if not self._per_account_catalog:
            return []
        candidates = {str(model_name).strip().lower()}
        for part in str(model_name).split("|"):
            if part.strip():
                candidates.add(part.strip().lower())
        candidates |= {_strip_effort_suffix(c) for c in list(candidates)}

        capable = [
            name
            for name in pool
            if candidates & {mid.lower() for mid in self._per_account_catalog.get(name, ())}
        ]
        return capable

    @staticmethod
    def _fetch_available_models(token: str) -> dict[str, dict]:
        req = urllib.request.Request(
            "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            },
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("fetchAvailableModels failed: %s", exc)
            return {}
        models = payload.get("models")
        return models if isinstance(models, dict) else {}

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
            matched_pattern, configured_pool = self.resolve_pool_for_model(model_name)
            is_strict_pool = configured_pool is not None

            if is_strict_pool:
                candidate_pool = [name for name in configured_pool if name in self._accounts]
                if not candidate_pool:
                    raise ModelPoolExhaustedError(
                        model_name or "*",
                        configured_pool,
                        f"Configured account pool for model '{model_name or '*'}' (rule '{matched_pattern}') has no valid accounts in pool: {configured_pool}",
                    )
            else:
                candidate_pool = list(self._accounts.keys())

            # 1. Exclude quarantined accounts
            healthy_pool = [name for name in candidate_pool if not self.is_quarantined(name)]
            if not healthy_pool:
                if is_strict_pool:
                    raise ModelPoolExhaustedError(
                        model_name or "*",
                        candidate_pool,
                        f"All accounts in pool for model '{model_name or '*'}' (rule '{matched_pattern}') are quarantined: {candidate_pool}",
                    )
                healthy_pool = list(candidate_pool)

            # 2. Exclude accounts in 429 cooldown
            cooldown_ok = [
                name
                for name in healthy_pool
                if self._cooldowns.get(name, 0) <= now
            ]
            if not cooldown_ok:
                if is_strict_pool:
                    raise ModelPoolExhaustedError(
                        model_name or "*",
                        candidate_pool,
                        f"All accounts in pool for model '{model_name or '*'}' (rule '{matched_pattern}') are in 429 cooldown: {candidate_pool}",
                    )
                oldest = min(healthy_pool, key=lambda x: self._cooldowns.get(x, 0))
                cooldown_ok = [oldest]

            model_str = (model_name or "").lower()
            is_claude_or_gpt = any(k in model_str for k in ("claude", "sonnet", "opus", "gpt", "o1", "o3"))
            target_family = "third_party" if is_claude_or_gpt else "gemini"

            # An account that does not advertise the model cannot serve it: upstream
            # answers 404 even when the account is healthy. When at least one candidate
            # advertises it, drop the rest; when none do we keep the pool as-is so a
            # declared-but-unlisted id still reaches upstream and fails honestly.
            capable = self._accounts_advertising(model_name, cooldown_ok)
            if capable:
                cooldown_ok = capable

            # 3. Filter out quota-exhausted accounts
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
                weekly_epoch = parse_reset_time_epoch(weekly.get("reset_time"))
                five_h_epoch = parse_reset_time_epoch(five_h.get("reset_time"))
                weekly_pct = float(weekly.get("percent", 100.0) or 0.0)
                five_h_pct = float(five_h.get("percent", 100.0) or 0.0)
                if weekly.get("disabled") or (weekly_pct <= 5.0 and weekly_epoch is not None and weekly_epoch > now):
                    continue
                if five_h.get("disabled") or (five_h_pct <= 5.0 and five_h_epoch is not None and five_h_epoch > now):
                    continue
                alive_pool.append(name)

            if not alive_pool:
                if is_strict_pool:
                    raise ModelPoolExhaustedError(
                        model_name or "*",
                        candidate_pool,
                        f"All accounts in pool for model '{model_name or '*'}' (rule '{matched_pattern}') have exhausted {target_family} quota: {candidate_pool}",
                    )
                alive_pool = cooldown_ok

            # 4. Expiry-Aware Sorting (Perishable Quota Arbitrage):
            # Prioritize accounts whose 5H window resets soonest (lowest TTL)
            # Tier 1 (Active 5H window expiring soonest): sorted by TTL ASC
            # Tier 2 (Window already passed reset time): sorted by name
            # Tier 3 (Fresh reserve with no active clock): preserved as standby
            def _sort_key(name: str):
                tier, ttl = self.calculate_account_ttl(name, target_family, now)
                return (tier, ttl if ttl is not None else float("inf"), name)

            alive_pool.sort(key=_sort_key)

            chosen_name = alive_pool[0]
            self._current_active_account = chosen_name

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
                "User-Agent": CODE_ASSIST_USER_AGENT,
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
                if not isinstance(group, dict):
                    continue
                disp_name = (group.get("displayName") or "").lower()
                desc_name = (group.get("description") or "").lower()
                combined_name = f"{disp_name} {desc_name}"
                buckets = group.get("buckets", [])
                if not isinstance(buckets, list):
                    continue
                label_family = "gemini" if "gemini" in combined_name else "third_party"

                for b in buckets:
                    if not isinstance(b, dict):
                        continue
                    bucket_id = str(b.get("bucketId") or "").lower()
                    if bucket_id.startswith("gemini-"):
                        target_key = "gemini"
                    elif bucket_id.startswith("3p-"):
                        target_key = "third_party"
                    else:
                        target_key = label_family
                    w = b.get("window")
                    if w not in ("5h", "weekly"):
                        continue

                    rem = b.get("remainingFraction")
                    pct = round(rem * 100, 2) if rem is not None else 100.0
                    if rem is not None and rem < 1.0 and pct >= 100.0:
                        pct = 99.99
                    is_disabled = bool(b.get("disabled", False))

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

    def get_account_names(self) -> list[str]:
        with self._lock:
            return list(self._accounts.keys())

    def warmup_account_detailed(self, account_name: str, models: list[str] | None = None) -> dict[str, dict]:
        models_to_warm = models if models is not None else ["gemini-2.5-flash", "claude-sonnet-4-6"]
        with self._lock:
            acc = self._accounts.get(account_name)
        if not acc:
            return {m: {"status": "permanent_error", "code": 404, "error": "Account not found"} for m in models_to_warm}

        token = acc.get("access_token")
        if not token:
            self.refresh_account_token(account_name)
            with self._lock:
                acc = self._accounts.get(account_name) or {}
                token = acc.get("access_token")
        if not token:
            return {m: {"status": "permanent_error", "code": 401, "error": "Missing access token"} for m in models_to_warm}

        results: dict[str, dict] = {}
        q = acc.get("quota") or {}
        gemini_exhausted = False
        claude_exhausted = False
        now = time.time()
        if q:
            g_w = q.get("gemini", {}).get("weekly", {})
            g_5 = q.get("gemini", {}).get("5h", {})
            gw_epoch = parse_reset_time_epoch(g_w.get("reset_time"))
            g5_epoch = parse_reset_time_epoch(g_5.get("reset_time"))
            gemini_exhausted = bool(
                g_w.get("disabled")
                or (g_w.get("percent", 100) <= 5.0 and gw_epoch is not None and gw_epoch > now)
                or g_5.get("disabled")
                or (g_5.get("percent", 100) <= 5.0 and g5_epoch is not None and g5_epoch > now)
            )

            c_w = q.get("third_party", {}).get("weekly", {})
            c_5 = q.get("third_party", {}).get("5h", {})
            cw_epoch = parse_reset_time_epoch(c_w.get("reset_time"))
            c5_epoch = parse_reset_time_epoch(c_5.get("reset_time"))
            claude_exhausted = bool(
                c_w.get("disabled")
                or (c_w.get("percent", 100) <= 5.0 and cw_epoch is not None and cw_epoch > now)
                or c_5.get("disabled")
                or (c_5.get("percent", 100) <= 5.0 and c5_epoch is not None and c5_epoch > now)
            )

        for m in models_to_warm:
            is_gem = "gemini" in m.lower()
            if is_gem and gemini_exhausted:
                logger.info(f"Skipping warmup for '{account_name}' on {m} (Gemini quota exhausted)")
                results[m] = {"status": "quota_exhausted", "code": 429, "error": "Quota Exhausted"}
                continue
            if not is_gem and claude_exhausted:
                logger.info(f"Skipping warmup for '{account_name}' on {m} (Claude/GPT quota exhausted)")
                results[m] = {"status": "quota_exhausted", "code": 429, "error": "Quota Exhausted"}
                continue

            payload = {
                "project": "aicode-consumers",
                "model": m,
                "request": {
                    "contents": [{"parts": [{"text": "ping"}]}],
                    "generationConfig": {"maxOutputTokens": 1},
                },
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
                results[m] = {"status": "success", "code": 200, "error": None}
                logger.info(f"Warmup successful for account '{account_name}' on {m}")
            except urllib.error.HTTPError as err:
                err_body = err.read().decode("utf-8", "ignore")
                logger.warning(f"Warmup HTTP {err.code} for '{account_name}' on {m}: {err_body[:100]}")
                if err.code == 429:
                    results[m] = {"status": "quota_exhausted", "code": 429, "error": "Quota Exhausted"}
                elif err.code == 401:
                    self.refresh_account_token(account_name)
                    results[m] = {"status": "permanent_error", "code": 401, "error": "HTTP 401 Unauthorized"}
                elif err.code in (500, 502, 503, 504):
                    results[m] = {"status": "transient_error", "code": err.code, "error": f"HTTP {err.code}"}
                else:
                    results[m] = {"status": "permanent_error", "code": err.code, "error": f"HTTP {err.code}"}
            except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
                logger.error(f"Warmup transient network error for '{account_name}' on {m}: {exc}")
                results[m] = {"status": "transient_error", "code": getattr(exc, "code", None), "error": str(exc)}
            except Exception as exc:
                logger.error(f"Warmup exception for '{account_name}' on {m}: {exc}")
                results[m] = {"status": "transient_error", "code": None, "error": str(exc)}

        return results

    def warmup_account(self, account_name: str, model: str | None = None) -> tuple[bool, str]:
        acc = self._accounts.get(account_name)
        if not acc:
            return False, "Account not found"
        token = acc.get("access_token")
        if not token:
            return False, "Missing access token"

        models = [model] if model else ["gemini-2.5-flash", "claude-sonnet-4-6"]
        detailed = self.warmup_account_detailed(account_name, models=models)
        success_models = [m for m, r in detailed.items() if r.get("status") == "success"]
        errors = [f"{m}: {r.get('error') or r.get('status')}" for m, r in detailed.items() if r.get("status") != "success"]
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
                in_quarantine = self.is_quarantined(name)
                quarantine_info = self._quarantines.get(name, {})
                quarantine_remaining_sec = max(0, int(quarantine_info.get("quarantined_until", 0) - now)) if in_quarantine else 0
                quarantine_reason = quarantine_info.get("reason") if in_quarantine else None

                quota = acc.get("quota") or {}
                five_h = quota.get("gemini", {}).get("5h", {})
                reset_time_raw = five_h.get("reset_time")
                reset_epoch = parse_reset_time_epoch(reset_time_raw)
                rem_fraction = float(five_h.get("fraction", 1.0) if five_h.get("fraction") is not None else 1.0)
                rem_percent = float(five_h.get("percent", 100.0) if five_h.get("percent") is not None else 100.0)
                is_actively_draining = (rem_fraction < 0.995 and rem_percent < 99.5 and rem_percent > 5.0)
                time_until_5h_reset_sec = max(0, int(reset_epoch - now)) if (reset_epoch and reset_epoch > now and is_actively_draining) else None
                time_until_5h_reset_formatted = format_duration_sec(time_until_5h_reset_sec)

                tier, _ = self.calculate_account_ttl(name, "gemini", now)
                priority_tier = 4 if (in_quarantine or cd > now) else tier

                label_email = acc.get("label_email")
                email = acc.get("email", name)
                res[name] = {
                    "name": name,
                    "email": email,
                    "identity_email": label_email,
                    "identity_mismatch": bool(
                        label_email and email and label_email.lower() != email.lower()
                    ),
                    "display_name": acc.get("display_name", name),
                    "is_active": (cd <= now) and (not in_quarantine),
                    "is_current_runner": (name == self._current_active_account),
                    "in_cooldown": cd > now,
                    "cooldown_remaining_sec": max(0, int(cd - now)),
                    "in_quarantine": in_quarantine,
                    "quarantine_remaining_sec": quarantine_remaining_sec,
                    "quarantine_reason": quarantine_reason,
                    "time_until_5h_reset_sec": time_until_5h_reset_sec,
                    "time_until_5h_reset_formatted": time_until_5h_reset_formatted,
                    "priority_tier": priority_tier,
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
