---
title: SQLite Telemetry and Token Persistence
status: completed
priority: P1
effort: medium
branch: main
tags: [sqlite, telemetry, tokens, proxy]
created: 2026-10-08
---

# SQLite Telemetry and Token Persistence

## Overview
Durable persistence of request logs and token consumption into a local SQLite database (`~/.agy-cli-manager/telemetry.db`). Replaces ephemeral in-memory `deque(maxlen=300)` with a zero-latency, single-writer queue architecture in WAL mode.

## Phases
- [x] [Phase 1: Telemetry Database Core Module](phase-01-telemetry-db-core.md)
- [x] [Phase 2: Proxy Server & FastAPI Lifecycle Integration](phase-02-proxy-fastapi-integration.md)
- [x] [Phase 3: Analytics API Endpoints & Dashboard Sync](phase-03-api-endpoints-and-dashboard.md)
- [x] [Phase 4: Concurrency Testing & Full Test Suite Pass](phase-04-concurrency-and-test-pass.md)

## Acceptance Criteria
- [x] SQLite database initialized at `manager_root / "telemetry.db"` with WAL mode and indexing.
- [x] Zero latency overhead: worker threads enqueue logs in `< 10 µs` via bounded queue.
- [x] Token usage and logs persist across proxy and server restarts.
- [x] `GET /api/status` returns aggregate token totals from SQLite.
- [x] `GET /api/logs` supports pagination and filtering.
- [x] `GET /api/analytics/tokens` provides daily breakdown, per-account, and per-model metrics.
- [x] 0 errors under concurrent writes; 100% test pass on `pytest`.
