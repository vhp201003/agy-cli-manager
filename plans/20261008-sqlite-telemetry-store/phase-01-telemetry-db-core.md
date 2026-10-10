# Phase 1: Telemetry Database Core Module

## Tasks
- [x] Implement `src/agy_cli_manager/proxy/db.py`:
  - [x] Initialize SQLite DB at `Path.home() / ".agy-cli-manager" / "telemetry.db"` (configurable path for tests).
  - [x] Configure `PRAGMA journal_mode = WAL;`, `PRAGMA synchronous = NORMAL;`, `PRAGMA busy_timeout = 5000;`, `PRAGMA auto_vacuum = INCREMENTAL;`.
  - [x] Create table `request_logs` with composite indexes (`idx_requests_created`, `idx_requests_token_agg`, `idx_requests_status`).
  - [x] Implement single background writer thread (`LogWriterThread`) reading from `queue.Queue(maxsize=5000)` with micro-batching.
  - [x] Clamp request/response body text at ingestion (max 16 KB preview).
  - [x] Implement query functions: `query_logs(limit, offset, filters)`, `query_aggregate_stats()`, `query_token_analytics(range_str)`.
  - [x] Implement two-tier pruning: clear bodies older than 7 days, delete rows older than 90 days.
- [x] Create unit tests in `tests/unit/test_telemetry_db.py`:
  - [x] Test schema initialization and WAL mode.
  - [x] Test asynchronous enqueue and micro-batch commit.
  - [x] Test token count extraction and aggregate stats calculation.
  - [x] Test pagination and filtering.
  - [x] Test body clamping and retention cleanup.
