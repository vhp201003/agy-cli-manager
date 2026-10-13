# Phase 2: Layout B Account Card Implementation

## Goals
Rebuild `renderAccountsGrid(items)` in `fastapi_app.py` to employ Layout B (Stacked Radial Rows) for each account card.

## Tasks
- [x] Restructure card markup in `renderAccountsGrid()`:
  - Header: Profile chip (`acc1`), display name, active runner dot, status badge (`ACTIVE`/`COOLDOWN`/`QUARANTINE`), and `DRAIN` TTL chip.
  - Body: Two stacked radial rows (Gemini block on top, Claude/GPT block below).
  - Each block: Concentric SVG gauge on the left + structured 2-line countdown metrics on the right.
  - Line 1: `5h: {pct}% · {rel_reset}` (e.g., `5h: 61% · Resets in 2h 28m` or `5h: 100% · Ready`).
  - Line 2: `Wk: {pct}% · {rel_reset}` (e.g., `Wk: 52% · Resets in 1d 22h` or `Wk: ≤5% · Resets in 1d 21h (Low)`).
  - Footer: Token expiry (`Exp: 33m`) and streamlined action buttons.
- [x] Provide exact ISO wall-clock tooltip on hover for high-precision inspection.
- [x] Ensure seamless responsive behavior across 5-column, 4-column, 3-column, and mobile viewports.
