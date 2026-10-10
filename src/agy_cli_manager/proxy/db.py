from __future__ import annotations

import contextlib
import logging
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any, Generator

logger = logging.getLogger("AgyProxy.DB")

_MAX_BODY_CHARS = 16384


class TelemetryDB:
    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = Path.home() / ".agy-cli-manager" / "telemetry.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=5000)
        self._stop_event = threading.Event()
        self._writer_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL;")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    time_str TEXT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    host TEXT,
                    account TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'unknown',
                    status INTEGER NOT NULL,
                    status_text TEXT,
                    latency_ms REAL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    retried INTEGER NOT NULL DEFAULT 0,
                    body_size INTEGER NOT NULL DEFAULT 0,
                    response_size INTEGER NOT NULL DEFAULT 0,
                    body TEXT,
                    response_preview TEXT
                );
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_created ON request_logs(created_at DESC);")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_requests_token_agg 
                ON request_logs(created_at, account, model, total_tokens, prompt_tokens, completion_tokens);
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON request_logs(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_account ON request_logs(account);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_model ON request_logs(model);")
            conn.commit()

    def start(self) -> None:
        with self._lock:
            if self._writer_thread is None or not self._writer_thread.is_alive():
                self._stop_event.clear()
                self._writer_thread = threading.Thread(
                    target=self._writer_loop,
                    name="AgyTelemetryWriter",
                    daemon=True,
                )
                self._writer_thread.start()

    def log_event(self, event: dict[str, Any]) -> None:
        if not event:
            return
        if self._writer_thread is None or not self._writer_thread.is_alive():
            self.start()

        clamped = dict(event)
        body = clamped.get("body")
        if isinstance(body, str) and len(body) > _MAX_BODY_CHARS:
            clamped["body"] = body[:_MAX_BODY_CHARS]

        resp = clamped.get("response_preview")
        if isinstance(resp, str) and len(resp) > _MAX_BODY_CHARS:
            clamped["response_preview"] = resp[:_MAX_BODY_CHARS]

        try:
            self._queue.put_nowait(clamped)
        except queue.Full:
            logger.warning("Telemetry log queue full; dropping event to preserve latency")

    def _writer_loop(self) -> None:
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute("PRAGMA busy_timeout = 5000;")
            while not self._stop_event.is_set() or not self._queue.empty():
                batch = []
                try:
                    item = self._queue.get(timeout=0.1)
                    if item is not None:
                        batch.append(item)
                    while len(batch) < 50:
                        extra = self._queue.get_nowait()
                        if extra is not None:
                            batch.append(extra)
                        self._queue.task_done()
                except queue.Empty:
                    pass

                if batch:
                    self._insert_batch(conn, batch)
                    for _ in range(1 if item is not None else 0):
                        try:
                            self._queue.task_done()
                        except ValueError:
                            pass

    def _insert_batch(self, conn: sqlite3.Connection, batch: list[dict[str, Any]]) -> None:
        rows = []
        now = time.time()
        for ev in batch:
            tokens = ev.get("tokens") or {}
            prompt_tok = tokens.get("prompt_tokens") or tokens.get("prompt") or 0
            compl_tok = tokens.get("completion_tokens") or tokens.get("completion") or 0
            total_tok = tokens.get("total_tokens") or tokens.get("total") or (prompt_tok + compl_tok)

            rows.append((
                ev.get("id") or str(now),
                ev.get("created_at") or now,
                ev.get("time") or time.strftime("%H:%M:%S", time.localtime(now)),
                ev.get("method") or "POST",
                ev.get("path") or "/",
                ev.get("host") or "",
                ev.get("account") or "unknown",
                ev.get("model") or "unknown",
                int(ev.get("status") or 200),
                ev.get("status_text") or "",
                float(ev.get("latency_ms") or 0.0),
                int(prompt_tok),
                int(compl_tok),
                int(total_tok),
                1 if ev.get("retried") else 0,
                int(ev.get("body_size") or (len(ev.get("body") or ""))),
                int(ev.get("response_size") or (len(ev.get("response_preview") or ""))),
                ev.get("body"),
                ev.get("response_preview"),
            ))

        try:
            conn.executemany("""
                INSERT OR REPLACE INTO request_logs (
                    id, created_at, time_str, method, path, host, account, model,
                    status, status_text, latency_ms, prompt_tokens, completion_tokens,
                    total_tokens, retried, body_size, response_size, body, response_preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, rows)
            conn.commit()
        except Exception:
            logger.exception("Failed to insert telemetry batch")

    def flush(self, timeout: float = 5.0) -> None:
        self._queue.join()

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=3.0)

    @contextlib.contextmanager
    def get_read_conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def query_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        account: str | None = None,
        model: str | None = None,
        status: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = []
        params: list[Any] = []

        if account:
            filters.append("account = ?")
            params.append(account)
        if model:
            filters.append("model = ?")
            params.append(model)
        if status is not None:
            filters.append("status = ?")
            params.append(status)

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        with self.get_read_conn() as conn:
            cursor = conn.cursor()
            count_query = f"SELECT COUNT(*) FROM request_logs {where_clause};"
            total = cursor.execute(count_query, params).fetchone()[0]

            data_query = f"""
                SELECT id, created_at, time_str, method, path, host, account, model,
                       status, status_text, latency_ms, prompt_tokens, completion_tokens,
                       total_tokens, retried, body_size, response_size, body, response_preview
                FROM request_logs
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?;
            """
            cursor.execute(data_query, params + [limit, offset])
            rows = cursor.fetchall()

        logs = []
        for r in rows:
            logs.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "time": r["time_str"],
                "method": r["method"],
                "path": r["path"],
                "host": r["host"],
                "account": r["account"],
                "model": r["model"],
                "status": r["status"],
                "status_text": r["status_text"],
                "latency_ms": r["latency_ms"],
                "tokens": {
                    "prompt_tokens": r["prompt_tokens"],
                    "completion_tokens": r["completion_tokens"],
                    "total_tokens": r["total_tokens"],
                },
                "retried": bool(r["retried"]),
                "body_size": r["body_size"],
                "response_size": r["response_size"],
                "body": r["body"],
                "response_preview": r["response_preview"],
            })
        return logs, total

    def query_aggregate_stats(self) -> dict[str, Any]:
        with self.get_read_conn() as conn:
            cursor = conn.cursor()
            row = cursor.execute("""
                SELECT 
                    COUNT(*),
                    COALESCE(SUM(total_tokens), 0),
                    COALESCE(SUM(prompt_tokens), 0),
                    COALESCE(SUM(completion_tokens), 0),
                    COALESCE(AVG(latency_ms), 0),
                    COALESCE(SUM(CASE WHEN retried = 1 OR status = 429 THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status < 400 THEN 1 ELSE 0 END), 0)
                FROM request_logs;
            """).fetchone()

        total_reqs = row[0]
        total_tok = row[1]
        prompt_tok = row[2]
        compl_tok = row[3]
        avg_lat = round(row[4], 1)
        failovers = row[5]
        success_reqs = row[6]
        success_rate = round((success_reqs / total_reqs * 100), 1) if total_reqs > 0 else 100.0

        return {
            "total_requests": total_reqs,
            "total_tokens": total_tok,
            "prompt_tokens": prompt_tok,
            "completion_tokens": compl_tok,
            "avg_latency_ms": int(avg_lat),
            "failover_count": failovers,
            "success_rate": success_rate,
        }

    def query_token_analytics(self, range_days: int = 7) -> dict[str, Any]:
        cutoff = time.time() - (range_days * 86400) if range_days > 0 else 0

        with self.get_read_conn() as conn:
            cursor = conn.cursor()

            timeline_rows = cursor.execute("""
                SELECT 
                    strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', 'localtime')) as day,
                    COUNT(*) as requests,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens
                FROM request_logs
                WHERE created_at >= ?
                GROUP BY day
                ORDER BY day ASC;
            """, (cutoff,)).fetchall()

            account_rows = cursor.execute("""
                SELECT 
                    account,
                    COUNT(*) as requests,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(CASE WHEN status = 429 OR retried = 1 THEN 1 ELSE 0 END), 0) as failovers
                FROM request_logs
                WHERE created_at >= ?
                GROUP BY account
                ORDER BY total_tokens DESC;
            """, (cutoff,)).fetchall()

            model_rows = cursor.execute("""
                SELECT 
                    model,
                    COUNT(*) as requests,
                    COALESCE(SUM(total_tokens), 0) as total_tokens
                FROM request_logs
                WHERE created_at >= ?
                GROUP BY model
                ORDER BY total_tokens DESC;
            """, (cutoff,)).fetchall()

        return {
            "range_days": range_days,
            "timeline": [
                {
                    "date": r["day"],
                    "requests": r["requests"],
                    "prompt_tokens": r["prompt_tokens"],
                    "completion_tokens": r["completion_tokens"],
                    "total_tokens": r["total_tokens"],
                }
                for r in timeline_rows
            ],
            "by_account": [
                {
                    "account": r["account"],
                    "requests": r["requests"],
                    "total_tokens": r["total_tokens"],
                    "failovers": r["failovers"],
                }
                for r in account_rows
            ],
            "by_model": [
                {
                    "model": r["model"],
                    "requests": r["requests"],
                    "total_tokens": r["total_tokens"],
                }
                for r in model_rows
            ],
        }

    def prune_retention(self, body_retention_days: int = 7, row_retention_days: int = 90) -> tuple[int, int]:
        now = time.time()
        body_cutoff = now - (body_retention_days * 86400)
        row_cutoff = now - (row_retention_days * 86400)

        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            cur1 = conn.execute("""
                UPDATE request_logs 
                SET body = NULL, response_preview = NULL 
                WHERE created_at < ? AND (body IS NOT NULL OR response_preview IS NOT NULL);
            """, (body_cutoff,))
            pruned_bodies = cur1.rowcount

            cur2 = conn.execute("DELETE FROM request_logs WHERE created_at < ?;", (row_cutoff,))
            pruned_rows = cur2.rowcount

            conn.execute("PRAGMA incremental_vacuum(500);")
            conn.commit()

        return pruned_bodies, pruned_rows


_GLOBAL_DB: TelemetryDB | None = None
_GLOBAL_DB_LOCK = threading.Lock()


def get_telemetry_db(db_path: Path | str | None = None) -> TelemetryDB:
    global _GLOBAL_DB
    with _GLOBAL_DB_LOCK:
        if _GLOBAL_DB is None:
            _GLOBAL_DB = TelemetryDB(db_path=db_path)
            _GLOBAL_DB.start()
        return _GLOBAL_DB
