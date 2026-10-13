from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable

logger = logging.getLogger("AgyProxy.WarmupScheduler")

DEFAULT_CONFIG = {
    "enabled": True,
    "times": ["05:00"],
    "max_retries": 3,
    "initial_delay_sec": 30,
    "backoff_factor": 2.0,
}


def _normalize_time(t: str) -> str | None:
    try:
        parts = str(t).strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except (ValueError, AttributeError):
        pass
    return None


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


class WarmupScheduler:
    def __init__(
        self,
        token_manager,
        manager_root: Path,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.tm = token_manager
        self.manager_root = Path(manager_root)
        self.config_path = self.manager_root / "config.json"
        self.on_event = on_event

        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._lock = threading.Lock()
        self._last_run_minute: str | None = None

        self.last_run: float | None = None
        self.last_status: str = "idle"
        self.retry_state: dict = {
            "in_retry": False,
            "attempt": 0,
            "max_retries": DEFAULT_CONFIG["max_retries"],
            "next_retry_sec": None,
            "next_retry_at": None,
            "failed_targets": {},
        }

        self.enabled: bool = DEFAULT_CONFIG["enabled"]
        self.times: list[str] = list(DEFAULT_CONFIG["times"])
        self.max_retries: int = DEFAULT_CONFIG["max_retries"]
        self.initial_delay_sec: int = DEFAULT_CONFIG["initial_delay_sec"]
        self.backoff_factor: float = DEFAULT_CONFIG["backoff_factor"]

        self._load_config()

    def _load_config(self) -> None:
        if not self.config_path.is_file():
            return
        try:
            full_cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            auto_cfg = full_cfg.get("auto_warmup")
            if not isinstance(auto_cfg, dict):
                return
            if "enabled" in auto_cfg:
                self.enabled = bool(auto_cfg["enabled"])
            if "times" in auto_cfg and isinstance(auto_cfg["times"], list):
                normalized = []
                for t in auto_cfg["times"]:
                    norm = _normalize_time(t)
                    if norm and norm not in normalized:
                        normalized.append(norm)
                if normalized:
                    self.times = normalized
            if "max_retries" in auto_cfg:
                self.max_retries = max(0, int(auto_cfg["max_retries"]))
            if "initial_delay_sec" in auto_cfg:
                self.initial_delay_sec = max(1, int(auto_cfg["initial_delay_sec"]))
            if "backoff_factor" in auto_cfg:
                self.backoff_factor = max(1.0, float(auto_cfg["backoff_factor"]))
            self.retry_state["max_retries"] = self.max_retries
        except Exception as exc:
            logger.warning(f"Failed to read auto_warmup config: {exc}")

    def _save_config(self) -> None:
        full_cfg = {}
        if self.config_path.is_file():
            try:
                full_cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                full_cfg = {}
        full_cfg["auto_warmup"] = {
            "enabled": self.enabled,
            "times": self.times,
            "max_retries": self.max_retries,
            "initial_delay_sec": self.initial_delay_sec,
            "backoff_factor": self.backoff_factor,
        }
        _save_json_atomic(self.config_path, full_cfg)

    def update_config(
        self,
        enabled: bool | None = None,
        times: list[str] | None = None,
        max_retries: int | None = None,
        initial_delay_sec: int | None = None,
        backoff_factor: float | None = None,
    ) -> dict:
        with self._lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if times is not None:
                normalized = []
                for t in times:
                    norm = _normalize_time(t)
                    if norm and norm not in normalized:
                        normalized.append(norm)
                if normalized:
                    self.times = normalized
            if max_retries is not None:
                self.max_retries = max(0, int(max_retries))
                self.retry_state["max_retries"] = self.max_retries
            if initial_delay_sec is not None:
                self.initial_delay_sec = max(1, int(initial_delay_sec))
            if backoff_factor is not None:
                self.backoff_factor = max(1.0, float(backoff_factor))
            self._save_config()
        return self.get_status()

    def _compute_next_run(self, now: datetime | None = None) -> tuple[datetime, int]:
        if now is None:
            now = datetime.now()
        candidates: list[datetime] = []
        times = self.times if self.times else ["05:00"]
        for t_str in times:
            try:
                parts = t_str.strip().split(":")
                h, m = int(parts[0]), int(parts[1])
                cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if cand <= now:
                    cand += timedelta(days=1)
                candidates.append(cand)
            except (ValueError, IndexError):
                continue
        if not candidates:
            default_cand = now.replace(hour=5, minute=0, second=0, microsecond=0)
            if default_cand <= now:
                default_cand += timedelta(days=1)
            candidates.append(default_cand)
        next_dt = min(candidates)
        delta_sec = max(0, int((next_dt - now).total_seconds()))
        return next_dt, delta_sec

    def get_status(self) -> dict:
        with self._lock:
            next_dt, delta_sec = self._compute_next_run()
            return {
                "enabled": self.enabled,
                "times": list(self.times),
                "last_run": self.last_run,
                "last_status": self.last_status,
                "next_run": next_dt.isoformat(),
                "next_run_in_sec": delta_sec,
                "retry_state": dict(self.retry_state),
            }

    def _notify_event(self, event_dict: dict) -> None:
        if not self.on_event:
            return
        try:
            if inspect.iscoroutinefunction(self.on_event):
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.run_coroutine_threadsafe(self.on_event(event_dict), loop)
                except RuntimeError:
                    pass
            else:
                self.on_event(event_dict)
        except Exception as exc:
            logger.debug(f"Scheduler event notification failed: {exc}")

    def _execute_cycle(self) -> dict:
        if not self._cycle_lock.acquire(blocking=False):
            logger.warning("Warmup cycle already running, skipping execution")
            return {"status": "busy", "message": "Cycle already in progress"}

        try:
            self.last_run = time.time()
            self.last_status = "running"
            self._notify_event({"type": "warmup_started", "timestamp": self.last_run})

            try:
                self.tm.refresh_all_accounts(force=False)
            except Exception as exc:
                logger.error(f"Error during pre-warmup token refresh: {exc}")

            accounts = (
                self.tm.get_account_names()
                if hasattr(self.tm, "get_account_names")
                else list(getattr(self.tm, "_accounts", {}).keys())
            )
            results: dict[str, dict] = {}
            pending_retries: dict[str, list[str]] = {}

            for acc in accounts:
                if self._stop_event.is_set():
                    self.last_status = "aborted"
                    return {"status": "aborted"}
                acc_results = self.tm.warmup_account_detailed(acc)
                results[acc] = acc_results
                transient_models = [
                    m for m, r in acc_results.items()
                    if r.get("status") == "transient_error"
                ]
                if transient_models:
                    pending_retries[acc] = transient_models

            try:
                self.tm.fetch_all_quotas()
            except Exception:
                pass

            attempt = 0
            while pending_retries and attempt < self.max_retries:
                if self._stop_event.is_set():
                    self.last_status = "aborted"
                    return {"status": "aborted"}

                attempt += 1
                delay = self.initial_delay_sec * (self.backoff_factor ** (attempt - 1))
                next_retry_at = time.time() + delay

                with self._lock:
                    self.retry_state = {
                        "in_retry": True,
                        "attempt": attempt,
                        "max_retries": self.max_retries,
                        "next_retry_sec": int(delay),
                        "next_retry_at": datetime.fromtimestamp(next_retry_at, tz=timezone.utc).isoformat(),
                        "failed_targets": {acc: list(mods) for acc, mods in pending_retries.items()},
                    }

                self._notify_event({
                    "type": "warmup_retry",
                    "attempt": attempt,
                    "max_retries": self.max_retries,
                    "next_retry_sec": int(delay),
                    "pending": {acc: list(mods) for acc, mods in pending_retries.items()},
                })

                if self._stop_event.wait(timeout=delay):
                    self.last_status = "aborted"
                    with self._lock:
                        self.retry_state["in_retry"] = False
                        self.retry_state["next_retry_sec"] = None
                    return {"status": "aborted"}

                for acc in list(pending_retries.keys()):
                    if self._stop_event.is_set():
                        self.last_status = "aborted"
                        with self._lock:
                            self.retry_state["in_retry"] = False
                            self.retry_state["next_retry_sec"] = None
                        return {"status": "aborted"}

                    mods_to_retry = pending_retries[acc]
                    retry_res = self.tm.warmup_account_detailed(acc, models=mods_to_retry)
                    results[acc].update(retry_res)

                    remaining = [
                        m for m, r in retry_res.items()
                        if r.get("status") == "transient_error"
                    ]
                    if remaining:
                        pending_retries[acc] = remaining
                    else:
                        del pending_retries[acc]

            with self._lock:
                self.retry_state = {
                    "in_retry": False,
                    "attempt": attempt,
                    "max_retries": self.max_retries,
                    "next_retry_sec": None,
                    "next_retry_at": None,
                    "failed_targets": {},
                }

            has_transient = any(
                r.get("status") == "transient_error"
                for acc_res in results.values()
                for r in acc_res.values()
            )
            has_success = any(
                r.get("status") == "success"
                for acc_res in results.values()
                for r in acc_res.values()
            )

            if not results:
                self.last_status = "idle"
            elif has_success and not has_transient:
                self.last_status = "success"
            elif has_success and has_transient:
                self.last_status = "partial"
            elif not has_success and not has_transient:
                self.last_status = "exhausted"
            else:
                self.last_status = "failed"

            try:
                self.tm.fetch_all_quotas()
            except Exception:
                pass

            self._notify_event({
                "type": "warmup_completed",
                "status": self.last_status,
                "results": results,
            })

            return {
                "status": self.last_status,
                "results": results,
                "retries_done": attempt,
            }
        except Exception as exc:
            logger.error(f"Warmup cycle failed with unexpected error: {exc}")
            self.last_status = "error"
            with self._lock:
                self.retry_state["in_retry"] = False
                self.retry_state["next_retry_sec"] = None
            self._notify_event({
                "type": "warmup_completed",
                "status": "error",
                "error": str(exc),
            })
            return {"status": "error", "error": str(exc)}
        finally:
            self._cycle_lock.release()

    async def run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = datetime.now()
                cur_hm = now.strftime("%H:%M")
                cur_day_min = now.strftime("%Y-%m-%d %H:%M")

                if self.enabled and cur_hm in self.times and self._last_run_minute != cur_day_min:
                    self._last_run_minute = cur_day_min
                    await asyncio.to_thread(self._execute_cycle)
            except Exception as exc:
                logger.error(f"Error in scheduler loop: {exc}")

            for _ in range(30):
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()

    def trigger_manual(self) -> dict:
        if self._cycle_lock.locked():
            return {"status": "busy", "message": "Warmup cycle already running"}
        t = threading.Thread(target=self._execute_cycle, daemon=True)
        t.start()
        return {"status": "triggered", "message": "Manual warmup cycle initiated"}
