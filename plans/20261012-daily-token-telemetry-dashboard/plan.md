---
title: Daily Token Breakdown & Analytics Dashboard
status: completed
priority: P1
effort: low
branch: main
tags: [telemetry, dashboard, ui, tokens, daily-stats]
created: 2026-10-12
completed: 2026-10-12
---

# Plan: Daily Token Breakdown & Analytics Dashboard

## Overview
Enhance the Web Dashboard with a dedicated, zero-dependency "Daily Token Consumption" section featuring:
- Time range switcher: 7 Days / 14 Days / 30 Days.
- Pure SVG / CSS stacked bar chart comparing Daily Prompt Tokens (Input) and Completion Tokens (Output).
- Interactive hover tooltips displaying date, request count, input/output breakdown, and percentages.
- Detailed summary table listing each date, request volume, input/output split, total tokens, and average tokens per request.
- Collapsible toggle to conserve screen real estate.
- Seamless auto-refresh alongside existing dashboard polling cycles.

## Phases
- [x] Phase 1: Verify & optimize `/api/analytics/tokens` endpoint in backend (`db.py` & `fastapi_app.py`).
- [x] Phase 2: Implement Daily Token Consumption UI component (HTML, CSS, SVG Bar Chart, Table) in `fastapi_app.py`.
- [x] Phase 3: Implement client-side JavaScript for fetching analytics, rendering bars with responsive heights, and range filtering.
- [x] Phase 4: Local testing & verification with live local test server.
- [x] Phase 5: Security review remediation (XSS sanitization, DoS bounds, midnight alignment, debouncing).
- [x] Phase 6: Handoff to user and ship to VPS.
