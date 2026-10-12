# agy-cli-manager

> **Zero-Downtime Multi-Account Gateway & OpenAI Proxy for Antigravity CLI and AI Coding Agents.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Vercel Geist Design](https://img.shields.io/badge/Design-Vercel%20Geist-000000.svg)](docs/architecture.md)

Stop getting blocked by `429: Individual quota reached`. `agy-cli-manager` pools multiple Google Antigravity CLI accounts into a resilient, high-concurrency token gateway. Run interactive terminal sessions without quota interruptions, or connect Cursor, Claude Code, Cline, and OpenCode directly via an OpenAI-compatible API.

---

## The Problem vs. The Solution

- **The Problem:** The original Antigravity CLI workflow was strictly single-account and disk-bound. When an account hit 5-hour quota limits (`429 RESOURCE_EXHAUSTED`), work stopped. Developers had to manually swap disk profiles, watch logs, kill terminal sessions, lose in-flight context, and restart. Other developer tools (Cursor, Claude Code, Cline) had no way to use these accounts.
- **The Solution:** `agy-cli-manager` acts as an intelligent, transparent SSL reverse proxy (`:8899`) and telemetry gateway (`:8800`). It pools all your accounts concurrently. When one account exhausts its quota or encounters rate limits, requests fail over instantly in-flight (<50ms) to the next healthy account with zero terminal restarts.

---

## Feature Comparison Matrix

| Capability | Legacy Filesystem Switcher | Enhanced Live Proxy Gateway (Recommended) |
| :--- | :--- | :--- |
| **Operational Architecture** | Disk directory swapping (`~/.gemini`) | Reverse proxy with transparent SSL MITM (`:8899`) |
| **Account Concurrency** | **1 active** (all other standby accounts sit idle) | **N concurrent accounts** in active round-robin equilibrium |
| **429 Quota Exhaustion** | 🔴 Terminal halted; manual restart required | 🟢 **Zero downtime**: Instant in-flight request replay (<50ms) |
| **Terminal / CLI State** | Lost session history on terminal kill | Preserved indefinitely; terminal never interrupts |
| **Multi-Agent / Concurrency** | Impossible (locked to single disk profile) | Full multi-tab, multi-process, and team concurrency |
| **AI Coding Agents** | Siloed strictly to `agy` CLI binary | Universal OpenAI API (`:8800/v1`) for Cursor, Cline, OpenCode, Aider |
| **Deployment Model** | Local developer laptop only | Local workstation OR remote team VPS (`agy-server` / `agy-client`) |
| **Certificate Trust** | N/A | Automated Root CA injection into OS Certificate Store |
| **Observability & Analytics** | Basic curses terminal TUI / raw logs | Real-time Web Dashboard (`:8800`), SSE stream, daily token bar charts |
| **Token Lifecycle** | Expired tokens fail mid-command | Proactive background token refresh (<5m remaining) & quota warmup |

---

## 60-Second Quickstart

### 1. Installation

Install `agy-cli-manager` with the proxy extension:

```bash
# Recommended: Install with proxy and dashboard support
pip install "agy-cli-manager[proxy]"
```

For editable local development:
```bash
git clone https://github.com/zcop/agy-cli-manager.git
cd agy-cli-manager
pip install -e ".[proxy]"
```

### 2. Onboard Your Accounts

Import an existing `~/.gemini` session or log in interactively:

```bash
# Import an existing active login
agy-cli-manager import-current work-account ~/.gemini

# Or start a fresh interactive login session
agy-cli-manager login personal-account
```

Inspect your account pool:
```bash
agy-cli-manager list
```

### 3. Launch the Gateway Daemon (`agy-server`)

```bash
agy-server
```

The server spins up two dedicated surfaces:
- **Port `8899`** (`Forward Proxy`): Intercepts Antigravity CLI traffic, rotates account tokens, and transparently handles 429 failovers.
- **Port `8800`** (`Web Dashboard & OpenAI API`): Real-time token telemetry, daily consumption charts, and `/v1/chat/completions`.

---

## Client-Server Architecture

`agy-cli-manager` supports a decoupled architecture: run `agy-server` 24/7 on a remote VPS or background daemon, and connect your developer machines in one command.

```text
┌─────────────────────────────────┐           ┌──────────────────────────────────┐
│   Developer Laptop (Windows/Mac)│           │   Server Daemon / Team VPS       │
│                                 │           │                                  │
│   agy-run ──> Antigravity CLI   │           │   agy-server                     │
│        │                        │           │     ├── Proxy (:8899) ──> Google │
│   Cursor / Cline ───────────────┼── HTTP ───┼──>  ├── Web UI (:8800)           │
│        │                        │           │     └── /v1/chat/completions     │
│   agy-client connect <url>      │           │                                  │
└─────────────────────────────────┘           └──────────────────────────────────┘
```

### On Developer Machines:

```bash
# 1. Connect to gateway (remote VPS or local server)
agy-client connect http://192.168.40.24:8800

# 2. Inspect active gateway status and pool health
agy-client status

# 3. Launch Antigravity CLI with zero-config proxy injection
agy-run
```

`agy-client connect` automatically:
- Verifies server health at `<gateway_url>/api/status`.
- Downloads the server's Root CA from `<gateway_url>/ca.crt`.
- Installs the certificate into your OS Certificate Store (`CurrentUser\Root` on Windows).
- Compiles the unified CA bundle `~/.agy-cli-manager/bundle.crt`.
- `agy-run` dynamically injects `HTTP_PROXY`, `HTTPS_PROXY`, and `SSL_CERT_FILE`.

To disconnect and restore local defaults:
```bash
agy-client disconnect
```

---

## Connect AI Coding Agents (Cursor, Cline, OpenCode)

`agy-server` exposes a standard OpenAI-compatible `/v1/chat/completions` endpoint that routes requests across your account pool.

### Quick Test with cURL
```bash
curl http://127.0.0.1:8800/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer none" \
  -H "x-session-id: agent-session-1" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [{"role": "user", "content": "Explain quantum entanglement in 2 sentences."}],
    "stream": false
  }'
```

### Cursor & Cline Configuration
Set your environment or agent custom model provider:
- **Base URL:** `http://127.0.0.1:8800/v1` (or your VPS IP `http://<vps-ip>:8800/v1`)
- **API Key:** `none` (any string)
- **Models:** `gemini-2.5-flash`, `gemini-2.5-pro`, `claude-3-7-sonnet` (depends on account entitlement)

---

## Real-Time Telemetry & Token Analytics

Visit `http://localhost:8800` (or `http://<vps-ip>:8800`) to access the Vercel Geist-styled dashboard:

- **Zero-G Prism Brand Identity:** High-precision monochromatic aesthetic with 1px hairlines and spectral refraction accents.
- **Daily Token Consumption:** Stacked bar chart visualizing Prompt vs. Completion tokens across 7, 14, and 30-day windows.
- **Account Health & Quota Telemetry:** Real-time visibility into 5-hour and weekly quota pools, cooldown timers, and active status.
- **One-Click Quota Warmup:** Trigger proactive background token refresh and quota checks across all accounts.
- **Live SSE Event Stream:** Real-time request logging, model routing, and failover notifications.

---

## CLI Command Cheat Sheet

### Gateway & Client Binaries
| Command | Role | Description |
| :--- | :--- | :--- |
| `agy-server` | Server Daemon | Start forward proxy (`:8899`) and Web Dashboard / OpenAI gateway (`:8800`) |
| `agy-client connect <url>` | Workstation Client | Connect to gateway, download Root CA, and configure local proxy bundle |
| `agy-client status` | Workstation Client | Inspect connection state, upstream server latency, and pool health |
| `agy-client disconnect` | Workstation Client | Remove gateway connection and restore direct network settings |
| `agy-run [args...]` | CLI Runner | Launch Antigravity CLI with automatic proxy environment variables |

### Account & Pool Administration
| Command | Description |
| :--- | :--- |
| `agy-cli-manager list` | List all accounts, health states, and quota levels |
| `agy-cli-manager import-current <name> <path>` | Import an existing `.gemini` home directory |
| `agy-cli-manager login <name>` | Launch interactive terminal onboarding for a new account |
| `agy-cli-manager refresh-usage [name]` | Probe and update live 5H and weekly quota metadata |
| `agy-cli-manager whoami` | Show detected Google identity and active account details |

---

## Architecture & System Design

For comprehensive technical specifications, including MITM certificate chaining, token rotation algorithms, database schema, and thread-safety models, see [docs/architecture.md](docs/architecture.md).

---

<details>
<summary><strong>Offline / Air-Gapped Fallback: Filesystem Profile Switcher</strong></summary>

For environments where MITM proxies or custom Root CAs are prohibited by organizational policy, `agy-cli-manager` retains its legacy filesystem rotation mode:

### How it works
1. **Active-Standby Profile Rotation:** Keeps one account active in `~/.gemini` or `~/.agy-cli-manager/runtime` while standby accounts remain idle.
2. **Log Watching:** Tails Antigravity CLI logs for `RESOURCE_EXHAUSTED (code 429): Individual quota reached`.
3. **Failover Execution:** On 429, rotates the disk profile to the healthiest standby account.
4. **Terminal Restart:** You must restart your terminal session to pick up the new token.

```bash
# Enable automatic filesystem switching
agy-cli-manager switch-mode auto

# Tail logs and automatically switch on quota exhaustion
agy-cli-manager watch

# Manual switch to next eligible account
agy-cli-manager switch-next
```
</details>

---

## License

MIT © [zcop](https://github.com/zcop)
