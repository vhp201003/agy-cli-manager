---
title: UI Concentric Quota Rings & Layout B Card Redesign
status: in-progress
priority: P1
effort: medium
branch: main
tags: [ui, concentric-rings, vercel-style, quota-display]
created: 2026-10-13
---

# UI Concentric Quota Rings & Layout B Card Redesign

## Executive Summary
Redesign the account quota representation on the AGY Web Dashboard from text-heavy progress bars into **Concentric Circular Progress Rings (Layout B)** matching Vercel / Linear / Geist engineering-grade aesthetics. Each model provider block (Gemini and Claude/GPT) features a clean SVG concentric ring gauge on the left with structured 5H & Weekly countdown metrics on the right.

## Objectives
- Eliminate information clutter and cognitive overload in account cards.
- Implement concentric radial gauges (Outer: 5H burst quota, Inner: Weekly quota, Center: 5H percentage).
- Deliver structured, readable 5H and Weekly countdown lines (`5h: 61% · Resets in 2h 28m`).
- Preserve 100% data visibility without requiring tooltips for macro progress.
- Maintain strict Vercel dark mode design tokens, smooth 60fps SVG stroke transitions, and responsive grid layout.

## Phase Overview
- [x] Phase 1: SVG Radial Gauge Generator & CSS System (`phase-01-svg-radial-system.md`)
- [x] Phase 2: Layout B Account Card Implementation (`phase-02-card-layout-b-implementation.md`)
- [x] Phase 3: Cross-browser Verification & Regression Suite (`phase-03-verification-and-tests.md`)
