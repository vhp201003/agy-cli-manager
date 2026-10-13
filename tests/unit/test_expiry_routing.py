import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from agy_cli_manager.proxy.token_manager import (
    TokenManager,
    parse_reset_time_epoch,
    format_duration_sec,
)


def test_parse_reset_time_epoch_and_format_duration():
    assert parse_reset_time_epoch(None) is None
    assert parse_reset_time_epoch("") is None
    assert parse_reset_time_epoch(1700000000) == 1700000000.0

    # Test ISO format with Z
    iso_z = "2026-10-13T12:00:00Z"
    ts = parse_reset_time_epoch(iso_z)
    assert ts is not None
    assert isinstance(ts, float)

    # Test ISO with timezone offset
    iso_offset = "2026-10-13T12:00:00+00:00"
    assert parse_reset_time_epoch(iso_offset) == ts

    # Duration formatter
    assert format_duration_sec(None) is None
    assert format_duration_sec(45) == "45s"
    assert format_duration_sec(125) == "2m 5s"
    assert format_duration_sec(3725) == "1h 2m"


def test_quarantine_lifecycle(tmp_path: Path):
    tm = TokenManager(manager_root=tmp_path)
    tm._accounts = {
        "acc1": {"access_token": "tok1", "expires_at": time.time() + 3600},
        "acc2": {"access_token": "tok2", "expires_at": time.time() + 3600},
    }

    assert not tm.is_quarantined("acc1")
    assert tm.get_quarantines() == {}

    # Quarantine acc1 for 100 seconds
    tm.quarantine_account("acc1", reason="auth_failure_401", duration_seconds=100)
    assert tm.is_quarantined("acc1")
    assert not tm.is_quarantined("acc2")

    quars = tm.get_quarantines()
    assert "acc1" in quars
    assert quars["acc1"]["reason"] == "auth_failure_401"
    assert quars["acc1"]["remaining_sec"] > 0

    # Status reflects quarantine
    status = tm.get_status()
    assert status["acc1"]["in_quarantine"] is True
    assert status["acc1"]["is_active"] is False
    assert status["acc1"]["quarantine_reason"] == "auth_failure_401"
    assert status["acc1"]["priority_tier"] == 4

    # Clear quarantine
    tm.clear_quarantine("acc1")
    assert not tm.is_quarantined("acc1")
    assert tm.get_status()["acc1"]["in_quarantine"] is False
    assert tm.get_status()["acc1"]["is_active"] is True


def test_expiry_aware_routing_prioritizes_lowest_ttl(tmp_path: Path):
    tm = TokenManager(manager_root=tmp_path)
    now = time.time()

    # acc_soon: 5h window resets in 20 minutes (1200s)
    reset_soon = datetime.fromtimestamp(now + 1200, tz=timezone.utc).isoformat()
    # acc_later: 5h window resets in 3 hours (10800s)
    reset_later = datetime.fromtimestamp(now + 10800, tz=timezone.utc).isoformat()

    tm._accounts = {
        "acc_fresh": {
            "name": "acc_fresh",
            "access_token": "tok_fresh",
            "expires_at": now + 3600,
            "quota": {
                "gemini": {
                    "5h": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "disabled": False},
                    "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "disabled": False},
                }
            },
        },
        "acc_later": {
            "name": "acc_later",
            "access_token": "tok_later",
            "expires_at": now + 3600,
            "quota": {
                "gemini": {
                    "5h": {"percent": 80.0, "fraction": 0.8, "reset_time": reset_later, "disabled": False},
                    "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "disabled": False},
                }
            },
        },
        "acc_soon": {
            "name": "acc_soon",
            "access_token": "tok_soon",
            "expires_at": now + 3600,
            "quota": {
                "gemini": {
                    "5h": {"percent": 40.0, "fraction": 0.4, "reset_time": reset_soon, "disabled": False},
                    "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "disabled": False},
                }
            },
        },
    }

    tier_soon, ttl_soon = tm.calculate_account_ttl("acc_soon", "gemini", now)
    assert tier_soon == 1
    assert 1190 <= ttl_soon <= 1210

    tier_later, ttl_later = tm.calculate_account_ttl("acc_later", "gemini", now)
    assert tier_later == 1
    assert 10790 <= ttl_later <= 10810

    tier_fresh, ttl_fresh = tm.calculate_account_ttl("acc_fresh", "gemini", now)
    assert tier_fresh == 3
    assert ttl_fresh is None

    # Expiry-Aware Routing MUST pick acc_soon first to drain perishable quota!
    chosen, token = tm.get_token_by_session()
    assert chosen == "acc_soon"
    assert token == "tok_soon"

    # When acc_soon is rate limited (429), it must route to acc_later
    tm.mark_429("acc_soon", cooldown_seconds=600)
    chosen2, token2 = tm.get_token_by_session()
    assert chosen2 == "acc_later"
    assert token2 == "tok_later"

    # When acc_later is quarantined, it must fall back to fresh reserve
    tm.quarantine_account("acc_later", reason="auth_401")
    chosen3, token3 = tm.get_token_by_session()
    assert chosen3 == "acc_fresh"
    assert token3 == "tok_fresh"


