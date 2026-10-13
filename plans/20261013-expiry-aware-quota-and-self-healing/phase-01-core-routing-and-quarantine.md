# Phase 1: Core TokenManager Expiry-Aware Routing & Silent Quarantine

## Objectives
Enhance `TokenManager` in `src/agy_cli_manager/proxy/token_manager.py` to:
1. Support account quarantine (`_quarantines: dict[str, dict]`) with duration (default 1800s / 30 mins) and reason tracking.
2. In `proxy_server.py`: When upstream returns HTTP 401 and token refresh fails, automatically trigger `token_manager.quarantine_account(name, reason="auth_failure", duration_seconds=1800)`.
3. Implement Expiry-Aware Routing in `get_token_by_session`:
   - Filter out quarantined accounts.
   - Filter out accounts in 429 cooldown.
   - For remaining available accounts, parse `5h.reset_time` (RFC3339 or float timestamp).
   - Rank accounts by priority:
     - Active 5H window nearing expiration (`0 < ttl < 5h`) sorted by `ttl ASC` (nearest expiry first).
     - Inactive 5H window / 100% fresh standby sorted last (preserved as reserve).
4. Provide helper methods:
   - `quarantine_account(name, reason, duration_seconds=1800)`
   - `clear_quarantine(name)`
   - `is_quarantined(name) -> bool`
   - `get_quarantines() -> dict[str, dict]`

## Checklist
- [x] Implement `_quarantines` dictionary and thread-safe lock management in `TokenManager`.
- [x] Add `quarantine_account`, `clear_quarantine`, `is_quarantined`, `get_quarantines` to `TokenManager`.
- [x] Parse `reset_time` safely (handling ISO RFC3339 strings, epoch numbers, and timezone offsets) into epoch seconds.
- [x] Implement `_calculate_account_priority(name, target_family, now)` in `TokenManager`.
- [x] Update `get_token_by_session` to sort `alive_pool` by expiry priority.
- [x] Update `proxy_server.py` to quarantine accounts when 401 refresh fails.
- [x] Ensure non-quarantined fallback if all accounts are quarantined.
