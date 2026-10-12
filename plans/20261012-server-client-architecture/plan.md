---
title: Dual Server-Client Architecture for AGY Gateway
status: completed
priority: P1
effort: medium
branch: main
tags: [proxy, cli, client, server, gateway]
created: 2026-10-12
completed: 2026-10-12
---

# Plan: Dual Server-Client Architecture for AGY Gateway

## Overview
Transform `agy-cli-manager` into a first-class Server & Client dual-mode system:
- **Server Mode:** Run 24/7 on VPS with account rotation, quota pooling, web telemetry, and CA distribution.
- **Client Mode:** Seamless 1-command onboarding (`agy-client connect <gateway-url>`), automatic CA trust, persistent client configuration, and transparent execution via `agy-run`.

## Phases
- [x] Phase 1: Client/Server Configuration & CA Distribution Endpoint
- [x] Phase 2: CLI Subcommands (`server` & `client`) & Shortcuts (`agy-server`, `agy-client`)
- [x] Phase 3: Web Dashboard "Connect Client" Modal & Telemetry Enhancements
- [x] Phase 4: Verification, Security Review & Tests
- [x] Phase 5: VPS Git Pull, Service Rebuild & Local E2E Verification
