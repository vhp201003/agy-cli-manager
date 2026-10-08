from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import ssl
import sys
import time
from typing import AsyncGenerator
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

_current = Path(__file__).resolve().parent
_parent = _current.parent
for _p in (str(_current), str(_parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from agy_cli_manager.proxy import proxy_server, token_manager, cert_manager
    from agy_cli_manager.proxy.token_manager import TokenManager
    from agy_cli_manager.proxy.proxy_server import (
        REQUEST_LOGS,
        register_event_listener,
        unregister_event_listener,
    )
except ImportError:
    try:
        from . import proxy_server, token_manager, cert_manager
        from .token_manager import TokenManager
        from .proxy_server import (
            REQUEST_LOGS,
            register_event_listener,
            unregister_event_listener,
        )
    except ImportError:
        import proxy_server
        import token_manager
        import cert_manager
        from token_manager import TokenManager
        from proxy_server import (
            REQUEST_LOGS,
            register_event_listener,
            unregister_event_listener,
        )

logger = logging.getLogger("AgyProxy.FastAPI")

tm_instance = TokenManager()
active_subscribers: list[asyncio.Queue] = []
main_loop: asyncio.AbstractEventLoop | None = None


def proxy_event_listener(event: dict) -> None:
    global main_loop
    if main_loop and main_loop.is_running():
        for q in list(active_subscribers):
            main_loop.call_soon_threadsafe(q.put_nowait, event)


async def cron_token_refresh_task(interval_seconds: int = 600) -> None:
    logger.info(f"Started Background Token & Quota Refresh Cron Job (Interval: {interval_seconds}s / 10m)")
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            logger.info("[Cron Job] Refreshing tokens and quota status...")
            tm_instance.refresh_all_accounts(force=False)
            tm_instance.fetch_all_quotas()
            logger.info("[Cron Job] Token & Quota refresh cycle completed.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Cron Job] Error in cron loop: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    register_event_listener(proxy_event_listener)

    # Initial check & quota sync on startup
    tm_instance.refresh_all_accounts(force=False)
    tm_instance.fetch_all_quotas()

    cron_task = asyncio.create_task(cron_token_refresh_task(interval_seconds=600))
    yield
    cron_task.cancel()
    unregister_event_listener(proxy_event_listener)


app = FastAPI(
    title="AGY Multi-Account Router & Live Proxy",
    description="Real-time Inspection, Header Interception, and Dual Quota Tracking for Antigravity CLI",
    version="2.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/status")
async def get_status():
    status = tm_instance.get_status()
    total_accs = len(status)
    active_accs = sum(1 for a in status.values() if a["is_active"])
    return {
        "pool_size": total_accs,
        "available_accounts": active_accs,
        "proxy_port": 8899,
        "accounts": status,
        "total_requests_intercepted": len(REQUEST_LOGS),
        "timestamp": time.time(),
    }


@app.get("/api/logs")
async def get_logs():
    return list(REQUEST_LOGS)


@app.post("/api/refresh-all")
async def refresh_all():
    res = tm_instance.refresh_all_accounts(force=True)
    tm_instance.fetch_all_quotas()
    return {"message": "Refreshed all accounts and quotas", "results": res, "status": tm_instance.get_status()}


@app.post("/api/sync-quotas")
async def sync_quotas():
    res = tm_instance.fetch_all_quotas()
    return {"message": "Synced quotas for all accounts", "quotas": res, "status": tm_instance.get_status()}


@app.post("/api/accounts/{name}/refresh")
async def refresh_account(name: str):
    ok, msg = tm_instance.refresh_account_token(name)
    tm_instance.fetch_account_quota(name)
    return {"success": ok, "message": msg, "account": tm_instance.get_status().get(name)}


@app.post("/api/warmup-all")
async def warmup_all(model: str | None = None):
    res = tm_instance.warmup_all_accounts(model=model)
    return {"message": "Warmup completed for all accounts", "results": res, "status": tm_instance.get_status()}


@app.post("/api/accounts/{name}/warmup")
async def warmup_account(name: str, model: str | None = None):
    ok, msg = tm_instance.warmup_account(name, model=model)
    tm_instance.fetch_account_quota(name)
    return {"success": ok, "message": msg, "account": tm_instance.get_status().get(name)}


@app.post("/api/accounts/{name}/clear-cooldown")
async def clear_cooldown(name: str):
    tm_instance.clear_cooldown(name)
    return {"message": f"Cleared cooldown for {name}", "status": tm_instance.get_status().get(name)}




@app.get("/api/stream")
async def stream_logs(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    active_subscribers.append(q)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=10.0)
                    yield {"event": "request", "data": json.dumps(event)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "keep-alive"}
        finally:
            if q in active_subscribers:
                active_subscribers.remove(q)

    return EventSourceResponse(event_generator())


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AGY Multi-Account Router & Quota Telemetry</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #09090b;
      --card-bg: #0d0d10;
      --sub-card: #08080a;
      --border: #222226;
      --border-focus: #38bdf8;
      --border-hover: #323238;
      --text: #ededed;
      --text-muted: #71717a;
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.12);
      --green: #22c55e;
      --yellow: #f59e0b;
      --red: #ef4444;
      --purple: #c084fc;
      --font-sans: 'Geist', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: var(--font-sans); padding: 24px; min-height: 100vh; line-height: 1.5; }
    
    /* Top Header */
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 18px; flex-wrap: wrap; gap: 16px; }
    .title-group h1 { font-size: 1.25rem; font-weight: 600; color: #fff; display: flex; align-items: center; gap: 10px; letter-spacing: -0.02em; }
    .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 2.5s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.3; transform: scale(1.1); } }
    .subtitle { font-size: 0.78rem; color: var(--text-muted); margin-top: 4px; font-family: var(--font-mono); }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    
    /* Vercel Minimalist Tool Buttons */
    button { 
      background: #0a0a0c; 
      border: 1px solid var(--border); 
      color: #d4d4d8; 
      padding: 6px 12px; 
      border-radius: 6px; 
      cursor: pointer; 
      font-size: 0.78rem; 
      font-weight: 500; 
      font-family: var(--font-sans); 
      transition: all 0.15s ease; 
      display: inline-flex; 
      align-items: center; 
      justify-content: center;
      gap: 6px; 
      user-select: none;
      line-height: 1.2;
      height: 30px;
    }
    button:hover:not(:disabled) { 
      background: #18181b; 
      color: #fff; 
      border-color: var(--border-hover);
    }
    button:active:not(:disabled) { 
      transform: scale(0.98); 
    }
    button:disabled { 
      opacity: 0.45; 
      cursor: not-allowed; 
      transform: none !important;
    }
    button.primary { 
      background: #ededed; 
      border-color: #ededed; 
      color: #000; 
      font-weight: 600; 
    }
    button.primary:hover:not(:disabled) { 
      background: #ffffff; 
      border-color: #ffffff; 
      color: #000;
    }
    button.card-btn {
      height: 24px;
      padding: 3px 8px;
      font-size: 0.72rem;
      border-radius: 5px;
      color: var(--text-muted);
    }
    button.card-btn:hover:not(:disabled) {
      color: #fff;
    }
    .btn-svg { 
      width: 13px; 
      height: 13px; 
      fill: none; 
      stroke: currentColor; 
      stroke-width: 2; 
      stroke-linecap: round; 
      stroke-linejoin: round; 
      flex-shrink: 0;
    }
    .btn-spinner {
      width: 12px;
      height: 12px;
      border: 1.5px solid currentColor;
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
      display: inline-block;
      flex-shrink: 0;
    }
    @keyframes spin { 100% { transform: rotate(360deg); } }
    
    /* KPI Strip */
    .kpi-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 24px; }
    .kpi-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px; position: relative; }
    .kpi-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; margin-bottom: 4px; display: flex; justify-content: space-between; align-items: center; font-family: var(--font-mono); }
    .kpi-value { font-size: 1.5rem; font-weight: 600; font-family: var(--font-mono); display: flex; align-items: baseline; gap: 6px; letter-spacing: -0.03em; }
    .kpi-sub { font-size: 0.74rem; color: #52525b; margin-top: 4px; }
    
    /* Account Grid */
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; margin-bottom: 24px; }
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; position: relative; transition: border-color 0.2s; }
    .card:hover { border-color: var(--border-hover); }
    .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
    .acc-email { font-size: 0.85rem; font-weight: 600; color: #fff; font-family: var(--font-mono); }
    .badge { font-size: 0.68rem; padding: 2px 7px; border-radius: 4px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; font-family: var(--font-mono); }
    .badge.active { background: rgba(34, 197, 94, 0.08); color: var(--green); border: 1px solid rgba(34, 197, 94, 0.25); }
    .badge.sticky { background: rgba(56, 189, 248, 0.08); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.25); }
    .badge.cooldown { background: rgba(245, 158, 11, 0.08); color: var(--yellow); border: 1px solid rgba(245, 158, 11, 0.25); }
    
    /* Modal Tabs UI */
    .modal-tab-bar { display: flex; gap: 6px; border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 10px; }
    .modal-tab-btn { background: transparent; border: 1px solid var(--border); color: var(--text-muted); padding: 5px 12px; border-radius: 5px; font-size: 0.75rem; cursor: pointer; transition: all 0.15s; font-family: var(--font-mono); }
    .modal-tab-btn.active, .modal-tab-btn:hover { background: #18181b; color: #fff; border-color: #38bdf8; }
    
    /* Quota Bars */
    .quota-box { background: var(--sub-card); border: 1px solid rgba(255,255,255,0.06); border-radius: 8px; padding: 10px 12px; margin-top: 10px; }
    .quota-title { font-size: 0.76rem; font-weight: 700; display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; letter-spacing: 0.3px; }
    .quota-row { margin-bottom: 8px; }
    .quota-row:last-child { margin-bottom: 0; }
    .quota-row-header { display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 3px; font-family: var(--font-mono); }
    .bar-bg { width: 100%; height: 6px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; }
    .bar-fill { height: 100%; border-radius: 3px; transition: width 0.4s cubic-bezier(0.16, 1, 0.3, 1); }
    .reset-label { font-size: 0.7rem; color: var(--text-muted); margin-top: 2px; }
    .meta-row { display: flex; justify-content: space-between; align-items: center; font-size: 0.75rem; color: var(--text-muted); margin-top: 12px; font-family: var(--font-mono); }
    
    /* Telemetry Table Container */
    .telemetry-container { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }
    .telemetry-toolbar { padding: 14px 18px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; background: #111114; }
    .filter-group { display: flex; gap: 6px; align-items: center; }
    .filter-btn { padding: 5px 10px; border-radius: 6px; font-size: 0.76rem; background: transparent; border: 1px solid var(--border); color: var(--text-muted); cursor: pointer; }
    .filter-btn.active, .filter-btn:hover { background: var(--border); color: #fff; }
    .search-box { background: var(--sub-card); border: 1px solid var(--border); color: #fff; padding: 6px 12px; border-radius: 6px; font-size: 0.8rem; font-family: var(--font-mono); width: 260px; outline: none; }
    .search-box:focus { border-color: var(--accent); }
    
    .table-pane { overflow-y: auto; overflow-x: auto; max-height: 560px; }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.82rem; }
    th { color: var(--text-muted); padding: 10px 14px; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: #131317; font-weight: 600; font-family: var(--font-sans); z-index: 2; }
    td { padding: 11px 14px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; font-family: var(--font-mono); }
    tr.log-item:hover { background: rgba(255, 255, 255, 0.02); }
    .highlight-new { animation: rowHighlight 1.5s ease-out; }
    @keyframes rowHighlight { 0% { background: var(--accent-glow); } 100% { background: transparent; } }
    
    /* Chips & Badges */
    .pill { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 0.7rem; font-weight: 700; text-transform: uppercase; font-family: var(--font-mono); }
    .pill.get { background: #1e293b; color: #94a3b8; border: 1px solid #334155; }
    .pill.post { background: #142a1f; color: #4ade80; border: 1px solid #1e452e; }
    .code-chip { background: #141418; padding: 2px 6px; border-radius: 4px; font-family: var(--font-mono); font-size: 0.75rem; border: 1px solid var(--border); }
    .btn-inspect { background: #18181b; border: 1px solid var(--border); color: #d4d4d8; padding: 4px 10px; border-radius: 6px; font-size: 0.74rem; cursor: pointer; display: inline-flex; align-items: center; gap: 5px; }
    .btn-inspect:hover { background: #27272a; color: #fff; border-color: #3f3f46; }

    /* Modal Dialog */
    .modal-backdrop { 
      position: fixed; 
      inset: 0; 
      background: rgba(0, 0, 0, 0.75); 
      display: none; 
      align-items: center; 
      justify-content: center; 
      z-index: 1000; 
      backdrop-filter: blur(6px); 
      opacity: 0;
      transition: opacity 0.2s ease;
    }
    .modal-backdrop.active { 
      display: flex; 
      opacity: 1; 
    }
    .modal-box { 
      background: #0f0f12; 
      border: 1px solid var(--border); 
      border-radius: 12px; 
      width: 90%; 
      max-width: 860px; 
      max-height: 85vh; 
      display: flex; 
      flex-direction: column; 
      box-shadow: 0 25px 50px -12px rgba(0,0,0,0.8); 
      transform: scale(0.96) translateY(10px);
      transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .modal-backdrop.active .modal-box {
      transform: scale(1) translateY(0);
    }
    .modal-header { 
      padding: 16px 22px; 
      border-bottom: 1px solid var(--border); 
      display: flex; 
      justify-content: space-between; 
      align-items: center; 
      background: #141418;
      border-top-left-radius: 12px;
      border-top-right-radius: 12px;
    }
    .modal-title { font-size: 1rem; font-weight: 600; display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); }
    .modal-close { 
      background: transparent; 
      border: none; 
      color: var(--text-muted); 
      cursor: pointer; 
      padding: 6px; 
      border-radius: 6px; 
      display: flex; 
      align-items: center; 
      justify-content: center; 
      transition: background 0.15s;
    }
    .modal-close:hover { background: rgba(255,255,255,0.08); color: #fff; }
    .modal-body { padding: 20px 22px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 14px; }
    
    /* JSON Syntax Colors */
    .json-container {
      background: #08080a;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      font-family: var(--font-mono);
      font-size: 0.8rem;
      line-height: 1.5;
      overflow-x: auto;
      max-height: 440px;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .json-key { color: #38bdf8; font-weight: 500; }
    .json-string { color: #a7f3d0; }
    .json-number { color: #fde047; }
    .json-boolean { color: #f472b6; font-weight: 600; }
    .json-null { color: #94a3b8; font-style: italic; }

    /* Toast Notification */
    #toastContainer {
      position: fixed;
      bottom: 24px;
      right: 24px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      z-index: 2000;
      pointer-events: none;
    }
    .toast-msg {
      background: #141417;
      border: 1px solid #2e2e34;
      color: #fff;
      padding: 10px 16px;
      border-radius: 8px;
      font-size: 0.8rem;
      font-family: var(--font-sans);
      box-shadow: 0 8px 20px rgba(0,0,0,0.5);
      display: flex;
      align-items: center;
      gap: 8px;
      animation: toastIn 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      transition: opacity 0.2s, transform 0.2s;
      pointer-events: auto;
    }
    .toast-msg.success { border-color: #22c55e; }
    .toast-msg.error { border-color: #ef4444; }
    @keyframes toastIn {
      from { opacity: 0; transform: translateY(12px) scale(0.96); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
  </style>
</head>
<body>
  <div id="toastContainer"></div>
  <!-- Header -->
  <div class="header">
    <div class="title-group">
      <h1><div class="pulse-dot"></div> AGY Multi-Account Router</h1>
      <div class="subtitle">Proxy Port: 8899 | Real-Time Telemetry & Quota Inspector</div>
    </div>
    <div class="actions">
      <!-- Trigger 5H Quota Button -->
      <button onclick="warmupAllAccounts(this)" id="btnWarmup" title="Trigger 5H Window on all accounts">
        <svg class="btn-svg" viewBox="0 0 24 24"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 3z"/></svg>
        <span>Warmup All</span>
      </button>

      <!-- Sync Quotas Button -->
      <button onclick="syncAllQuotas(this)" class="primary" id="btnSyncQuotas" title="Sync live quotas from Google">
        <svg class="btn-svg" viewBox="0 0 24 24"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
        <span>Sync Quotas</span>
      </button>

      <!-- Refresh Tokens Button -->
      <button onclick="refreshAllTokens(this)" id="btnRefreshTokens" title="Refresh Google OAuth tokens">
        <svg class="btn-svg" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
        <span>Refresh Tokens</span>
      </button>

      <!-- Reload Button -->
      <button onclick="fetchStatus(this)" id="btnReload" title="Reload status data">
        <svg class="btn-svg" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
        <span>Reload</span>
      </button>
    </div>
  </div>

  <!-- KPI Strip -->
  <div class="kpi-strip">
    <div class="kpi-card">
      <div class="kpi-label">Active Pool <span style="color:var(--green)">●</span></div>
      <div class="kpi-value" id="kpiPoolReady">4 / 4</div>
      <div class="kpi-sub">Ready for LLM routing</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Gemini Capacity <span style="color:var(--accent)">●</span></div>
      <div class="kpi-value" id="kpiGeminiAvg" style="color:var(--accent);">-- %</div>
      <div class="kpi-sub">5h pool average capacity</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Claude Capacity <span style="color:var(--purple)">●</span></div>
      <div class="kpi-value" id="kpiClaudeAvg" style="color:var(--purple);">-- %</div>
      <div class="kpi-sub">5h pool average capacity</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Intercepted Traffic <span style="color:var(--text-muted)">●</span></div>
      <div class="kpi-value" id="kpiTotalReqs">0</div>
      <div class="kpi-sub" id="kpiSyncStatus">SSE Connected</div>
    </div>
  </div>

  <!-- Account Matrix -->
  <div class="grid" id="accountsGrid"></div>

  <!-- Telemetry Table View -->
  <div class="telemetry-container">
    <div class="telemetry-toolbar">
      <div class="filter-group">
        <span style="font-size:0.75rem; color:var(--text-muted); font-weight:700; margin-right:4px;">FILTER:</span>
        <button class="filter-btn active" onclick="setFilter('all', this)">All</button>
        <button class="filter-btn" onclick="setFilter('stream', this)">Streaming (SSE)</button>
        <button class="filter-btn" onclick="setFilter('quota', this)">429 Failover</button>
        <button class="filter-btn" onclick="setFilter('errors', this)">Errors</button>
      </div>
      <div>
        <input type="text" id="searchInput" class="search-box" placeholder="Filter endpoint or account..." oninput="applyFilters()">
      </div>
    </div>

    <div class="table-pane">
      <table id="logTable">
        <thead>
          <tr>
            <th>Time</th>
            <th>Method</th>
            <th>Model / API</th>
            <th>Endpoint</th>
            <th>Account</th>
            <th>Status</th>
            <th>Latency</th>
            <th>Telemetry</th>
          </tr>
        </thead>
        <tbody id="logBody">
          <tr id="emptyRow"><td colspan="8" style="text-align:center; padding:40px; color:var(--text-muted);">Waiting for agy requests... Run agy in your terminal!</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Modal Dialog: Payload & Response Inspector -->
  <div class="modal-backdrop" id="payloadModal" onclick="if(event.target===this)closeModal()">
    <div class="modal-box">
      <div class="modal-header">
        <div class="modal-title" id="modalTitle">Request & Response Telemetry</div>
        <button class="modal-close" onclick="closeModal()">
          <svg class="btn-svg" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <div id="modalMeta" style="font-size:0.8rem; color:var(--text-muted); font-family:var(--font-mono);"></div>
          <button onclick="copyCurrentModalContent(this)" class="primary" style="padding:5px 12px; font-size:0.75rem;">
            <svg class="btn-svg" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            <span>Copy View</span>
          </button>
        </div>

        <!-- 3 Tabs: Request, Response, Raw Tokens -->
        <div class="modal-tab-bar">
          <button class="modal-tab-btn active" id="tabBtnReq" onclick="switchModalTab('request')">Request Payload</button>
          <button class="modal-tab-btn" id="tabBtnResp" onclick="switchModalTab('response')">Response Output</button>
          <button class="modal-tab-btn" id="tabBtnRaw" onclick="switchModalTab('tokens')">Token Override Flow</button>
        </div>

        <!-- Tab 1: Request -->
        <div id="modalTabRequest" class="modal-tab-content">
          <div style="font-size:0.72rem; text-transform:uppercase; color:var(--text-muted); font-weight:700; font-family:var(--font-mono); margin-bottom:6px;">
            Inbound Request Body / Prompts:
          </div>
          <div class="json-container" id="modalJsonContainer"></div>
        </div>

        <!-- Tab 2: Response -->
        <div id="modalTabResponse" class="modal-tab-content" style="display:none;">
          <div style="font-size:0.72rem; text-transform:uppercase; color:var(--text-muted); font-weight:700; font-family:var(--font-mono); margin-bottom:6px;">
            Upstream Response Output / Stream Preview:
          </div>
          <div class="json-container" id="modalRespContainer"></div>
        </div>

        <!-- Tab 3: Tokens Override -->
        <div id="modalTabTokens" class="modal-tab-content" style="display:none;">
          <div style="font-size:0.72rem; text-transform:uppercase; color:var(--text-muted); font-weight:700; font-family:var(--font-mono); margin-bottom:6px;">
            OAuth Token Injection Flow:
          </div>
          <div style="font-size:0.8rem; background:rgba(255,255,255,0.03); padding:12px 14px; border-radius:8px; border:1px solid var(--border); font-family:var(--font-mono);" id="modalHeaderFlow"></div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const renderedIds = new Set();
    const logDataMap = new Map();
    let currentFilter = 'all';
    let currentModalRawBody = "";

    // Anti-Spam Button Debounce Utility
    async function handleActionWithButton(btn, asyncFn) {
      if (!btn || btn.disabled) return;
      btn.disabled = true;
      const svg = btn.querySelector('.btn-svg');
      let spinner = null;
      if (svg) {
        svg.style.display = 'none';
        spinner = document.createElement('span');
        spinner.className = 'btn-spinner';
        btn.insertBefore(spinner, svg);
      }
      try {
        await asyncFn();
      } finally {
        setTimeout(() => {
          if (spinner) spinner.remove();
          if (svg) svg.style.display = '';
          btn.disabled = false;
        }, 600); // 600ms anti-spam cooloff
      }
    }

    function syntaxHighlightJson(jsonObj) {
      if (typeof jsonObj !== 'string') {
        jsonObj = JSON.stringify(jsonObj, null, 2);
      }
      jsonObj = jsonObj.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      return jsonObj.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, function (match) {
        let cls = 'json-number';
        if (/^"/.test(match)) {
          if (/:$/.test(match)) {
            cls = 'json-key';
          } else {
            cls = 'json-string';
          }
        } else if (/true|false/.test(match)) {
          cls = 'json-boolean';
        } else if (/null/.test(match)) {
          cls = 'json-null';
        }
        return '<span class="' + cls + '">' + match + '</span>';
      });
    }

    function formatTimeUntil(isoString) {
      if (!isoString) return "Ready";
      const target = new Date(isoString).getTime();
      const diff = target - Date.now();
      if (diff <= 0) return "Resetting now";
      const hours = Math.floor(diff / (1000 * 60 * 60));
      const mins = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
      if (hours >= 24) {
        const days = Math.floor(hours / 24);
        const remH = hours % 24;
        return `Resets in ${days}d ${remH}h`;
      }
      return `Resets in ${hours}h ${mins}m`;
    }

    function getBarColor(pct, defaultColor) {
      if (pct < 15) return 'var(--red)';
      if (pct < 45) return 'var(--yellow)';
      return defaultColor;
    }

    async function fetchStatus(btn = null) {
      const doFetch = async () => {
        const res = await fetch('/api/status');
        const data = await res.json();
        renderAccounts(data.accounts);
        updateKPIs(data);
      };
      if (btn) await handleActionWithButton(btn, doFetch);
      else await doFetch();
    }

    function updateKPIs(data) {
      const accs = Object.values(data.accounts || {});
      const total = accs.length;
      const ready = accs.filter(a => a.is_active).length;
      document.getElementById('kpiPoolReady').innerText = `${ready} / ${total}`;

      let gemTotal = 0, claudeTotal = 0, count = 0;
      accs.forEach(a => {
        const q = a.quota || {};
        const g5h = (q.gemini && q.gemini['5h']) || { percent: 100 };
        const gW = (q.gemini && q.gemini['weekly']) || { percent: 100 };
        const c5h = (q.third_party && q.third_party['5h']) || { percent: 100 };
        const cW = (q.third_party && q.third_party['weekly']) || { percent: 100 };

        const gExhausted = gW.disabled || (gW.percent <= 5.0 && Boolean(gW.reset_time));
        const cExhausted = cW.disabled || (cW.percent <= 5.0 && Boolean(cW.reset_time));

        gemTotal += gExhausted ? 0 : g5h.percent;
        claudeTotal += cExhausted ? 0 : c5h.percent;
        count++;
      });
      if (count > 0) {
        document.getElementById('kpiGeminiAvg').innerText = `${(gemTotal / count).toFixed(2)}%`;
        document.getElementById('kpiClaudeAvg').innerText = `${(claudeTotal / count).toFixed(2)}%`;
      }
      document.getElementById('kpiTotalReqs').innerText = data.total_requests_intercepted || renderedIds.size;
    }

    function renderAccounts(accounts) {
      const container = document.getElementById('accountsGrid');
      container.innerHTML = '';
      for (const [name, acc] of Object.entries(accounts)) {
        const q = acc.quota || {};
        const g5h = (q.gemini && q.gemini['5h']) || { percent: 100, reset_time: null, disabled: false };
        const gWeekly = (q.gemini && q.gemini['weekly']) || { percent: 100, reset_time: null, disabled: false };
        const c5h = (q.third_party && q.third_party['5h']) || { percent: 100, reset_time: null, disabled: false };
        const cWeekly = (q.third_party && q.third_party['weekly']) || { percent: 100, reset_time: null, disabled: false };

        const cWeeklyExhausted = cWeekly.disabled || (cWeekly.percent <= 5.0 && Boolean(cWeekly.reset_time));
        const gWeeklyExhausted = gWeekly.disabled || (gWeekly.percent <= 5.0 && Boolean(gWeekly.reset_time));
        const c5hExhausted = c5h.disabled || (c5h.percent <= 5.0 && Boolean(c5h.reset_time));
        const g5hExhausted = g5h.disabled || (g5h.percent <= 5.0 && Boolean(g5h.reset_time));

        const isGeminiHealthy = !gWeeklyExhausted && !g5hExhausted;
        const isClaudeHealthy = !cWeeklyExhausted && !c5hExhausted;

        let badgeClass = 'active';
        let badgeText = 'POOL ACTIVE';
        if (acc.in_cooldown) {
          badgeClass = 'cooldown';
          badgeText = `COOLDOWN (${acc.cooldown_remaining_sec}s)`;
        } else if (!isGeminiHealthy && !isClaudeHealthy) {
          badgeClass = 'cooldown';
          badgeText = 'ALL EXHAUSTED';
        } else if (!isClaudeHealthy) {
          badgeClass = 'active';
          badgeText = 'GEMINI ONLY';
        } else if (!isGeminiHealthy) {
          badgeClass = 'active';
          badgeText = 'CLAUDE ONLY';
        }

        // 5h limit represents the current 5-hour session capacity; only show 0% if weekly quota is exhausted or 5h <= 5.0%
        const c5hEffectivePct = cWeeklyExhausted ? 0 : c5h.percent;
        const g5hEffectivePct = gWeeklyExhausted ? 0 : g5h.percent;

        const emailDisplay = acc.email && acc.email.includes('@') ? acc.email : (acc.email || name);
        const nameDisplay = acc.display_name && acc.display_name !== name ? acc.display_name : '';
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="card-header">
            <div style="min-width: 0; flex: 1; padding-right: 8px;">
              <div class="acc-email" title="${emailDisplay}">
                <span>${emailDisplay}</span>
              </div>
              <div style="display: flex; align-items: center; gap: 8px; margin-top: 4px;">
                <span class="code-chip" style="color:var(--accent);">Profile: ${name}</span>
                ${nameDisplay ? `<span style="font-size: 0.76rem; color: var(--text-muted);">${nameDisplay}</span>` : ''}
              </div>
            </div>
            <div class="badge ${badgeClass}">${badgeText}</div>
          </div>

          <!-- Section 1: Gemini Quota -->
          <div class="quota-box">
            <div class="quota-title" style="color: var(--accent);">
              <span>Gemini Limits</span>
              <span style="font-size:0.7rem; color:${isGeminiHealthy ? 'var(--green)' : 'var(--red)'}; font-weight: 600;">${isGeminiHealthy ? '● Ready' : '● Exhausted'}</span>
            </div>
            <div class="quota-row">
              <div class="quota-row-header">
                <span>Weekly Limit:</span>
                <b>${gWeeklyExhausted ? '<span style="color:var(--red);">Exhausted (≤5%)</span>' : Number(gWeekly.percent).toFixed(2) + '%'}</b>
              </div>
              <div class="bar-bg">
                <div class="bar-fill" style="width: ${gWeekly.percent}%; background: ${getBarColor(gWeekly.percent, 'var(--accent)')}"></div>
              </div>
              <div class="reset-label">${formatTimeUntil(gWeekly.reset_time)}</div>
            </div>
            <div class="quota-row" style="margin-top: 8px;">
              <div class="quota-row-header">
                <span>Five Hour Limit:</span>
                <b>${gWeeklyExhausted ? '<span style="color:var(--red);">0% (Weekly Limit)</span>' : (g5h.percent <= 5.0 && g5h.reset_time ? '<span style="color:var(--red);">Exhausted (≤5%)</span>' : Number(g5hEffectivePct).toFixed(2) + '%')}</b>
              </div>
              <div class="bar-bg">
                <div class="bar-fill" style="width: ${g5hEffectivePct}%; background: ${getBarColor(g5hEffectivePct, 'var(--accent)')}"></div>
              </div>
              <div class="reset-label">${gWeeklyExhausted ? 'Blocked by weekly limit' : formatTimeUntil(g5h.reset_time)}</div>
            </div>
          </div>

          <!-- Section 2: Claude & GPT Quota -->
          <div class="quota-box" style="border-color: rgba(192, 132, 252, 0.2);">
            <div class="quota-title" style="color: var(--purple);">
              <span>Claude & GPT Limits</span>
              <span style="font-size:0.7rem; color:${isClaudeHealthy ? 'var(--green)' : 'var(--red)'}; font-weight: 600;">${isClaudeHealthy ? '● Ready' : '● Exhausted'}</span>
            </div>
            <div class="quota-row">
              <div class="quota-row-header">
                <span>Weekly Limit:</span>
                <b>${cWeeklyExhausted ? '<span style="color:var(--red);">Exhausted (≤5%)</span>' : Number(cWeekly.percent).toFixed(2) + '%'}</b>
              </div>
              <div class="bar-bg">
                <div class="bar-fill" style="width: ${cWeekly.percent}%; background: ${getBarColor(cWeekly.percent, 'var(--purple)')}"></div>
              </div>
              <div class="reset-label">${formatTimeUntil(cWeekly.reset_time)}</div>
            </div>
            <div class="quota-row" style="margin-top: 8px;">
              <div class="quota-row-header">
                <span>Five Hour Limit:</span>
                <b>${cWeeklyExhausted ? '<span style="color:var(--red);">0% (Weekly Limit)</span>' : (c5h.percent <= 5.0 && c5h.reset_time ? '<span style="color:var(--red);">Exhausted (≤5%)</span>' : Number(c5hEffectivePct).toFixed(2) + '%')}</b>
              </div>
              <div class="bar-bg">
                <div class="bar-fill" style="width: ${c5hEffectivePct}%; background: ${getBarColor(c5hEffectivePct, 'var(--purple)')}"></div>
              </div>
              <div class="reset-label">${cWeeklyExhausted ? 'Blocked by weekly limit' : formatTimeUntil(c5h.reset_time)}</div>
            </div>
          </div>

          <div class="meta-row">
            <span>Exp: <b>${acc.token_expires_in_min}m</b></span>
            <span>OAuth: <b style="color: ${acc.has_refresh_token ? 'var(--green)' : 'var(--red)'};">${acc.has_refresh_token ? 'Connected' : 'Missing'}</b></span>
            <div style="display: flex; gap: 6px;">
              <button onclick="warmupSingle('${name}', this)" class="card-btn" title="Trigger 5H Window for ${name}">
                <svg class="btn-svg" viewBox="0 0 24 24"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 3z"/></svg>
                <span>Warmup</span>
              </button>
              <button onclick="refreshSingle('${name}', this)" class="card-btn" title="Refresh Token for ${name}">
                <svg class="btn-svg" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                <span>Refresh</span>
              </button>
            </div>
          </div>
        `;
        container.appendChild(card);
      }
    }

    function showToast(message, type = 'info') {
      const container = document.getElementById('toastContainer');
      if (!container) return;
      const t = document.createElement('div');
      t.className = `toast-msg ${type}`;
      t.innerHTML = `<span>${type === 'success' ? '✓' : type === 'error' ? '⚠' : 'ℹ'}</span> <span>${message}</span>`;
      container.appendChild(t);
      setTimeout(() => {
        t.style.opacity = '0';
        t.style.transform = 'translateY(10px) scale(0.95)';
        setTimeout(() => t.remove(), 250);
      }, 4000);
    }

    async function warmupAllAccounts(btn) {
      await handleActionWithButton(btn, async () => {
        try {
          const res = await fetch('/api/warmup-all', { method: 'POST' });
          const data = await res.json();
          const successes = Object.values(data.results || {}).filter(r => r.success).length;
          const total = Object.keys(data.results || {}).length;
          showToast(`Trigger 5h window: ${successes}/${total} accounts activated`, successes > 0 ? 'success' : 'error');
          await fetchStatus();
        } catch (e) {
          showToast(`Warmup error: ${e.message}`, 'error');
        }
      });
    }

    async function warmupSingle(name, btn) {
      await handleActionWithButton(btn, async () => {
        try {
          const res = await fetch(`/api/accounts/${name}/warmup`, { method: 'POST' });
          const data = await res.json();
          showToast(`${name}: ${data.message}`, data.success ? 'success' : 'error');
          await fetchStatus();
        } catch (e) {
          showToast(`${name} error: ${e.message}`, 'error');
        }
      });
    }

    async function refreshAllTokens(btn) {
      await handleActionWithButton(btn, async () => {
        try {
          const res = await fetch('/api/refresh-all', { method: 'POST' });
          const data = await res.json();
          showToast('OAuth tokens refreshed successfully', 'success');
          await fetchStatus();
        } catch (e) {
          showToast(`Refresh error: ${e.message}`, 'error');
        }
      });
    }

    async function syncAllQuotas(btn) {
      await handleActionWithButton(btn, async () => {
        try {
          const res = await fetch('/api/sync-quotas', { method: 'POST' });
          const data = await res.json();
          showToast('Quotas synced from Google successfully', 'success');
          await fetchStatus();
        } catch (e) {
          showToast(`Sync error: ${e.message}`, 'error');
        }
      });
    }

    async function refreshSingle(name, btn) {
      await handleActionWithButton(btn, async () => {
        try {
          const res = await fetch(`/api/accounts/${name}/refresh`, { method: 'POST' });
          const data = await res.json();
          showToast(`${name}: ${data.message || 'Refreshed'}`, data.success ? 'success' : 'error');
          await fetchStatus();
        } catch (e) {
          showToast(`${name} refresh error: ${e.message}`, 'error');
        }
      });
    }



    async function pollLogs() {
      try {
        const res = await fetch('/api/logs');
        const logs = await res.json();
        if (logs.length > 0) {
          const emptyRow = document.getElementById('emptyRow');
          if (emptyRow) emptyRow.remove();
        }
        logs.forEach(evt => {
          logDataMap.set(evt.id, evt);
          if (!renderedIds.has(evt.id)) {
            appendLogRow(evt, true);
          }
        });
        document.getElementById('kpiTotalReqs').innerText = logs.length;
      } catch (e) {
        console.error("Polling logs error:", e);
      }
    }

    let currentModalActiveTab = 'request';
    let currentModalEvt = null;

    function switchModalTab(tab) {
      currentModalActiveTab = tab;
      document.querySelectorAll('.modal-tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.modal-tab-content').forEach(c => c.style.display = 'none');

      if (tab === 'request') {
        document.getElementById('tabBtnReq').classList.add('active');
        document.getElementById('modalTabRequest').style.display = 'block';
      } else if (tab === 'response') {
        document.getElementById('tabBtnResp').classList.add('active');
        document.getElementById('modalTabResponse').style.display = 'block';
      } else if (tab === 'tokens') {
        document.getElementById('tabBtnRaw').classList.add('active');
        document.getElementById('modalTabTokens').style.display = 'block';
      }
    }

    function copyCurrentModalContent(btn) {
      if (!currentModalEvt) return;
      let textToCopy = "";
      if (currentModalActiveTab === 'request') {
        textToCopy = currentModalEvt.body || "";
      } else if (currentModalActiveTab === 'response') {
        textToCopy = currentModalEvt.response_preview || "";
      } else {
        textToCopy = JSON.stringify({
          inbound_token: currentModalEvt.inbound_token,
          overridden_token: currentModalEvt.overridden_token,
          account: currentModalEvt.account,
          status: currentModalEvt.status,
          latency_ms: currentModalEvt.latency_ms
        }, null, 2);
      }
      navigator.clipboard.writeText(textToCopy);
      const span = btn.querySelector('span');
      const oldText = span.innerText;
      span.innerText = "Copied!";
      setTimeout(() => { span.innerText = oldText; }, 1500);
    }

    function appendLogRow(evt, isNew = true) {
      if (renderedIds.has(evt.id)) return;
      renderedIds.add(evt.id);
      logDataMap.set(evt.id, evt);

      const tbody = document.getElementById('logBody');
      const emptyRow = document.getElementById('emptyRow');
      if (emptyRow) emptyRow.remove();

      const row = document.createElement('tr');
      row.id = `row-${evt.id}`;
      row.className = 'log-item' + (isNew ? ' highlight-new' : '');

      let statusColor = 'var(--green)';
      if (evt.status === 429) statusColor = 'var(--yellow)';
      else if (evt.status >= 400) statusColor = 'var(--red)';

      const methodClass = (evt.method || 'GET').toLowerCase();
      const bodySize = evt.body_size !== undefined ? evt.body_size : (evt.body ? evt.body.length : 0);
      const sizeLabel = bodySize > 1024 ? `${(bodySize/1024).toFixed(1)} KB` : `${bodySize} B`;
      const modelLabel = evt.model || 'API Request';

      row.innerHTML = `
        <td style="color:var(--text-muted); font-size:0.75rem;">${evt.time}</td>
        <td><span class="pill ${methodClass}">${evt.method}</span></td>
        <td><span class="code-chip" style="color:var(--accent); font-weight:600;">${modelLabel}</span></td>
        <td><span class="code-chip" title="${evt.path}">${evt.path.length > 28 ? evt.path.substring(0,28)+'...' : evt.path}</span></td>
        <td><b style="color:#fff;">${evt.account}</b></td>
        <td><b style="color: ${statusColor};">${evt.status_text || evt.status}</b></td>
        <td style="color:var(--text-muted);">${evt.latency_ms}ms</td>
        <td>
          <button class="btn-inspect" onclick="openPayloadModal('${evt.id}')">
            <svg class="btn-svg" style="width:13px; height:13px;" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            <span>Telemetry (${sizeLabel})</span>
          </button>
        </td>
      `;

      if (tbody.firstChild) {
        tbody.insertBefore(row, tbody.firstChild);
      } else {
        tbody.appendChild(row);
      }
      applyFilters();
    }

    function openPayloadModal(id) {
      const evt = logDataMap.get(id);
      if (!evt) return;
      currentModalEvt = evt;

      document.getElementById('modalTitle').innerHTML = `
        <span class="pill ${(evt.method||'POST').toLowerCase()}">${evt.method}</span>
        <span style="color:var(--accent);">${evt.model || 'API'}</span> &bull;
        <span style="color:var(--text-muted); font-size:0.85rem;">${evt.path}</span>
      `;
      let statusColor = evt.status >= 400 ? 'var(--red)' : 'var(--green)';
      if (evt.status === 429) statusColor = 'var(--yellow)';
      document.getElementById('modalMeta').innerHTML = `
        Account: <b style="color:var(--accent);">${evt.account}</b> &bull; 
        Status: <b style="color:${statusColor}">${evt.status_text || evt.status}</b> &bull; 
        Latency: <b>${evt.latency_ms}ms</b> &bull; Time: <b>${evt.time}</b>
      `;
      document.getElementById('modalHeaderFlow').innerHTML = `
        <div style="margin-bottom:6px;"><span style="color:var(--text-muted);">Inbound Client Token:</span> <span class="code-chip">${evt.inbound_token}</span></div>
        <div style="margin-bottom:6px;"><span style="color:var(--accent);">➔ Routed Account:</span> <b style="color:#fff;">${evt.account}</b></div>
        <div><span style="color:var(--accent);">➔ Google OAuth Token:</span> <span class="code-chip">${evt.overridden_token}</span></div>
      `;

      // Tab 1: Request
      let reqHtml = '<span class="json-null">(empty body)</span>';
      if (evt.body) {
        try {
          const parsed = JSON.parse(evt.body);
          reqHtml = syntaxHighlightJson(parsed);
        } catch (e) {
          reqHtml = `<span class="json-string">${evt.body}</span>`;
        }
      }
      document.getElementById('modalJsonContainer').innerHTML = reqHtml;

      // Tab 2: Response Output
      let respHtml = '<span class="json-null">(no response preview captured)</span>';
      if (evt.response_preview) {
        try {
          const parsed = JSON.parse(evt.response_preview);
          respHtml = syntaxHighlightJson(parsed);
        } catch (e) {
          respHtml = `<span class="json-string">${evt.response_preview}</span>`;
        }
      }
      document.getElementById('modalRespContainer').innerHTML = respHtml;

      switchModalTab('request');
      document.getElementById('payloadModal').classList.add('active');
    }

    function closeModal() {
      document.getElementById('payloadModal').classList.remove('active');
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeModal();
    });

    function setFilter(type, el) {
      currentFilter = type;
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      el.classList.add('active');
      applyFilters();
    }

    function applyFilters() {
      const q = (document.getElementById('searchInput').value || "").toLowerCase();
      document.querySelectorAll('tr.log-item').forEach(row => {
        const id = row.id.replace('row-', '');
        const evt = logDataMap.get(id);
        if (!evt) return;

        let matchesFilter = true;
        if (currentFilter === 'stream') matchesFilter = evt.path.includes('streamGenerateContent');
        else if (currentFilter === 'quota') matchesFilter = evt.status === 429;
        else if (currentFilter === 'errors') matchesFilter = evt.status >= 400;

        let matchesSearch = true;
        if (q) {
          const matchTarget = `${evt.path} ${evt.account} ${evt.method} ${evt.body || ''}`.toLowerCase();
          matchesSearch = matchTarget.includes(q);
        }

        row.style.display = (matchesFilter && matchesSearch) ? '' : 'none';
      });
    }

    function initSSE() {
      try {
        const evtSource = new EventSource('/api/stream');
        evtSource.addEventListener('request', (e) => {
          const data = JSON.parse(e.data);
          appendLogRow(data, true);
          fetchStatus();
        });
        evtSource.onerror = () => {
          document.getElementById('kpiSyncStatus').innerText = 'Polling (SSE Reconnecting)';
        };
        evtSource.onopen = () => {
          document.getElementById('kpiSyncStatus').innerText = 'Real-time SSE Active';
        };
      } catch (err) {
        console.warn("SSE init error:", err);
      }
    }

    fetchStatus();
    pollLogs();
    initSSE();

    setInterval(pollLogs, 1500);
    setInterval(fetchStatus, 8000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)
