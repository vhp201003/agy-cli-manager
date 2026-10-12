# Phase 3: Comprehensive System Documentation (ak:docs)

## Checklist
- [x] Map real codebase logic into architecture documentation:
  - Client-Server separation (`agy-server`, `agy-client`, `agy-run`).
  - Gateway proxy (`:8899`), dashboard (`:8800`), OAuth refresh, 5H quota warmup.
  - TLS certificate handling (root CA bundle generation, local trust store injection).
  - Telemetry SQLite WAL storage & Daily Token Consumption Analytics.
- [x] Update `README.md` with complete usage guides and brand identity.
- [x] Create detailed architecture documentation in `docs/architecture.md`.
