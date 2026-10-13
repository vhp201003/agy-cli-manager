from datetime import datetime, timedelta
import json
from pathlib import Path
from unittest.mock import MagicMock

from agy_cli_manager.proxy.scheduler import WarmupScheduler


def test_scheduler_default_config(tmp_path: Path):
    tm = MagicMock()
    scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path)
    status = scheduler.get_status()

    assert status["enabled"] is True
    assert status["times"] == ["05:00"]
    assert status["last_run"] is None
    assert status["last_status"] == "idle"
    assert status["retry_state"]["in_retry"] is False
    assert status["retry_state"]["max_retries"] == 3


def test_scheduler_config_persistence(tmp_path: Path):
    tm = MagicMock()
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"google_client_id": "existing-id"}), encoding="utf-8")

    scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path)
    scheduler.update_config(enabled=False, times=["06:30", "18:00"], max_retries=5)

    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["google_client_id"] == "existing-id"
    assert data["auto_warmup"]["enabled"] is False
    assert data["auto_warmup"]["times"] == ["06:30", "18:00"]
    assert data["auto_warmup"]["max_retries"] == 5

    # Reload from disk
    new_scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path)
    assert new_scheduler.enabled is False
    assert new_scheduler.times == ["06:30", "18:00"]
    assert new_scheduler.max_retries == 5


def test_compute_next_run_today_and_midnight_boundary(tmp_path: Path):
    scheduler = WarmupScheduler(token_manager=MagicMock(), manager_root=tmp_path)
    scheduler.times = ["05:00"]

    # Before 05:00 today -> runs today at 05:00
    now_early = datetime(2026, 10, 13, 4, 30, 0)
    next_dt, delta = scheduler._compute_next_run(now_early)
    assert next_dt == datetime(2026, 10, 13, 5, 0, 0)
    assert delta == 1800

    # At or after 05:00 today -> runs tomorrow at 05:00
    now_late = datetime(2026, 10, 13, 23, 0, 0)
    next_dt, delta = scheduler._compute_next_run(now_late)
    assert next_dt == datetime(2026, 10, 14, 5, 0, 0)
    assert delta == 6 * 3600

    # Multi-time slot -> picks closest upcoming slot
    scheduler.times = ["05:00", "17:00"]
    now_mid = datetime(2026, 10, 13, 10, 0, 0)
    next_dt, delta = scheduler._compute_next_run(now_mid)
    assert next_dt == datetime(2026, 10, 13, 17, 0, 0)
    assert delta == 7 * 3600


def test_execute_cycle_pre_warmup_refresh(tmp_path: Path):
    tm = MagicMock()
    tm.get_account_names.return_value = []
    scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path)

    res = scheduler._execute_cycle()
    tm.refresh_all_accounts.assert_called_once_with(force=False)
    assert res["status"] == "idle"


def test_execute_cycle_success_no_retries(tmp_path: Path):
    tm = MagicMock()
    tm.get_account_names.return_value = ["acc1"]
    tm.warmup_account_detailed.return_value = {
        "gemini-2.5-flash": {"status": "success", "code": 200, "error": None},
        "claude-sonnet-4-6": {"status": "success", "code": 200, "error": None},
    }

    events = []
    scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path, on_event=events.append)
    res = scheduler._execute_cycle()

    assert res["status"] == "success"
    assert res["retries_done"] == 0
    assert scheduler.retry_state["in_retry"] is False
    assert any(e["type"] == "warmup_started" for e in events)
    assert any(e["type"] == "warmup_completed" and e["status"] == "success" for e in events)


def test_execute_cycle_transient_retry_with_backoff(tmp_path: Path, monkeypatch):
    tm = MagicMock()
    tm.get_account_names.return_value = ["acc1"]

    call_count = 0

    def mock_warmup(acc, models=None):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return {"gemini-2.5-flash": {"status": "transient_error", "code": 503, "error": "HTTP 503"}}
        return {"gemini-2.5-flash": {"status": "success", "code": 200, "error": None}}

    tm.warmup_account_detailed.side_effect = mock_warmup

    events = []
    scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path, on_event=events.append)
    monkeypatch.setattr(scheduler._stop_event, "wait", lambda timeout: False)

    res = scheduler._execute_cycle()

    assert res["status"] == "success"
    assert res["retries_done"] == 2
    retry_events = [e for e in events if e["type"] == "warmup_retry"]
    assert len(retry_events) == 2
    assert retry_events[0]["attempt"] == 1
    assert retry_events[1]["attempt"] == 2


def test_execute_cycle_terminal_429_quota_exhaustion_no_retry(tmp_path: Path):
    tm = MagicMock()
    tm.get_account_names.return_value = ["acc1"]
    tm.warmup_account_detailed.return_value = {
        "gemini-2.5-flash": {"status": "quota_exhausted", "code": 429, "error": "Quota Exhausted"}
    }

    events = []
    scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path, on_event=events.append)
    res = scheduler._execute_cycle()

    assert res["status"] == "exhausted"
    assert scheduler.last_status == "exhausted"
    assert res["retries_done"] == 0
    assert not any(e["type"] == "warmup_retry" for e in events)


def test_time_normalization(tmp_path: Path):
    from agy_cli_manager.proxy.scheduler import _normalize_time

    assert _normalize_time("5:00") == "05:00"
    assert _normalize_time(" 05:00 ") == "05:00"
    assert _normalize_time("14:30") == "14:30"
    assert _normalize_time("25:00") is None
    assert _normalize_time("bad") is None

    scheduler = WarmupScheduler(token_manager=MagicMock(), manager_root=tmp_path)
    scheduler.update_config(times=["5:00", "05:00", "18:5", "invalid"])
    assert scheduler.times == ["05:00", "18:05"]


def test_execute_cycle_partial_retry_only_probes_failed_targets(tmp_path: Path, monkeypatch):
    tm = MagicMock()
    tm.get_account_names.return_value = ["acc1", "acc2"]

    def mock_warmup(acc, models=None):
        if acc == "acc1":
            return {"gemini-2.5-flash": {"status": "success", "code": 200, "error": None}}
        if models is None:
            return {"gemini-2.5-flash": {"status": "transient_error", "code": 500, "error": "HTTP 500"}}
        return {"gemini-2.5-flash": {"status": "success", "code": 200, "error": None}}

    tm.warmup_account_detailed.side_effect = mock_warmup

    scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path)
    monkeypatch.setattr(scheduler._stop_event, "wait", lambda timeout: False)

    res = scheduler._execute_cycle()
    assert res["status"] == "success"
    assert res["retries_done"] == 1

    # Verify acc1 was only called once, while acc2 was called twice (initial + retry)
    calls = [c.args[0] for c in tm.warmup_account_detailed.call_args_list]
    assert calls.count("acc1") == 1
    assert calls.count("acc2") == 2


def test_execute_cycle_stop_event_aborts_cleanly(tmp_path: Path):
    tm = MagicMock()
    tm.get_account_names.return_value = ["acc1"]
    scheduler = WarmupScheduler(token_manager=tm, manager_root=tmp_path)
    scheduler._stop_event.set()

    res = scheduler._execute_cycle()
    assert res["status"] == "aborted"
    assert scheduler.last_status == "aborted"