def test_quarantine_endpoints(monkeypatch, tmp_path: Path):
    import sys
    import agy_cli_manager.proxy.fastapi_app
    fastapi_module = sys.modules["agy_cli_manager.proxy.fastapi_app"]

    tm = TokenManager(manager_root=tmp_path)
    tm._accounts = {
        "test_acc": {
            "name": "test_acc",
            "access_token": "valid_tok",
            "expires_at": time.time() + 3600,
        }
    }
    monkeypatch.setattr(fastapi_module, "tm_instance", tm)

    client = TestClient(fastapi_module.app)

    # 1. Post quarantine
    resp = client.post("/api/accounts/test_acc/quarantine", json={"reason": "test_quarantine", "duration_seconds": 600})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"]["in_quarantine"] is True
    assert data["status"]["quarantine_reason"] == "test_quarantine"
    assert data["status"]["priority_tier"] == 4

    # 2. Clear quarantine
    resp2 = client.post("/api/accounts/test_acc/clear-quarantine")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"]["in_quarantine"] is False
    assert data2["status"]["is_active"] is True

    # 3. 404 on nonexistent account
    assert client.post("/api/accounts/ghost_acc/quarantine", json={"reason": "test"}).status_code == 404
    assert client.post("/api/accounts/ghost_acc/clear-quarantine").status_code == 404
    assert client.post("/api/accounts/ghost_acc/clear-cooldown").status_code == 404

    # 4. Duration clamping
    resp_clamp_low = client.post("/api/accounts/test_acc/quarantine", json={"duration_seconds": 10})
    assert resp_clamp_low.status_code == 200
    assert "Quarantined test_acc for 60s" in resp_clamp_low.json()["message"]

    resp_clamp_high = client.post("/api/accounts/test_acc/quarantine", json={"duration_seconds": 9999999})
    assert resp_clamp_high.status_code == 200
    assert "Quarantined test_acc for 604800s" in resp_clamp_high.json()["message"]


def test_tier_2_past_reset_account_routing(tmp_path: Path):
    tm = TokenManager(manager_root=tmp_path)
    now = time.time()
    reset_past = datetime.fromtimestamp(now - 300, tz=timezone.utc).isoformat()

    tm._accounts = {
        "acc_fresh_standby": {
            "name": "acc_fresh_standby",
            "access_token": "tok_fresh",
            "expires_at": now + 3600,
            "quota": {
                "gemini": {
                    "5h": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "disabled": False},
                    "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "disabled": False},
                }
            },
        },
        "acc_past_reset": {
            "name": "acc_past_reset",
            "access_token": "tok_past",
            "expires_at": now + 3600,
            "quota": {
                "gemini": {
                    "5h": {"percent": 0.0, "fraction": 0.0, "reset_time": reset_past, "disabled": False},
                    "weekly": {"percent": 100.0, "fraction": 1.0, "reset_time": None, "disabled": False},
                }
            },
        },
    }

    tier_past, ttl_past = tm.calculate_account_ttl("acc_past_reset", "gemini", now)
    assert tier_past == 2
    assert ttl_past == 0.0

    tier_fresh, ttl_fresh = tm.calculate_account_ttl("acc_fresh_standby", "gemini", now)
    assert tier_fresh == 3
    assert ttl_fresh is None

    chosen, token = tm.get_token_by_session()
    assert chosen == "acc_past_reset"
    assert token == "tok_past"

