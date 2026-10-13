# Phase 1: SVG Radial Gauge Generator & CSS System

## Goals
Develop a high-performance, dependency-free SVG concentric ring renderer and CSS utility classes in `src/agy_cli_manager/proxy/fastapi_app.py`.

## Tasks
- [x] Create pure JavaScript helper `renderConcentricRingSvg(p5h, pWk, p5hColor, pWkColor, labelCenter)`:
  - Outer circle: Radius 21px, stroke width 3.5px (5H quota).
  - Inner circle: Radius 14px, stroke width 3.0px (Weekly quota).
  - Background track circles: subtle `rgba(255, 255, 255, 0.07)`.
  - Center label: `labelCenter` (e.g. `61%`) formatted in bold Geist Mono.
  - Dimension: 52×52px SVG with `viewBox="0 0 52 52"`.
- [x] Add CSS classes for concentric gauge container, rows, and typography:
  - `.radial-block`: Container for model family row.
  - `.radial-gauge-wrap`: 52×52px container with hover scale transition.
  - `.radial-meta-col`: Vertical stack for 5H and Weekly textual countdowns.
  - Semantic status color tokens: Cyan `#38bdf8`, Purple `#a855f7`, Emerald `#10b981`, Amber `#f59e0b`, Red `#ef4444`.
- [x] Verify SVG stroke-dashoffset math: `circumference = 2 * Math.PI * radius` with rotation `-90deg` so progress starts at 12 o'clock.
