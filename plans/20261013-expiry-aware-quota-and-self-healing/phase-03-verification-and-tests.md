# Phase 3: Comprehensive Unit Tests & Verification

## Objectives
1. Create dedicated unit tests in `tests/unit/test_expiry_routing.py` testing:
   - Account quarantine logic (quarantine, auto-release after duration, manual release, reason logging).
   - Expiry-Aware Routing logic (account expiring in 20 min picked before account expiring in 3 hours, and before fresh account with no active window).
   - Failover and fallback when all accounts are quarantined or cooling down.
   - Status API responses including quarantine and 5H reset fields.
2. Run full test suite with `pytest tests/unit/`.
3. Verify all tests pass with zero regressions.

## Checklist
- [x] Create `tests/unit/test_expiry_routing.py` covering quarantine lifecycle and expiry ranking.
- [x] Add TestClient tests in `tests/unit/test_analytics_endpoint.py` or `test_expiry_routing.py` for `/api/accounts/{name}/quarantine` and `clear-quarantine`.
- [x] Run `python -m pytest tests/unit/` and verify all tests pass.
- [x] Reconcile checklist and update plan status.
