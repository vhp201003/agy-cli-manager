# Phase 2: FastAPI Endpoints & Dashboard Status Enrichment

## Objectives
1. Expose Quarantine management endpoints in `fastapi_app.py`:
   - `POST /api/accounts/{name}/quarantine`: manually quarantine an account with optional duration and reason.
   - `POST /api/accounts/{name}/clear-quarantine`: release an account from quarantine.
2. Enrich `TokenManager.get_status()` with:
   - `in_quarantine: bool`
   - `quarantine_remaining_sec: int`
   - `quarantine_reason: str | None`
   - `time_until_5h_reset_sec: int | None`
   - `time_until_5h_reset_formatted: str | None`
   - `priority_tier: int` (1: Expiring soonest, 2: Active window, 3: Standby fresh)
3. Dashboard / Web UI integration in `fastapi_app.py`:
   - Account status badge reflects Quarantine state (red warning badge with countdown).
   - "Release Quarantine" button appears when an account is in quarantine.
   - Account row displays 5H TTL countdown and expiry badge.

## Checklist
- [x] Add `POST /api/accounts/{name}/quarantine` endpoint in `fastapi_app.py`.
- [x] Add `POST /api/accounts/{name}/clear-quarantine` endpoint in `fastapi_app.py`.
- [x] Enrich `TokenManager.get_status()` with quarantine and 5H reset fields.
- [x] Update Web UI in `fastapi_app.py` to render quarantine badge, 5H TTL indicator, and clear-quarantine action.
