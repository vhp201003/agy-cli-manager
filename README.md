# agy-cli-manager

`agy-cli-manager` is a Python account manager for Antigravity CLI (`agy`) with active-standby failover, quota-aware switching, and machine-readable automation APIs.

It helps you run multiple Antigravity CLI accounts more safely by:

- switching away from low-quota or failed accounts
- watching live Antigravity CLI logs for `Individual quota reached` and failing over automatically
- keeping a managed runtime profile in sync with the active account
- exposing CLI and Python APIs for bots, schedulers, and external apps
- supporting manual or automatic account rotation policies

Keywords:
Antigravity CLI account manager, Antigravity CLI multi account manager, Antigravity multi account auth, Antigravity login manager, Antigravity auth manager, Antigravity account switcher, agy multi account manager, agy multi account auth, agy login manager, agy auth manager, agy failover, agy quota switching, Gemini CLI multi account auth, Gemini CLI account rotation.

It operates in two complementary modes:

- **CLI Profile Switcher:** Active-standby filesystem rotation for interactive Antigravity CLI sessions, watching logs for quota exhaustion and failing over automatically.
- **Multi-Account Proxy & OpenAI Gateway:** Concurrent token pool with automatic 429/403 failover, live telemetry web dashboard, and standard OpenAI-compatible `/v1/chat/completions` API.

It is application-agnostic. A Telegram bot or external coding agent can call it, but the manager itself is not client-specific.

![Sanitized dashboard example](docs/dashboard-screenshot.svg)

Project links:

- Repo: `https://github.com/zcop/agy-cli-manager`
- Release wheel: `https://github.com/zcop/agy-cli-manager/releases`
- GitHub Pages site: `https://zcop.github.io/agy-cli-manager/`

## What it does

- stores multiple account profiles safely
- keeps one account active while others stay standby/cooldown/disabled
- supports isolated interactive `agy` login
- can import an existing `~/.gemini` or similar live home
- supports both manual-only and automatic failover switching modes
- prefers fuller, healthier standby accounts when auto-switching
- tracks cached identity, health, and usage metadata
- tracks separate Gemini and Claude/GPT-OSS five-hour and weekly quota pools
- tracks live switch coordinator state for callers that need to wait on failover
- exposes CLI commands and JSON output for automation
- supports account failover with cooldowns and lock-protected state changes
- tails Antigravity CLI logs so a running `agy` TUI can trigger failover without a bot caller

## Requirements

- Python 3.10+
- a working `agy` binary available in `PATH`, or passed explicitly with `--agy-binary`
- a terminal if you want to use `login` or the full-screen dashboard

## Install

From a GitHub release wheel:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install https://github.com/zcop/agy-cli-manager/releases/download/v0.2.2/agy_cli_manager-0.2.2-py3-none-any.whl
```

To upgrade an existing installation to this release:

```bash
pip install --upgrade https://github.com/zcop/agy-cli-manager/releases/download/v0.2.2/agy_cli_manager-0.2.2-py3-none-any.whl
```

From this repo:

```bash
cd agy-cli-manager
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

After that, you can use either:

```bash
agy-cli-manager --help
```

or:

```bash
PYTHONPATH=src python3 -m agy_cli_manager.cli --help
```

## Quick Start

### 1. Initialize the manager state

```bash
agy-cli-manager init
```

By default, state lives under:

```text
~/.agy-cli-manager
```

You can override that with `--root /path/to/root`.

### 2. Add your first account

If you already have a live Antigravity home:

```bash
agy-cli-manager import-current my-account ~/.gemini
```

If you want the manager to drive a fresh interactive login itself:

```bash
agy-cli-manager login my-account --agy-binary /path/to/agy
```

`login` will hand your terminal to a real `agy` session. Complete the normal Antigravity onboarding/login there, then exit `agy`. The manager will save the resulting profile snapshot.

### 3. Check what is active

```bash
agy-cli-manager status
agy-cli-manager current
agy-cli-manager list
```

### 4. Open the dashboard

```bash
agy-cli-manager
```

### 5. Multi-Account Proxy & OpenAI API Gateway

Run the built-in dual-surface gateway with token pooling, automatic 429/403 failover, live web telemetry, and standard OpenAI `/v1/chat/completions` API:

```bash
# Install proxy dependencies
pip install ".[proxy]"

# Start proxy gateway (Web Dashboard & API on :8800, forward proxy on :8899)
agy-cli-manager proxy
```

The server exposes two distinct ports:
- **Port `8899`** (`--proxy-port`): Forward HTTP/HTTPS proxy for Antigravity CLI (`agy-run`) with SSL interception.
- **Port `8800`** (`--dashboard-port`): FastAPI server hosting the Web Dashboard, interactive OpenAPI docs (`/docs`), and OpenAI-compatible API (`/v1/chat/completions`).

