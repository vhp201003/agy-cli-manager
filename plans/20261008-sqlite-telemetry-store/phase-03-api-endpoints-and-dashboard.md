# Phase 3: Analytics API Endpoints & Dashboard Sync

## Tasks
- [x] Refactor `GET /api/status` in `fastapi_app.py`:
  - [x] Use `asyncio.to_thread(query_aggregate_stats)` to retrieve total tokens, request count, success rate, and latency.
- [x] Update `GET /api/logs` in `fastapi_app.py`:
  - [x] Accept query parameters: `limit` (default 100), `offset` (default 0), `account`, `model`, `status`.
  - [x] Query SQLite via `asyncio.to_thread(query_logs)`.
- [x] Add `GET /api/analytics/tokens` in `fastapi_app.py`:
  - [x] Return daily usage timeline, per-account breakdown, and per-model breakdown.
- [x] Verify Dashboard HTML displays persistent tokens and loads historical logs.
