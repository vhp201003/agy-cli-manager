# AGY CLI Manager — System Architecture & Client-Server Specification

## 1. System Overview

`agy-cli-manager` is a high-performance account orchestrator, token-rotating reverse proxy, and telemetry platform for **Google Antigravity CLI** (`agy`) and AI coding agents.

The system is decoupled into two primary tiers:
1. **Server Tier (`agy-server` / `agy-cli-manager proxy`):** Central daemon deployed on VPS or local machines. Manages account pools, OAuth tokens, upstream routing, SSL interception, rate-limit failovers, SQLite WAL telemetry, and the Vercel-styled Web Dashboard.
2. **Client Tier (`agy-client` & `agy-run`):** Lightweight developer workstation CLI. Connects developers to remote or local gateways, manages Authenticode and Root CA certificate stores, and transparently routes developer CLI tools through the proxy.

```
+-----------------------------------------------------------------------------------------+
|                                    DEVELOPER WORKSTATION                                |
|                                                                                         |
|  +---------------------------+               +---------------------------------------+  |
|  |        agy-client         |               |                agy-run                |  |
|  | - connect <gateway_url>   |               | - Injects HTTP_PROXY / HTTPS_PROXY    |  |
|  | - disconnect / status     |               | - Injects SSL_CERT_FILE (bundle.crt)  |  |
|  | - Installs CA into OS     |               | - Spawns antigravity-cli / gemini-cli |  |
|  +-------------+-------------+               +-------------------+-------------------+  |
+----------------|-------------------------------------------------|----------------------+
                 |                                                 |
                 | HTTP /ca.crt & /api/status                      | HTTPS (SSL MITM)
                 v                                                 v
+-----------------------------------------------------------------------------------------+
|                                GATEWAY SERVER (VPS or Local)                            |
|                                                                                         |
|  +-------------------------------------+   +-----------------------------------------+  |
|  |    PORT 8800: FastAPI Dashboard     |   |       PORT 8899: mitmproxy Router       |  |
|  | - Web UI (Vercel Geist Prism Theme) |   | - Intercepts Gemini / CodeAssist APIs   |  |
|  | - Daily Token Analytics Chart       |   | - Round-Robin Multi-Account Rotation    |  |
|  | - Real-time SSE Telemetry (/events) |   | - Auto-Retry on 429/403 with Cooldown   |  |
|  | - Root CA Distribution (/ca.crt)    |   | - Dynamic Token & Header Rewriting      |  |
|  | - 5H Quota Window Warmup            |   | - Generates per-host leaf certificates  |  |
|  +------------------+------------------+   +--------------------+--------------------+  |
|                     |                                           |                       |
|                     +---------------------+---------------------+                       |
|                                           |                                             |
|                                           v                                             |
|                     +-------------------------------------------+                       |
|                     |             SHARED BACKEND CORE           |                       |
|                     | - TokenManager: Multi-account OAuth pool  |                       |
|                     | - Proactive Near-Expiry OAuth Refresh     |                       |
|                     | - TelemetryDB: SQLite WAL Event Store     |                       |
|                     | - CertManager: Root CA (ca.crt / ca.key)  |                       |
|                     +---------------------+---------------------+                       |
+-------------------------------------------|---------------------------------------------+
                                            | Upstream HTTPS with Bearer OAuth Token
                                            v
                         +-------------------------------------+
                         |      GOOGLE ANTIGRAVITY & CLOUD     |
                         |   - daily-cloudcode-pa.googleapis   |
                         |   - cloudcode-pa.googleapis.com     |
                         |   - generativelanguage.googleapis   |
                         +-------------------------------------+
```

---

## 2. Server Architecture (`agy-server` / `agy-cli-manager proxy`)

The Server daemon runs two concurrent network services:

### 2.1 Forward Proxy Gateway (Port `8899`)
- **Technology:** `mitmproxy` core embedded addon (`AgyProxyAddon`).
- **SSL Interception:** Generates a custom internal Root CA (`ca.crt` / `ca.key`) stored in `~/.agy-cli-manager/ca/`. On client connections, dynamically mints SNI-valid leaf certificates.
- **Traffic Routing & Filtering:**
  - Intercepts requests targeting Google Antigravity and CodeAssist domains (`cloudcode-pa.googleapis.com`, `daily-cloudcode-pa.googleapis.com`, `generativelanguage.googleapis.com`).
  - Passes non-target traffic through transparently or tunnels via upstream CONNECT.
- **Multi-Account Rotation:**
  - Accounts loaded from `~/.agy-cli-manager/accounts/<name>/`.
  - State tracked via `TokenManager`: accounts cycled round-robin across requests.
  - Session stickiness: requests with identical `x-session-id` or client interaction headers pin to the same account unless that account hits a rate limit.
- **Resilience & Failover:**
  - Intercepts HTTP `429 Too Many Requests` or `RESOURCE_EXHAUSTED`.
  - Automatically flags the offending account into a cooldown period (default 5–15 minutes).
  - Switches to the next available account and transparently replays the request without surfacing the error to the developer.
- **Background Cron:**
  - A background thread wakes every 600 seconds (10 minutes) to check token validity and refresh quotas from upstream Google APIs.
  - Proactively refreshes OAuth access tokens when remaining lifetime is under 5 minutes using Antigravity default OAuth client credentials.