#### Using with Antigravity CLI
In your working terminal, run:
```powershell
agy-run
# or: agy-cli-manager run
```
*(Automatically hooks into the proxy at `127.0.0.1:8899` and configures the SSL certificate, or safely falls back to native `agy` if the proxy is offline).*

#### Using the OpenAI-Compatible API (`/v1/chat/completions`)
Call the endpoint directly with curl or any HTTP client:
```bash
curl http://127.0.0.1:8800/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -H "x-session-id: agent-session-1" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

Or configure any OpenAI SDK, agent, or tool (Cursor, Cline, Aider, OpenCode):
```bash
export OPENAI_BASE_URL="http://127.0.0.1:8800/v1"
export OPENAI_API_KEY="none"
```

Features included:
- **Automatic Model Translation:** Aliases legacy and standard model names (e.g., `gemini-1.5-pro-latest` mapped to `gemini-2.5-flash`).
- **Bidirectional Tool Calling:** Full translation between OpenAI `tools`/`tool_calls` and Gemini `functionDeclarations`/`functionResponse`.
- **Streaming:** Server-Sent Events (`stream: true`) fully compatible with standard OpenAI streaming clients.
- **Resilient Multi-Account Routing:** Manages account token pools, preserves session stickiness via `x-session-id` (or agent request IDs), auto-refreshes OAuth tokens, and transparently retries on `429`/`403` with cooldown backoff.
- **Live SSE Telemetry Dashboard:** Web interface at `http://127.0.0.1:8800` with payload inspection and token statistics.
- **1-Click 5H Window Quota Trigger:** Proactively initializes the 5h quota window across all accounts using minimal token cost (`daily-cloudcode-pa.googleapis.com`).

### 6. Auto-switch when quota is full

When `agy` hits **Individual quota reached**, the manager switches to the next account. Restart `agy` after that. The old process may keep writing quota errors; those lines do not rotate again until you acknowledge the restart or a new session log appears.

![Quota full? Switch accounts.](docs/quota-log-watch.png)

```bash
agy-cli-manager switch-mode auto
agy-cli-manager watch
```

After restarting `agy`:

```bash
agy-cli-manager ack-restart
```

Leave the dashboard open instead of `watch` if you prefer (`Y` acknowledges the restart). Do not pass `--from-start` unless you intend to replay old quota errors.

## First Useful Commands

```bash
agy-cli-manager status --json
agy-cli-manager whoami
agy-cli-manager models --json
agy-cli-manager ensure-active --json
agy-cli-manager ensure-active --family gemini --json
agy-cli-manager ensure-active --family other --json
agy-cli-manager resolve-route --family gemini --json
agy-cli-manager switch-mode
agy-cli-manager switch-mode manual
agy-cli-manager switch-mode auto
agy-cli-manager switch-policy --json
agy-cli-manager switch-policy --short-threshold 10 --refresh-failure-threshold 2 --candidate-strategy balanced
agy-cli-manager switch-policy --gemini-threshold 10 --other-threshold 10
agy-cli-manager switch-policy --family-fallback-strategy same-family-first
agy-cli-manager refresh-usage --json
agy-cli-manager switch-next
agy-cli-manager rotate-after-failure --reason quota --cooldown-minutes 60 --json
agy-cli-manager watch
agy-cli-manager watch --once --json
agy-cli-manager ack-restart
```

The current switch policy is stored in manager state and can be controlled by either:

- CLI: `switch-mode`, `switch-policy`, `ensure-active`
- Python API: `get_status_snapshot()`, `get_switch_policy()`, `update_switch_policy()`, `ensure_active_account()`

Directory layout:

```text
~/.agy-cli-manager/
├── accounts/
│   └── <account-name>/
│       └── .gemini/
│           └── ...
├── runtime/
│   └── .gemini/
└── state.json
```

Optional integration:

- `live_dir` can point at a real Antigravity/Gemini CLI home such as `~/.gemini`
- when set, switches sync the managed active profile into that live CLI home

Example:

```bash
agy-cli-manager set-live-dir ~/.gemini
agy-cli-manager apply-active
```

This is useful when another process launches `agy` and you want that live home to always reflect the currently active saved profile.

For a full list of available subcommands and flags, run:

```bash
agy-cli-manager --help
```

`add` accepts either:

- a directory that is already a `.gemini` profile root
- or a parent directory containing `.gemini/`

## JSON/API-oriented usage

For automation, prefer the JSON-capable commands:

