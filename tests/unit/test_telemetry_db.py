from __future__ import annotations

import time
import pytest
from agy_cli_manager.proxy.db import TelemetryDB


@pytest.fixture
def telemetry_db(tmp_path):
    db_file = tmp_path / "telemetry.db"
    db = TelemetryDB(db_path=db_file)
    db.start()
    yield db
    db.close()


def test_schema_init_and_wal_mode(telemetry_db, tmp_path):
    assert (tmp_path / "telemetry.db").exists()
    with telemetry_db.get_read_conn() as conn:
        cursor = conn.cursor()
        mode = cursor.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"

        tables = [r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        assert "request_logs" in tables


def test_enqueue_and_flush_logs(telemetry_db):
    event = {
        "id": "req-1",
        "time": "12:00:00",
        "method": "POST",
        "path": "/v1/chat/completions",
        "host": "daily-cloudcode-pa.googleapis.com",
        "account": "acc-1",
        "model": "gemini-2.5-flash",
        "status": 200,
        "status_text": "200 OK",
        "latency_ms": 150.5,
        "body": "{\"prompt\": \"hello\"}",
        "body_size": 20,
        "response_preview": "{\"response\": \"world\"}",
        "response_size": 22,
        "tokens": {"prompt_tokens": 15, "completion_tokens": 25, "total_tokens": 40},
        "retried": False,
    }

    telemetry_db.log_event(event)
    telemetry_db.flush()

    logs, total = telemetry_db.query_logs(limit=10)
    assert total == 1
    assert len(logs) == 1
    assert logs[0]["id"] == "req-1"
    assert logs[0]["account"] == "acc-1"
    assert logs[0]["model"] == "gemini-2.5-flash"
    assert logs[0]["tokens"]["total_tokens"] == 40
    assert logs[0]["tokens"]["prompt_tokens"] == 15
    assert logs[0]["tokens"]["completion_tokens"] == 25


def test_query_aggregate_stats(telemetry_db):
    # Log two events: one success, one 429
    telemetry_db.log_event({
        "id": "req-1",
        "method": "POST",
        "path": "/test",
        "account": "acc-1",
        "model": "gemini-2.5-flash",
        "status": 200,
        "latency_ms": 100.0,
        "tokens": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "retried": False,
    })
    telemetry_db.log_event({
        "id": "req-2",
        "method": "POST",
        "path": "/test",
        "account": "acc-2",
        "model": "claude-3-7-sonnet",
        "status": 429,
        "latency_ms": 50.0,
        "tokens": None,
        "retried": True,
    })
    telemetry_db.flush()

    stats = telemetry_db.query_aggregate_stats()
    assert stats["total_requests"] == 2
    assert stats["total_tokens"] == 30
    assert stats["prompt_tokens"] == 10
    assert stats["completion_tokens"] == 20
    assert stats["failover_count"] == 1
    assert stats["success_rate"] == 50.0
    assert stats["avg_latency_ms"] == 75


def test_query_logs_filtering_and_pagination(telemetry_db):
    for i in range(5):
        telemetry_db.log_event({
            "id": f"req-{i}",
            "method": "POST",
            "path": f"/path-{i}",
            "account": "acc-1" if i < 3 else "acc-2",
            "model": "model-A" if i % 2 == 0 else "model-B",
            "status": 200 if i < 4 else 500,
            "latency_ms": 10.0 * i,
            "tokens": {"total": 10},
        })
    telemetry_db.flush()

    logs, total = telemetry_db.query_logs(limit=2, offset=0)
    assert total == 5
    assert len(logs) == 2

    # Filter by account
    acc1_logs, acc1_total = telemetry_db.query_logs(account="acc-1")
    assert acc1_total == 3
    assert len(acc1_logs) == 3

    # Filter by status
    err_logs, err_total = telemetry_db.query_logs(status=500)
    assert err_total == 1
    assert err_logs[0]["id"] == "req-4"


def test_body_clamping_and_retention_prune(telemetry_db):
    huge_body = "x" * 20000  # 20 KB
    telemetry_db.log_event({
        "id": "req-huge",
        "method": "POST",
        "path": "/huge",
        "account": "acc-1",
        "status": 200,
        "body": huge_body,
        "body_size": len(huge_body),
    })
    telemetry_db.flush()

    logs, _ = telemetry_db.query_logs(limit=1)
    # Body must be clamped to at most 16384 chars
    assert len(logs[0]["body"]) <= 16384
    assert logs[0]["body_size"] == 20000

    # Prune simulation
    pruned_bodies, pruned_rows = telemetry_db.prune_retention(body_retention_days=0, row_retention_days=30)
    assert pruned_bodies >= 1
    logs_after, _ = telemetry_db.query_logs(limit=1)
    assert logs_after[0]["body"] is None


def test_query_token_analytics(telemetry_db):
    telemetry_db.log_event({
        "id": "req-1",
        "method": "POST",
        "path": "/test",
        "account": "acc-1",
        "model": "gemini-2.5-flash",
        "status": 200,
        "tokens": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    })
    telemetry_db.log_event({
        "id": "req-2",
        "method": "POST",
        "path": "/test",
        "account": "acc-2",
        "model": "gemini-2.5-flash",
        "status": 200,
        "tokens": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
    })
    telemetry_db.flush()

    analytics = telemetry_db.query_token_analytics(range_days=7)
    assert "timeline" in analytics
    assert "by_account" in analytics
    assert "by_model" in analytics

    assert len(analytics["by_model"]) == 1
    assert analytics["by_model"][0]["model"] == "gemini-2.5-flash"
    assert analytics["by_model"][0]["total_tokens"] == 450

    accounts = {item["account"]: item["total_tokens"] for item in analytics["by_account"]}
    assert accounts["acc-1"] == 150
    assert accounts["acc-2"] == 300


def test_concurrent_writes_no_lock_error(telemetry_db):
    import concurrent.futures

    def _worker(thread_id: int):
        for j in range(5):
            telemetry_db.log_event({
                "id": f"th-{thread_id}-req-{j}",
                "method": "POST",
                "path": f"/concurrent-{thread_id}",
                "account": f"acc-{thread_id % 3}",
                "model": "gemini-2.5-flash",
                "status": 200,
                "latency_ms": 25.0,
                "tokens": {"total": 10},
            })
            time.sleep(0.005)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_worker, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    telemetry_db.flush()

    stats = telemetry_db.query_aggregate_stats()
    assert stats["total_requests"] == 50
    assert stats["total_tokens"] == 500

