# Phase 2: Proxy Server & FastAPI Lifecycle Integration

## Tasks
- [x] Connect `broadcast_event` in `src/agy_cli_manager/proxy/proxy_server.py` to `db.log_event()`.
- [x] Ensure non-blocking enqueue: `log_event()` never raises or stalls calling thread.
- [x] Update `src/agy_cli_manager/proxy/fastapi_app.py` lifespan:
  - [x] Start `LogWriterThread` on app startup.
  - [x] Stop and drain writer thread on app shutdown.
- [x] Keep existing SSE `/api/events` and in-memory listeners intact for live client updates.