```bash
agy-cli-manager status --json
agy-cli-manager current --json
agy-cli-manager list --json
agy-cli-manager ensure-active --json
agy-cli-manager ensure-active --family other --json
agy-cli-manager resolve-route --family gemini --fallback-strategy same-account-first --json
agy-cli-manager switch-policy --json
agy-cli-manager switch-policy --short-threshold 12.5 --refresh-failure-threshold 3 --candidate-strategy highest-short --json
agy-cli-manager refresh-usage account1 --json
agy-cli-manager refresh-due --json
agy-cli-manager models --json
agy-cli-manager rotate-after-failure --reason quota --family other --cooldown-minutes 60 --json
agy-cli-manager watch --once --json
```

Typical external-app flow:

1. read current state with `status --json`
2. call `ensure-active --family gemini|other --json` before sending real work so the manager evaluates the quota pool the selected model will use
3. read `switch_mode` and `switch_policy` to decide how aggressively your caller should auto-fail over
4. use `models --json` if the caller needs model choices for the active account
5. call `refresh-usage --json` or `refresh-due --json` only when needed
6. if a real request fails due to quota, call `rotate-after-failure --family gemini|other --json`; omit the family only when it is genuinely unknown
7. inspect `switch_runtime` or wait briefly until it leaves `switching`
8. retry the real request once on the new active account
9. persist caller-side observations back with `update-meta`

For a chatbox/load-balancer caller, `resolve-route` implements the family/account matrix and returns `selected_family` plus the active account:

- `same-family-first` (default): current account/preferred family, another account/preferred family, current account/other family, then another account/other family
- `same-account-first`: current account/preferred family, current account/other family, another account/preferred family, then another account/other family
- `strict-family`: never cross to the other model family

The manager applies an allowed account switch. It does not select a concrete model inside `agy`; the caller uses `selected_family` to choose the model. In manual switch mode, a route that needs another account returns `outcome=switch_required` and `recommended_account` unless `--force-switch` is supplied.

Notes:

- running `agy-cli-manager` with no subcommand opens the full-screen dashboard
- `dashboard` is a TTY-only full-screen view with a fast local-only UI refresh and manual account actions
- `list`, `current`, `activate`, and `rotate` are convenience commands for standalone use; they map to the same manager state as the lower-level commands.
- local operator notes such as `AGENTS.md` are intentionally kept untracked and are not part of the public repo contract.
- `agy-cli-manager login` prompts for the account name if you do not pass one
- `switch-next` skips accounts in cooldown.
- `mark-bad` clears the active pointer if that account was active.
- `ensure-active --family gemini|other` evaluates the requested model family's five-hour and weekly quota and can recover from no active account, known low quota, auth missing, or repeated refresh failures. Omitting `--family` preserves the legacy Gemini-oriented behavior.
- `ensure-active` returns JSON with `switch_runtime`, so callers can see whether the manager is idle, switching, ready, or has no standby account available.
- `switch-mode` controls whether `rotate-after-failure` automatically moves to the next eligible standby account or stops after marking the active account bad.
- `switch-policy` controls per-family proactive short-window thresholds, refresh-failure threshold, and standby candidate ranking strategy. `--short-threshold` sets both families; `--gemini-threshold` and `--other-threshold` override them independently.
- `family_fallback_strategy` controls whether routing preserves the requested family, preserves the current account, or forbids cross-family fallback.
- state and switching are protected by a single lock file so a caller can safely trigger failover from another process.
- `set-live-dir` lets the manager drive a real CLI home in addition to its own internal `runtime/`.
- the manager currently copies the managed profile under `.gemini/`, centered on the Antigravity auth/token artifacts it needs for switching.
- it supports Antigravity-style `antigravity-cli/antigravity-oauth-token` auth storage and related identity extraction.
- `login` hands the terminal directly to a real `agy` session in the configured runtime home; complete onboarding/login there, exit `agy`, and the manager then saves the captured profile snapshot.
- `login` stores the profile under the detected account name when available, not just the typed label.
- if that detected account already exists, `login` warns and asks whether to overwrite the saved profile.
- `whoami` reports the detected signed-in account name from profile metadata, and `--probe-usage` can additionally run `agy -p /usage` against that profile as a live check.
- `models` runs `agy models` for the active account or a named saved profile and can return structured JSON for external callers.
- the manager intentionally does not use scripted PTY startup probing for `agy`; profile switching is filesystem-based. Runtime health still comes from real request success/failure, including Antigravity CLI log lines.
- `watch` tails `live_dir/antigravity-cli/log/` (and `cli.log`) for `RESOURCE_EXHAUSTED (code 429): Individual quota reached` and weekly quota lines. It starts at end-of-file so historical quota errors are not replayed.
- in `auto` mode, `watch` and the dashboard log poll call `rotate-after-failure` with `trigger=log-watch`. In `manual` mode they report the error and leave the active account in place unless `--force-switch` is set.
- a switched profile is on disk (and in the live CLI home) immediately; a running `agy` process must be restarted to pick up the new token.
- in `auto` mode, `ensure-active --family ...` can proactively switch away when either that family's cached five-hour or weekly window falls to its configured threshold. A family-specific quota failure records only a `family_cooldowns` entry; it does not globally cool down an account whose other model family remains usable.
- cached quota is advisory; real runtime failure is still the final authority for callers such as bots.
- when auto-switching for a requested family, the manager rejects candidates known to be depleted in that family, then ranks the remaining pool by health and that family's remaining quota. Unknown quota stays eligible but ranks behind known usable quota.
- the default switch policy uses a 10% threshold for both families, `refresh_failure_threshold=2`, and `candidate_strategy=balanced`.
- `rotate-after-failure` is the public failover operation for external apps: mark the current active account bad, optionally put it in cooldown, then switch to the next eligible standby account.
- `rotate-after-failure` is idempotent across a short dedupe window and reports an `outcome` such as `switched`, `already_switched`, or `no_candidate`.
- `switch_runtime` is persisted in state so a caller can coordinate retry logic without racing another caller into a second switch.
- `rotate-after-failure` follows the persisted switch mode by default: `auto` attempts failover, `manual` leaves the manager inactive until an operator or caller explicitly switches accounts. Use `--force-switch` to override that for one run.
- `update-meta` lets an external app persist cached runtime metadata such as usage, reset time, health, last check, and next refresh time.
- `refresh-due` is the non-interactive refresh entrypoint for cron/systemd/external callers; it refreshes the active account first when due, otherwise the first due eligible standby account.
- usage metadata is stored under `usage_windows.short` and `usage_windows.weekly`; the old flat `usage_*` and `reset_at` fields remain as compatibility aliases for the short window.
- dashboard keybindings: `Up/Down` or `j/k` move, `n` login, `i` import, `Enter` or `a` activate, `r` rotate, `w` toggle switch mode (`auto`/`manual`), `e` enable/disable, `c` clear bad, `m` mark bad, `s` cycle sort (`added`, `usage`, `countdown`), `u` local refresh, `t` cycle UI refresh (`5s/10s/15s/30s`), `q` quit.
- dashboard overview now shows both account quota state and switch coordinator state.
- while the dashboard is open it also tails live Antigravity CLI logs every second and can fail over in `auto` mode. The header shows `LogWatch: restart agy` until you restart `agy` after a log-triggered switch.

