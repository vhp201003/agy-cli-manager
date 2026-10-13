---
title: Expiry-Aware Quota Draining & Pool Self-Healing
status: completed
priority: P0
effort: medium
branch: main
tags: [proxy, quota, token-manager, self-healing, resilience]
created: 2026-10-13
---

# Expiry-Aware Quota Draining & Pool Self-Healing

## Executive Summary
This initiative implements the core strategic moat for `agy-cli-manager`: **Perishable-Quota Arbitrage & Pool Self-Healing**.
Google Antigravity accounts operate on rolling 5-hour quota windows. Tokens unspent prior to reset evaporate permanently. This feature prioritizes draining accounts nearing 5H window reset (`TTL ASC`) before touching fresh reserve accounts, while isolating failing/quarantined accounts (HTTP 401 / CAPTCHAs) for 30 minutes to protect autonomous coding agents from pool poisoning.

## Architecture & Core Mechanics

```
               [ Coding Agent Request ]
                          │
                          ▼
             [ TokenManager Account Selector ]
                          │
         ┌────────────────┴────────────────┐
         ▼                                 ▼
[ Filter Quarantined ]             [ Filter Cooldown ]
(Auth 401 / CAPTCHA)               (HTTP 429 Active)
         │                                 │
         └────────────────┬────────────────┘
                          ▼
            [ Expiry-Aware Priority Ladder ]
   - Tier 1: Active 5H window with lowest TTL (drain first!)
   - Tier 2: Active 5H window with longer TTL
   - Tier 3: Fresh inactive window (zero-loss standby reserve)
                          │
                          ▼
                 [ Upstream Dispatch ]
```

## Phases
- [x] Phase 1: Core TokenManager Expiry-Aware Routing & Silent Quarantine (`phase-01-core-routing-and-quarantine.md`)
- [x] Phase 2: FastAPI Endpoints & Dashboard Status Enrichment (`phase-02-api-and-ui.md`)
- [x] Phase 3: Comprehensive Unit Tests & Verification (`phase-03-verification-and-tests.md`)

## Acceptance Criteria
- [x] Accounts nearing 5H reset (`reset_time` in future) are selected first in `get_token_by_session`.
- [x] Fresh standby accounts without active 5H clock are reserved until expiring accounts are drained.
- [x] Accounts encountering 401 (auth failure / token revocation) are quarantined for 1800s (`quarantined_until`), preventing pool poisoning.
- [x] `/api/accounts/{name}/clear-quarantine` allows immediate un-quarantine.
- [x] Dashboard / `get_status()` exposes `in_quarantine`, `quarantine_remaining_sec`, `time_until_5h_reset_sec`, and `priority_tier`.
- [x] All unit tests pass with zero regressions.