### 2.2 FastAPI Web Dashboard & Telemetry API (Port `8800`)
- **Technology:** `FastAPI` + `Uvicorn` asynchronous server.
- **Endpoints:**
  - `GET /`: Single-page responsive Web Dashboard styled according to the **Vercel Geist Design System**.
  - `GET /favicon.svg` & `GET /favicon.ico`: Serves the Bi-Facet Delta optical SVG favicon.
  - `GET /ca.crt`: Downloads the public Root CA certificate for client setup.
  - `GET /api/status`: Real-time system snapshot (pool size, active accounts, failover count, token burn, latency).
  - `GET /api/events`: Server-Sent Events (SSE) stream pushing live request logs to connected dashboard clients.
  - `GET /api/logs`: Paginated and filterable request history from SQLite.
  - `GET /api/analytics/tokens`: Token consumption breakdown grouped by date, account, and model.
  - `POST /api/accounts/warmup`: 1-Click activation of the 5-Hour quota window across all accounts.
  - `POST /api/refresh-all`: Proactive OAuth token refresh for all accounts.
  - `POST /api/sync-quotas`: Real-time quota balance sync with Google.

### 2.3 Telemetry Database (`src/agy_cli_manager/proxy/db.py`)
- **Engine:** SQLite 3.
- **Concurrency Mode:** `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;`. WAL (Write-Ahead Logging) permits non-blocking concurrent reads while the proxy writes logs.
- **Asynchronous Batching:** Ingestion runs through a thread-safe `Queue`. A background worker flushes batches (up to 100 entries or 0.5s intervals) to prevent disk I/O bottlenecks during high-throughput token streaming.
- **Retention:** Auto-prunes request bodies exceeding 50 KB and logs older than 30 days.

---

## 3. Client Architecture (`agy-client` & `agy-run`)

The client tier allows developers on Windows, macOS, or Linux to utilize a remote VPS or local proxy without manual environment configuration.

### 3.1 Gateway Connection Management (`agy-client`)
- **Configuration File:** `~/.agy-cli-manager/client.json`.
- **Commands:**
  - `agy-client connect <gateway_url>`:
    1. Probes `<gateway_url>/api/status` to ensure connectivity.
    2. Downloads public CA certificate from `<gateway_url>/ca.crt`.
    3. Builds unified certificate bundle `~/.agy-cli-manager/bundle.crt` (system root certificates + proxy CA).
    4. On Windows: Automatically imports the Root CA into the `CurrentUser\Root` certificate store via PowerShell `certutil` / .NET `X509Store`.
    5. Saves active gateway address to `client.json`.
  - `agy-client disconnect`:
    1. Removes active proxy endpoint configuration.
    2. Resets `client.json` back to standalone local defaults.
  - `agy-client status`:
    1. Displays active gateway, latency, account pool health, and certificate trust status.

### 3.2 Transparent Process Execution (`agy-run`)
- **Wrapper Logic:**
  1. Resolves target gateway endpoint from `client.json` or fallback `http://127.0.0.1:8899`.
  2. Resolves CA bundle path: prioritizes `~/.agy-cli-manager/bundle.crt`.
  3. Prepares environment variables for the child process:
     - `HTTP_PROXY=<proxy_url>`
     - `HTTPS_PROXY=<proxy_url>`
     - `http_proxy=<proxy_url>`
     - `https_proxy=<proxy_url>`
     - `SSL_CERT_FILE=<bundle_path>`
     - `REQUESTS_CA_BUNDLE=<bundle_path>`
     - `NODE_EXTRA_CA_CERTS=<bundle_path>`
  4. Removes conflicting Windows Go TLS flags (e.g. `x509usefallbackroots=1`).
  5. Launches the target binary (`antigravity-cli`, `agy`, or custom command) with stream redirection.

---

## 4. Visual Identity & Vercel Geist Design System

The application identity embodies **Vercel / Geist** minimalism:

### 4.1 Concept: Zero-G Prism (Vercel Edition)
- **Symbolism:** Represents reverse proxy multiplexing. A single developer prompt enters as a collimated white beam and refracts through an antigravity glass prism into balanced, multi-account token streams.
- **Achromatic Dominance (95%):** The prism body is constructed with monochromatic chiaroscuro glass facets (`rgba(255,255,255,0.28)` to `0.02`) on pitch black (`#000000`).
- **Precision Hairlines:** 1px stroke boundaries snapped to integer pixel coordinates (`shape-rendering="geometricPrecision"`). Zero Gaussian blur filters or glowing halos.
- **Spectral Dispersion (5%):**
  - Ray 1 (High-Energy Cyan): `#00DFD8`
  - Ray 2 (Vercel Signature Blue): `#0070F3`
  - Ray 3 (Hyper-Purple): `#7928CA`
- **Adaptive Level-of-Detail (LOD):**
  - **16×16 Favicon:** Asymmetric Bi-Facet Delta (left half white `#EDEDED`, right half carbon `#171717` with 1px border) with a clipped optical laser notch (`#0070F3`).
  - **32×32 Header Logo:** Full 3-facet volumetric prism with 1px laser paths embedded in the dashboard navbar.

---

## 5. Daily Token Analytics Engine

- **Endpoint:** `GET /api/analytics/tokens?range_days=7|14|30`
- **Aggregation:**
  - SQLite query groups token usage by calendar date (`%Y-%m-%d`) using server local time.
  - Automatically backfills missing dates with 0-token empty records to ensure continuous calendar timelines.
  - Calculates prompt tokens, completion tokens, total tokens, requests, and per-request averages.
- **Frontend Visualization:**
  - Pure CSS/SVG stacked bar chart with dynamic height scaling based on `maxTokens = Math.max(...timeline.map(d => d.total_tokens || 0), 1)`.
  - Non-zero bars maintain a minimum height of 8% for visibility.
  - Sub-percent completion tokens are explicitly labeled as `<1%` to prevent confusing `0%` roundoff artifacts.
  - Detailed hover tooltips and interactive reverse-chronological data table.