Cached runtime metadata:

- usage/reset/health data is persisted in manager state
- the dashboard list currently uses the short window for its usage and countdown columns
- the selected-account panel shows five-hour and weekly quota for both Gemini and Claude/GPT-OSS model families
- on relaunch, the dashboard reuses cached metadata immediately
- countdowns and freshness are recalculated locally from saved timestamps
- external apps should update this metadata after real checks or real requests
- fast dashboard refresh does not itself perform live checks

Python usage:

```python
from pathlib import Path

from agy_cli_manager import (
    build_paths,
    get_status_snapshot,
    get_switch_policy,
    list_models,
    poll_quota_logs,
    rotate_after_failure,
    update_switch_policy,
)

paths = build_paths(Path.home() / ".agy-cli-manager")
snapshot = get_status_snapshot(paths)
policy = get_switch_policy(paths)
update_switch_policy(paths, short_usage_threshold_percent=12.5, candidate_strategy="highest-short")
models = list_models(paths)
result = rotate_after_failure(paths, reason="quota", cooldown_minutes=60)
print(snapshot["active"], "->", result.switched_to)
print(policy)
print([model["name"] for model in models["models"]])
```

Public Python API:

Import core coordinator routines directly from `agy_cli_manager` (see [`src/agy_cli_manager/__init__.py`](src/agy_cli_manager/__init__.py) for public exports).

Important returned state:

- `get_status_snapshot(paths)` includes `switch_runtime` and `log_watch`
- `ensure_active_account(...)` reports the active account decision
- `rotate_after_failure(...)` returns a `RotationResult` with `outcome`

`switch_runtime` has these practical states:

- `idle`: no failover is happening
- `switching`: a caller has started coordinated failover
- `ready`: failover finished and an active account is set
- `no_account`: failover finished but no eligible standby account was available

More explicit example:

```python
from pathlib import Path

from agy_cli_manager import build_paths, ensure_layout, list_models

paths = build_paths(Path.home() / ".agy-cli-manager")
ensure_layout(paths)

payload = list_models(paths)
for model in payload["models"]:
    print(model["name"], model["variant"])
```

## Running Tests

Run the test suite using pytest:

```bash
python -m pytest tests/unit/ tests/integration/ tests/test_router.py
```
