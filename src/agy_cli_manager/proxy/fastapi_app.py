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

    # Aggregate telemetry metrics across intercepted logs
    logs = list(REQUEST_LOGS)
    total_reqs = len(logs)
    failovers = sum(1 for r in logs if r.get("retried") or r.get("status") == 429)
    success_reqs = sum(1 for r in logs if r.get("status") and r.get("status") < 400)
    success_rate = round((success_reqs / total_reqs * 100), 1) if total_reqs > 0 else 100.0

    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    latencies = []

    for r in logs:
        t = r.get("tokens")
        if t and isinstance(t, dict):
            total_tokens += t.get("total", 0)
            prompt_tokens += t.get("prompt", 0)
            completion_tokens += t.get("completion", 0)
        lat = r.get("latency_ms")
        if lat is not None:
            latencies.append(lat)

    avg_latency = round(sum(latencies) / len(latencies), 0) if latencies else 0

    return {
        "pool_size": total_accs,
        "available_accounts": active_accs,
        "proxy_port": 8899,
        "accounts": status,
        "total_requests_intercepted": total_reqs,
        "failover_count": failovers,
        "success_rate": success_rate,
        "tokens": {
            "total": total_tokens,
            "prompt": prompt_tokens,
            "completion": completion_tokens,
        },
        "avg_latency_ms": int(avg_latency),
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

from agy_cli_manager.proxy.translators.openai import convert_openai_messages_to_gemini, convert_openai_tools_to_gemini, convert_gemini_response_to_openai, convert_gemini_error_to_openai
from agy_cli_manager.proxy.translators.streaming import stream_gemini_to_openai
import httpx
from agy_cli_manager.proxy.proxy_server import broadcast_event, _extract_usage_metadata, _format_body_for_log
import uuid

_MODEL_MAP = {
    "gemini-1.5-pro-latest": "gemini-2.5-flash",
    "gemini-1.5-pro": "gemini-2.5-flash",
    "gemini-1.5-flash-latest": "gemini-2.5-flash",
    "gemini-1.5-flash": "gemini-2.5-flash",
    "gemini-1.0-pro": "gemini-2.5-flash",
    "gemini-pro": "gemini-2.5-flash",
    "gemini-pro-latest": "gemini-2.5-flash",
}

_CLOUDCODE_HOST = "daily-cloudcode-pa.googleapis.com"
_CLOUDCODE_PROJECT = "aicode-consumers"

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        
    model_name = body.get("model", "gemini-2.5-flash")
    if not model_name.startswith("gemini"):
        model_name = "gemini-2.5-flash"
    model_name = _MODEL_MAP.get(model_name, model_name).replace("-latest", "")
        
    contents, system_instruction = convert_openai_messages_to_gemini(body.get("messages", []))
    gemini_payload = {"contents": contents}
    if system_instruction:
        gemini_payload["systemInstruction"] = system_instruction
        
    if "temperature" in body:
        gemini_payload.setdefault("generationConfig", {})["temperature"] = body["temperature"]
    if "max_tokens" in body:
        gemini_payload.setdefault("generationConfig", {})["maxOutputTokens"] = body["max_tokens"]
    if "top_p" in body:
        gemini_payload.setdefault("generationConfig", {})["topP"] = body["top_p"]
    
    if body.get("tools"):
        gemini_tools, tool_config = convert_openai_tools_to_gemini(body["tools"], body.get("tool_choice"))
        if gemini_tools:
            gemini_payload["tools"] = gemini_tools
        if tool_config:
            gemini_payload["toolConfig"] = tool_config
        
    is_stream = body.get("stream", False)
    max_attempts = 4
    session_key = request.headers.get("x-session-id", str(uuid.uuid4()))
    
    last_error_resp = None
    last_status_code = 502
    
    path = "/v1internal:streamGenerateContent?alt=sse" if is_stream else "/v1internal:streamGenerateContent"
    url = f"https://{_CLOUDCODE_HOST}{path}"
    cloudcode_payload = {"project": _CLOUDCODE_PROJECT, "model": model_name, "request": gemini_payload}

    if not is_stream:
        async with httpx.AsyncClient(timeout=180.0, trust_env=False) as client:
            for attempt in range(max_attempts):
                acc_name, access_token = tm_instance.get_token_by_session(session_key, model_name=model_name)
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": "antigravity",
                }
                req = client.build_request("POST", url, json=cloudcode_payload, headers=headers)
                t0 = time.time()
                try:
                    resp = await client.send(req, stream=False)
                except Exception as e:
                    logger.error(f"Error calling Gemini: {e}")
                    return JSONResponse({"error": {"message": str(e), "type": "api_error", "param": None, "code": "502"}}, status_code=502)

                if resp.status_code >= 400:
                    resp_bytes = await resp.aread()
                    try:
                        err_data = json.loads(resp_bytes)
                        if isinstance(err_data, list) and err_data:
                            err_data = err_data[0]
                        last_error_resp = convert_gemini_error_to_openai(err_data)
                    except Exception:
                        last_error_resp = {"error": {"message": f"Upstream error {resp.status_code}", "type": "api_error", "param": None, "code": str(resp.status_code)}}
                    last_status_code = resp.status_code

                    if resp.status_code == 401 and attempt < max_attempts - 1:
                        tm_instance.refresh_account_token(acc_name)
                        await resp.aclose()
                        continue
                    if resp.status_code == 429 and attempt < max_attempts - 1:
                        tm_instance.mark_429(acc_name, cooldown_seconds=600)
                        await resp.aclose()
                        continue
                    if resp.status_code == 403 and attempt < max_attempts - 1:
                        tm_instance.mark_429(acc_name, cooldown_seconds=1800)
                        await resp.aclose()
                        continue

                    await resp.aclose()
                    return JSONResponse(last_error_resp, status_code=last_status_code)

                try:
                    resp_bytes = await resp.aread()
                    gemini_resp_array = json.loads(resp_bytes)

                    combined_parts: list = []
                    usage = {}
                    finish_reason = "STOP"
                    if isinstance(gemini_resp_array, list):
                        for item in gemini_resp_array:
                            resp_obj = item.get("response", {})
                            for cand in resp_obj.get("candidates", []):
                                for part in cand.get("content", {}).get("parts", []):
                                    if part.get("thought"):
                                        continue
                                    if "functionCall" in part or ("text" in part and part["text"]):
                                        combined_parts.append(part)
                                if "finishReason" in cand:
                                    finish_reason = cand["finishReason"]
                            if "usageMetadata" in resp_obj:
                                usage = resp_obj["usageMetadata"]
                    else:
                        combined_parts = gemini_resp_array.get("candidates", [{}])[0].get("content", {}).get("parts", [])

                    gemini_resp = {
                        "candidates": [{"content": {"parts": combined_parts or [{"text": ""}]}, "finishReason": finish_reason}],
                        "usageMetadata": usage,
                    }
                    openai_resp = convert_gemini_response_to_openai(gemini_resp, model_name)
                except Exception as e:
                    await resp.aclose()
                    return JSONResponse({"error": "Failed to parse Gemini response"}, status_code=502)

                token_usage = _extract_usage_metadata(resp_bytes, resp_bytes.decode("utf-8", "ignore"))
                broadcast_event({
                    "id": str(uuid.uuid4())[:8],
                    "time": time.strftime("%H:%M:%S"),
                    "method": "POST",
                    "path": path,
                    "host": _CLOUDCODE_HOST,
                    "account": acc_name,
                    "model": model_name,
                    "inbound_token": "OpenAI-Mapped",
                    "overridden_token": f"Bearer {access_token[:10]}...{access_token[-5:]}",
                    "status": resp.status_code,
                    "status_text": f"{resp.status_code} {resp.reason_phrase}",
                    "latency_ms": round((time.time() - t0) * 1000, 1),
                    "body": json.dumps(gemini_payload),
                    "body_size": len(json.dumps(gemini_payload)),
                    "response_preview": _format_body_for_log(resp_bytes, {}),
                    "response_size": len(resp_bytes),
                    "tokens": token_usage,
                    "retried": attempt > 0,
                })
                await resp.aclose()
                return JSONResponse(openai_resp)

        return JSONResponse({"error": "Failed after retries"}, status_code=502)

    # --- Streaming path: client lifetime managed inside the generator ---
    stream_acc_name = None
    stream_token = None
    stream_resp = None
    stream_client = None

    for attempt in range(max_attempts):
        stream_client = httpx.AsyncClient(timeout=180.0, trust_env=False)
        acc_name, access_token = tm_instance.get_token_by_session(session_key, model_name=model_name)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity",
        }
        req = stream_client.build_request("POST", url, json=cloudcode_payload, headers=headers)
        try:
            resp = await stream_client.send(req, stream=True)
        except Exception as e:
            await stream_client.aclose()
            logger.error(f"Error calling Gemini (stream): {e}")
            return JSONResponse({"error": {"message": str(e), "type": "api_error", "param": None, "code": "502"}}, status_code=502)

        if resp.status_code >= 400:
            resp_bytes = await resp.aread()
            try:
                err_data = json.loads(resp_bytes)
                if isinstance(err_data, list) and err_data:
                    err_data = err_data[0]
                last_error_resp = convert_gemini_error_to_openai(err_data)
            except Exception:
                last_error_resp = {"error": {"message": f"Upstream error {resp.status_code}", "type": "api_error", "param": None, "code": str(resp.status_code)}}
            last_status_code = resp.status_code
            await resp.aclose()
            await stream_client.aclose()

            if resp.status_code == 401 and attempt < max_attempts - 1:
                tm_instance.refresh_account_token(acc_name)
                continue
            if resp.status_code == 429 and attempt < max_attempts - 1:
                tm_instance.mark_429(acc_name, cooldown_seconds=600)
                continue
            if resp.status_code == 403 and attempt < max_attempts - 1:
                tm_instance.mark_429(acc_name, cooldown_seconds=1800)
                continue
            return JSONResponse(last_error_resp, status_code=last_status_code)

        stream_acc_name, stream_token, stream_resp, stream_client_ref = acc_name, access_token, resp, stream_client
        break
    else:
        if stream_client:
            await stream_client.aclose()
        return JSONResponse({"error": "Failed after retries"}, status_code=502)

    _client_ref = stream_client_ref
    _resp_ref = stream_resp
    _acc_ref = stream_acc_name
    _token_ref = stream_token

    async def generator():
        t0 = time.time()
        full_gemini_response = bytearray()

        async def tee_stream():
            async for chunk in _resp_ref.aiter_bytes():
                full_gemini_response.extend(chunk)
                yield chunk

        try:
            async for openai_chunk in stream_gemini_to_openai(tee_stream()):
                yield openai_chunk
        finally:
            latency_ms = round((time.time() - t0) * 1000, 1)
            await _resp_ref.aclose()
            await _client_ref.aclose()

            token_usage = _extract_usage_metadata(bytes(full_gemini_response), bytes(full_gemini_response).decode("utf-8", "ignore"))
            broadcast_event({
                "id": str(uuid.uuid4())[:8],
                "time": time.strftime("%H:%M:%S"),
                "method": "POST",
                "path": path,
                "host": _CLOUDCODE_HOST,
                "account": _acc_ref,
                "model": model_name,
                "inbound_token": "OpenAI-Mapped",
                "overridden_token": f"Bearer {_token_ref[:10]}...{_token_ref[-5:]}",
                "status": _resp_ref.status_code,
                "status_text": f"{_resp_ref.status_code} {_resp_ref.reason_phrase}",
                "latency_ms": latency_ms,
                "body": json.dumps(gemini_payload),
                "body_size": len(json.dumps(gemini_payload)),
                "response_preview": _format_body_for_log(bytes(full_gemini_response), {}),
                "response_size": len(full_gemini_response),
                "tokens": token_usage,
                "retried": False,
            })

    from fastapi.responses import StreamingResponse
    return StreamingResponse(generator(), media_type="text/event-stream")



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
    /* Account Section Header & Controls */
    .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 10px; }
    .section-title { font-size: 0.92rem; font-weight: 700; color: #fff; letter-spacing: -0.01em; display: flex; align-items: center; gap: 8px; font-family: var(--font-sans); }
    .section-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    
    .view-toggle { display: inline-flex; background: #0c0c0e; border: 1px solid var(--border); border-radius: 6px; padding: 2px; }
    .view-btn { background: transparent; border: none; color: var(--text-muted); padding: 4px 9px; font-size: 0.74rem; font-weight: 600; border-radius: 4px; cursor: pointer; transition: all 0.15s; display: inline-flex; align-items: center; gap: 5px; height: 26px; font-family: var(--font-sans); }
    .view-btn.active { background: #222228; color: #fff; }
    .view-btn:hover:not(.active) { color: #d4d4d8; }

    .acc-filter-tabs { display: inline-flex; gap: 4px; }
    .acc-tab-btn { background: transparent; border: 1px solid var(--border); color: var(--text-muted); padding: 4px 10px; font-size: 0.74rem; font-weight: 500; border-radius: 6px; cursor: pointer; transition: all 0.15s; height: 26px; display: inline-flex; align-items: center; gap: 5px; font-family: var(--font-mono); }
    .acc-tab-btn.active { background: #1c1c22; color: #fff; border-color: #38bdf8; font-weight: 600; }
    .acc-tab-btn:hover:not(.active) { background: #141418; color: #d4d4d8; }
    .tab-counter { background: rgba(255,255,255,0.08); padding: 1px 5px; border-radius: 10px; font-size: 0.68rem; }

    /* Account Grid View: Max 5 columns, 2 rows (10 accounts per page) */
    .grid { 
      display: grid; 
      grid-template-columns: repeat(5, minmax(0, 1fr)); 
      gap: 12px; 
      margin-bottom: 16px; 
    }
    @media (max-width: 1700px) {
      .grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    }
    @media (max-width: 1380px) {
      .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 1040px) {
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 680px) {
      .grid { grid-template-columns: 1fr; }
    }

    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 13px 14px; position: relative; transition: border-color 0.2s; }
    .card:hover { border-color: var(--border-hover); }
    .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; gap: 6px; }
    .acc-email { font-size: 0.82rem; font-weight: 600; color: #fff; font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .badge { font-size: 0.65rem; padding: 2px 6px; border-radius: 4px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; font-family: var(--font-mono); white-space: nowrap; flex-shrink: 0; }
    .badge.active { background: rgba(34, 197, 94, 0.08); color: var(--green); border: 1px solid rgba(34, 197, 94, 0.25); }
    .badge.sticky { background: rgba(56, 189, 248, 0.08); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.25); }
    .badge.cooldown { background: rgba(245, 158, 11, 0.08); color: var(--yellow); border: 1px solid rgba(245, 158, 11, 0.25); }
    .badge.danger { background: rgba(239, 68, 68, 0.08); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.25); }
    
    /* Quota Bars - Twin Inline Bar */
    .quota-twin-box { background: var(--sub-card); border: 1px solid rgba(255,255,255,0.06); border-radius: 6px; padding: 8px 10px; margin-top: 8px; display: flex; flex-direction: column; gap: 7px; }
    .twin-row { display: grid; grid-template-columns: 74px 1fr auto; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 0.72rem; }
    .twin-label { font-weight: 700; white-space: nowrap; font-size: 0.7rem; }
    .twin-bar-wrap { height: 5px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; min-width: 40px; }
    .twin-bar-fill { height: 100%; border-radius: 3px; transition: width 0.4s cubic-bezier(0.16, 1, 0.3, 1); }
    .twin-val { text-align: right; font-weight: 600; font-size: 0.72rem; white-space: nowrap; }
    .twin-val-sub { color: var(--text-muted); font-size: 0.65rem; font-weight: 400; margin-left: 3px; white-space: nowrap; }
    
    .meta-row { display: flex; justify-content: space-between; align-items: center; font-size: 0.72rem; color: var(--text-muted); margin-top: 10px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.04); font-family: var(--font-mono); }

    /* Account Pagination Bar */
    .account-pagination { display: flex; justify-content: space-between; align-items: center; padding: 10px 4px 20px 4px; font-size: 0.76rem; color: var(--text-muted); font-family: var(--font-mono); }
    .pagination-controls { display: inline-flex; align-items: center; gap: 6px; }
    .page-btn { background: #0c0c0e; border: 1px solid var(--border); color: #d4d4d8; padding: 4px 10px; border-radius: 6px; cursor: pointer; font-size: 0.74rem; font-family: var(--font-mono); transition: all 0.15s; }
    .page-btn:hover:not(:disabled) { background: #18181b; color: #fff; border-color: var(--border-hover); }
    .page-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    .page-info { font-weight: 600; color: #fff; margin: 0 4px; }
    
    /* Table Mode for Accounts */
    .account-table-container { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 24px; }
    .acc-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.8rem; }
    .acc-table th { color: var(--text-muted); padding: 10px 14px; border-bottom: 1px solid var(--border); background: #111114; font-weight: 600; font-family: var(--font-sans); }
    .acc-table td { padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; font-family: var(--font-mono); }
    .acc-table tr:hover { background: rgba(255, 255, 255, 0.02); }
    .bar-compact-wrap { display: flex; align-items: center; gap: 8px; min-width: 170px; }
    .bar-compact { flex: 1; height: 6px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; }
    .bar-compact-fill { height: 100%; border-radius: 3px; }
    .sub-tag { font-size: 0.68rem; color: var(--text-muted); display: block; margin-top: 2px; font-family: var(--font-mono); }
    .tooltip-anchor { cursor: help; border-bottom: 1px dotted rgba(255,255,255,0.2); }
    
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
      <button onclick="warmupAllAccounts(this)" id="btnWarmup" title="Activate 5H window on all accounts">
        <svg class="btn-svg" viewBox="0 0 24 24"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 3z"/></svg>
        <span>Activate All</span>
      </button>

      <!-- Sync Quotas Button -->
      <button onclick="syncAllQuotas(this)" class="primary" id="btnSyncQuotas" title="Update quotas from Google">
        <svg class="btn-svg" viewBox="0 0 24 24"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
        <span>Update Quotas</span>
      </button>

      <!-- Refresh Tokens Button -->
      <button onclick="refreshAllTokens(this)" id="btnRefreshTokens" title="Refresh Google tokens">
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

  <!-- KPI Strip - Modern Operational Metrics -->
  <div class="kpi-strip">
    <!-- Card 1: Pool Readiness -->
    <div class="kpi-card">
      <div class="kpi-label">Accounts Ready <span style="color:var(--green)">●</span></div>
      <div class="kpi-value" id="kpiPoolReady">-- / --</div>
      <div class="kpi-sub" id="kpiPoolSub">Loading...</div>
    </div>

    <!-- Card 2: Total Token Burn -->
    <div class="kpi-card">
      <div class="kpi-label">Tokens Used <span style="color:var(--accent)">●</span></div>
      <div class="kpi-value" id="kpiTokenBurn" style="color:var(--accent);">0</div>
      <div class="kpi-sub" id="kpiTokenSub">Input: 0 · Output: 0</div>
    </div>

    <!-- Card 3: Failover Resilience -->
    <div class="kpi-card">
      <div class="kpi-label">Stability <span style="color:var(--purple)">●</span></div>
      <div class="kpi-value" id="kpiResilience" style="color:var(--purple);">100%</div>
      <div class="kpi-sub" id="kpiResilienceSub">0 failovers · 0 errors</div>
    </div>

    <!-- Card 4: Traffic & Latency -->
    <div class="kpi-card">
      <div class="kpi-label">Traffic & Latency <span style="color:var(--text-muted)">●</span></div>
      <div class="kpi-value" id="kpiTrafficSpeed">0 <span style="font-size:0.85rem; font-weight:400; color:var(--text-muted);">reqs</span></div>
      <div class="kpi-sub" id="kpiSyncStatus">-- ms avg</div>
    </div>
  </div>

  <!-- Account Management Section Header -->
  <div class="section-header">
    <div class="section-title">
      <svg class="btn-svg" viewBox="0 0 24 24" style="color:var(--accent);"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
      <span>Accounts</span>
    </div>
    <div class="section-controls">
      <!-- Filter Tabs -->
      <div class="acc-filter-tabs">
        <button class="acc-tab-btn active" onclick="setAccountFilter('all', this)">All <span class="tab-counter" id="tabCountAll">0</span></button>
        <button class="acc-tab-btn" onclick="setAccountFilter('active', this)">Active <span class="tab-counter" id="tabCountActive">0</span></button>
        <button class="acc-tab-btn" onclick="setAccountFilter('exhausted', this)">Low / Exhausted <span class="tab-counter" id="tabCountExhausted">0</span></button>
      </div>

      <!-- View Mode Toggle: Grid vs Table -->
      <div class="view-toggle">
        <button class="view-btn active" id="btnViewGrid" onclick="setAccountView('grid')" title="Card Grid View">
          <svg class="btn-svg" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
          <span>Cards</span>
        </button>
        <button class="view-btn" id="btnViewTable" onclick="setAccountView('table')" title="Dense Data Table View">
          <svg class="btn-svg" viewBox="0 0 24 24"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
          <span>Table</span>
        </button>
      </div>
    </div>
  </div>

  <!-- Account Matrix (Grid View) -->
  <div class="grid" id="accountsGrid"></div>

  <!-- Pagination Bar for Accounts (Visible when items > 10) -->
  <div class="account-pagination" id="accountsPagination" style="display:none;">
    <div id="paginationSummary">Showing 1-10 of 20 accounts</div>
    <div class="pagination-controls">
      <button class="page-btn" id="btnPagePrev" onclick="changeAccountPage(-1)">← Prev</button>
      <span class="page-info" id="pageCurrentInfo">1 / 2</span>
      <button class="page-btn" id="btnPageNext" onclick="changeAccountPage(1)">Next →</button>
    </div>
  </div>

  <!-- Account Table View (Dense Data Table) -->
  <div class="account-table-container" id="accountsTableContainer" style="display:none;">
    <table class="acc-table">
      <thead>
        <tr>
          <th>Account / Profile</th>
          <th>Status</th>
          <th>Gemini Quota (5h / Wk)</th>
          <th>Claude & GPT Quota (5h / Wk)</th>
          <th style="text-align:right;">Expiry / Actions</th>
        </tr>
      </thead>
      <tbody id="accountsTableBody"></tbody>
    </table>
  </div>

  <!-- Telemetry Table View -->
  <div class="telemetry-container">
    <div class="telemetry-toolbar">
      <div class="filter-group">
        <span style="font-size:0.75rem; color:var(--text-muted); font-weight:700; margin-right:4px;">FILTER:</span>
        <button class="filter-btn active" onclick="setFilter('all', this)">All</button>
        <button class="filter-btn" onclick="setFilter('stream', this)">Streaming</button>
        <button class="filter-btn" onclick="setFilter('quota', this)">Quota Hit (429)</button>
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
            <th>Details</th>
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
          <button class="modal-tab-btn" id="tabBtnRaw" onclick="switchModalTab('tokens')">Token Flow</button>
        </div>

        <!-- Tab 1: Request -->
        <div id="modalTabRequest" class="modal-tab-content">
          <div style="font-size:0.72rem; text-transform:uppercase; color:var(--text-muted); font-weight:700; font-family:var(--font-mono); margin-bottom:6px;">
            Request Body:
          </div>
          <div class="json-container" id="modalJsonContainer"></div>
        </div>

        <!-- Tab 2: Response -->
        <div id="modalTabResponse" class="modal-tab-content" style="display:none;">
          <div style="font-size:0.72rem; text-transform:uppercase; color:var(--text-muted); font-weight:700; font-family:var(--font-mono); margin-bottom:6px;">
            Response:
          </div>
          <div class="json-container" id="modalRespContainer"></div>
        </div>

        <!-- Tab 3: Tokens Override -->
        <div id="modalTabTokens" class="modal-tab-content" style="display:none;">
          <div style="font-size:0.72rem; text-transform:uppercase; color:var(--text-muted); font-weight:700; font-family:var(--font-mono); margin-bottom:6px;">
            Token Flow:
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

    let accountsCache = {};
    let currentAccountFilter = 'all';
    let currentAccountView = localStorage.getItem('agy_account_view') || 'grid';
    let accountPage = 1;
    const ACCOUNTS_PER_PAGE = 10; // 2 rows x 5 columns

    function changeAccountPage(delta) {
      accountPage += delta;
      renderAccounts(accountsCache);
    }

    function setAccountView(viewMode) {
      currentAccountView = viewMode;
      localStorage.setItem('agy_account_view', viewMode);
      
      const btnGrid = document.getElementById('btnViewGrid');
      const btnTable = document.getElementById('btnViewTable');
      const gridContainer = document.getElementById('accountsGrid');
      const tableContainer = document.getElementById('accountsTableContainer');

      if (viewMode === 'table') {
        btnTable.classList.add('active');
        btnGrid.classList.remove('active');
        gridContainer.style.display = 'none';
        tableContainer.style.display = 'block';
      } else {
        btnGrid.classList.add('active');
        btnTable.classList.remove('active');
        gridContainer.style.display = 'grid';
        tableContainer.style.display = 'none';
      }
      renderAccounts(accountsCache);
    }

    function setAccountFilter(filterType, btn) {
      currentAccountFilter = filterType;
      accountPage = 1; // Reset to page 1 on filter switch
      document.querySelectorAll('.acc-tab-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      renderAccounts(accountsCache);
    }

    async function fetchStatus(btn = null) {
      const doFetch = async () => {
        const res = await fetch('/api/status');
        const data = await res.json();
        accountsCache = data.accounts || {};
        renderAccounts(accountsCache);
        updateKPIs(data);
      };
      if (btn) await handleActionWithButton(btn, doFetch);
      else await doFetch();
    }

    function formatTokenCount(num) {
      if (!num || num <= 0) return '0';
      if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
      if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
      return num.toLocaleString();
    }

    function updateKPIs(data) {
      const accs = Object.values(data.accounts || {});
      const total = accs.length;
      
      let healthyCount = 0;
      let partialCount = 0;
      let depletedCount = 0;

      accs.forEach(a => {
        const q = a.quota || {};
        const gW = (q.gemini && q.gemini['weekly']) || { percent: 100 };
        const cW = (q.third_party && q.third_party['weekly']) || { percent: 100 };
        const gExhausted = gW.disabled || (gW.percent <= 5.0 && Boolean(gW.reset_time));
        const cExhausted = cW.disabled || (cW.percent <= 5.0 && Boolean(cW.reset_time));

        if (!gExhausted && !cExhausted && a.is_active) healthyCount++;
        else if (gExhausted && cExhausted) depletedCount++;
        else partialCount++;
      });

      // 1. Pool Readiness
      document.getElementById('kpiPoolReady').innerText = `${healthyCount} / ${total} Ready`;
      document.getElementById('kpiPoolSub').innerText = `${partialCount} partial · ${depletedCount} depleted`;

      // 2. Token Burn
      const tok = data.tokens || { total: 0, prompt: 0, completion: 0 };
      document.getElementById('kpiTokenBurn').innerText = formatTokenCount(tok.total);
      document.getElementById('kpiTokenSub').innerText = `Input: ${formatTokenCount(tok.prompt)} · Output: ${formatTokenCount(tok.completion)}`;

      // 3. Router Resilience
      const succRate = data.success_rate !== undefined ? data.success_rate : 100;
      const failovers = data.failover_count || 0;
      document.getElementById('kpiResilience').innerText = `${succRate}%`;
      document.getElementById('kpiResilienceSub').innerText = `${failovers} auto-switches · 0 dropped`;

      // 4. Traffic & Latency
      const totalReqs = data.total_requests_intercepted || renderedIds.size;
      const avgLat = data.avg_latency_ms || 0;
      document.getElementById('kpiTrafficSpeed').innerHTML = `${totalReqs} <span style="font-size:0.85rem; font-weight:400; color:var(--text-muted);">reqs</span>`;
      document.getElementById('kpiSyncStatus').innerText = avgLat > 0 ? `${avgLat}ms avg · Real-time SSE` : 'Live Connected';
    }

    function calculateAccountMeta(name, acc) {
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
      let isExhaustedOrLow = false;

      if (acc.in_cooldown) {
        badgeClass = 'cooldown';
        badgeText = `COOLDOWN (${acc.cooldown_remaining_sec}s)`;
        isExhaustedOrLow = true;
      } else if (!isGeminiHealthy && !isClaudeHealthy) {
        badgeClass = 'danger';
        badgeText = 'ALL EXHAUSTED';
        isExhaustedOrLow = true;
      } else if (!isClaudeHealthy) {
        badgeClass = 'active';
        badgeText = 'GEMINI ONLY';
        isExhaustedOrLow = true;
      } else if (!isGeminiHealthy) {
        badgeClass = 'active';
        badgeText = 'CLAUDE ONLY';
        isExhaustedOrLow = true;
      }

      const c5hEffectivePct = cWeeklyExhausted ? 0 : c5h.percent;
      const g5hEffectivePct = gWeeklyExhausted ? 0 : g5h.percent;

      const emailDisplay = acc.email && acc.email.includes('@') ? acc.email : (acc.email || name);
      const nameDisplay = acc.display_name && acc.display_name !== name ? acc.display_name : '';

      return {
        name,
        acc,
        emailDisplay,
        nameDisplay,
        g5h, gWeekly, c5h, cWeekly,
        gWeeklyExhausted, cWeeklyExhausted,
        g5hEffectivePct, c5hEffectivePct,
        isGeminiHealthy, isClaudeHealthy,
        badgeClass, badgeText,
        isExhaustedOrLow
      };
    }

    function renderAccounts(accounts) {
      accountsCache = accounts || {};
      const entries = Object.entries(accountsCache);
      
      let countAll = entries.length;
      let countActive = 0;
      let countExhausted = 0;

      const items = entries.map(([name, acc]) => {
        const meta = calculateAccountMeta(name, acc);
        if (meta.isExhaustedOrLow) countExhausted++;
        else countActive++;
        return meta;
      });

      // Update counters
      const elAll = document.getElementById('tabCountAll');
      const elAct = document.getElementById('tabCountActive');
      const elExh = document.getElementById('tabCountExhausted');
      if (elAll) elAll.innerText = countAll;
      if (elAct) elAct.innerText = countActive;
      if (elExh) elExh.innerText = countExhausted;

      // Filter
      const filtered = items.filter(item => {
        if (currentAccountFilter === 'active') return !item.isExhaustedOrLow;
        if (currentAccountFilter === 'exhausted') return item.isExhaustedOrLow;
        return true;
      });

      // Pagination calculation (Max 10 accounts: 2 rows x 5 columns)
      const totalItems = filtered.length;
      const totalPages = Math.max(1, Math.ceil(totalItems / ACCOUNTS_PER_PAGE));
      if (accountPage > totalPages) accountPage = totalPages;
      if (accountPage < 1) accountPage = 1;

      const startIndex = (accountPage - 1) * ACCOUNTS_PER_PAGE;
      const paginatedItems = filtered.slice(startIndex, startIndex + ACCOUNTS_PER_PAGE);

      // Update Pagination UI
      const pagContainer = document.getElementById('accountsPagination');
      if (pagContainer) {
        if (totalItems > ACCOUNTS_PER_PAGE) {
          pagContainer.style.display = 'flex';
          const endDisplay = Math.min(startIndex + ACCOUNTS_PER_PAGE, totalItems);
          document.getElementById('paginationSummary').innerText = `Showing ${startIndex + 1}-${endDisplay} of ${totalItems} accounts`;
          document.getElementById('pageCurrentInfo').innerText = `${accountPage} / ${totalPages}`;
          document.getElementById('btnPagePrev').disabled = accountPage <= 1;
          document.getElementById('btnPageNext').disabled = accountPage >= totalPages;
        } else {
          pagContainer.style.display = 'none';
        }
      }

      if (currentAccountView === 'table') {
        renderAccountsTable(filtered); // Table can show all filtered or paginated
      } else {
        renderAccountsGrid(paginatedItems);
      }
    }

    function renderAccountsGrid(items) {
      const container = document.getElementById('accountsGrid');
      container.innerHTML = '';

      if (items.length === 0) {
        container.innerHTML = `<div style="grid-column: 1 / -1; padding: 36px; text-align: center; color: var(--text-muted); font-size: 0.85rem; border: 1px dashed var(--border); border-radius: 8px;">No accounts found for current filter.</div>`;
        return;
      }

      for (const item of items) {
        const { name, acc, emailDisplay, nameDisplay, badgeClass, badgeText, isGeminiHealthy, isClaudeHealthy, gWeekly, gWeeklyExhausted, g5h, g5hEffectivePct, cWeekly, cWeeklyExhausted, c5h, c5hEffectivePct } = item;
        const g5hColor = getBarColor(g5hEffectivePct, 'var(--accent)');
        const c5hColor = getBarColor(c5hEffectivePct, 'var(--purple)');
        const gWkTime = formatTimeUntil(gWeekly.reset_time);
        const cWkTime = formatTimeUntil(cWeekly.reset_time);
        const g5hTime = formatTimeUntil(g5h.reset_time);
        const c5hTime = formatTimeUntil(c5h.reset_time);

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
                ${nameDisplay ? `<span style="font-size: 0.74rem; color: var(--text-muted);">${nameDisplay}</span>` : ''}
              </div>
            </div>
            <div class="badge ${badgeClass}">${badgeText}</div>
          </div>

          <!-- Dual Inline Quota Box (Primary 5H Progress Bar) -->
          <div class="quota-twin-box">
            <!-- Row 1: Gemini -->
            <div class="twin-row" title="Gemini 5h: ${Number(g5hEffectivePct).toFixed(1)}% | Weekly: ${Number(gWeekly.percent).toFixed(1)}% (${gWkTime})">
              <span class="twin-label" style="color:var(--accent);">Gemini</span>
              <div class="twin-bar-wrap">
                <div class="twin-bar-fill" style="width: ${g5hEffectivePct}%; background: ${g5hColor};"></div>
              </div>
              <div class="twin-val" style="color:${g5hColor};">
                ${gWeeklyExhausted ? '<span style="color:var(--red);">0%</span>' : (g5h.percent <= 5.0 && g5h.reset_time ? '<span style="color:var(--red);">≤5%</span>' : Number(g5hEffectivePct).toFixed(0) + '%')}
                <span class="twin-val-sub" style="color:${gWeeklyExhausted ? 'var(--red)' : 'var(--text-muted)'};">(Wk: ${gWeeklyExhausted ? '≤5%' : Number(gWeekly.percent).toFixed(0) + '%'})</span>
              </div>
            </div>

            <!-- Row 2: Claude / GPT -->
            <div class="twin-row" title="Claude & GPT 5h: ${Number(c5hEffectivePct).toFixed(1)}% | Weekly: ${Number(cWeekly.percent).toFixed(1)}% (${cWkTime})">
              <span class="twin-label" style="color:var(--purple);">Claude/GPT</span>
              <div class="twin-bar-wrap">
                <div class="twin-bar-fill" style="width: ${c5hEffectivePct}%; background: ${c5hColor};"></div>
              </div>
              <div class="twin-val" style="color:${c5hColor};">
                ${cWeeklyExhausted ? '<span style="color:var(--red);">0%</span>' : (c5h.percent <= 5.0 && c5h.reset_time ? '<span style="color:var(--red);">≤5%</span>' : Number(c5hEffectivePct).toFixed(0) + '%')}
                <span class="twin-val-sub" style="color:${cWeeklyExhausted ? 'var(--red)' : 'var(--text-muted)'};">(Wk: ${cWeeklyExhausted ? '≤5%' : Number(cWeekly.percent).toFixed(0) + '%'})</span>
              </div>
            </div>
          </div>

          <!-- Bottom Footer Row: Exp & Actions -->
          <div class="meta-row">
            <span>Exp: <b>${acc.token_expires_in_min}m</b></span>
            <div style="display:flex; gap:6px;">
              <button onclick="warmupSingle('${name}', this)" class="card-btn" title="Activate 5H for ${name}">
                <svg class="btn-svg" viewBox="0 0 24 24"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 3z"/></svg>
                <span>Activate</span>
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

    function renderAccountsTable(items) {
      const tbody = document.getElementById('accountsTableBody');
      tbody.innerHTML = '';

      if (items.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:36px; color:var(--text-muted);">No accounts found for current filter.</td></tr>`;
        return;
      }

      for (const item of items) {
        const { name, acc, emailDisplay, nameDisplay, badgeClass, badgeText, gWeekly, gWeeklyExhausted, g5h, g5hEffectivePct, cWeekly, cWeeklyExhausted, c5h, c5hEffectivePct } = item;
        const tr = document.createElement('tr');
        
        const g5hColor = getBarColor(g5hEffectivePct, 'var(--accent)');
        const c5hColor = getBarColor(c5hEffectivePct, 'var(--purple)');
        const gWkTime = formatTimeUntil(gWeekly.reset_time);
        const cWkTime = formatTimeUntil(cWeekly.reset_time);

        tr.innerHTML = `
          <td>
            <div style="font-weight:600; color:#fff;">${emailDisplay}</div>
            <div style="display:flex; align-items:center; gap:6px; margin-top:3px;">
              <span class="code-chip" style="color:var(--accent); font-size:0.7rem;">${name}</span>
              ${nameDisplay ? `<span style="font-size:0.72rem; color:var(--text-muted);">${nameDisplay}</span>` : ''}
            </div>
          </td>
          <td>
            <span class="badge ${badgeClass}">${badgeText}</span>
          </td>
          <td>
            <div class="bar-compact-wrap" title="5h: ${Number(g5hEffectivePct).toFixed(1)}% | Weekly: ${Number(gWeekly.percent).toFixed(1)}%">
              <div class="bar-compact">
                <div class="bar-compact-fill" style="width:${g5hEffectivePct}%; background:${g5hColor};"></div>
              </div>
              <span style="font-weight:600; font-size:0.76rem; width:46px; text-align:right;">${Number(g5hEffectivePct).toFixed(1)}%</span>
            </div>
            <span class="sub-tag">Wk: <b>${gWeeklyExhausted ? 'Exhausted' : Number(gWeekly.percent).toFixed(1) + '%'}</b> · <span class="tooltip-anchor" title="${gWkTime}">${gWkTime}</span></span>
          </td>
          <td>
            <div class="bar-compact-wrap" title="5h: ${Number(c5hEffectivePct).toFixed(1)}% | Weekly: ${Number(cWeekly.percent).toFixed(1)}%">
              <div class="bar-compact">
                <div class="bar-compact-fill" style="width:${c5hEffectivePct}%; background:${c5hColor};"></div>
              </div>
              <span style="font-weight:600; font-size:0.76rem; width:46px; text-align:right;">${cWeeklyExhausted ? '0.0%' : Number(c5hEffectivePct).toFixed(1) + '%'}</span>
            </div>
            <span class="sub-tag">Wk: <b>${cWeeklyExhausted ? 'Exhausted' : Number(cWeekly.percent).toFixed(1) + '%'}</b> · <span class="tooltip-anchor" title="${cWkTime}">${cWkTime}</span></span>
          </td>
          <td style="text-align:right;">
            <div style="display:inline-flex; align-items:center; gap:10px;">
              <span class="sub-tag" style="margin:0;">Exp: <b>${acc.token_expires_in_min}m</b></span>
              <button onclick="refreshSingle('${name}', this)" class="card-btn" title="Refresh Token">
                <svg class="btn-svg" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                <span>Refresh</span>
              </button>
            </div>
          </td>
        `;
        tbody.appendChild(tr);
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
          showToast(`Activation error: ${e.message}`, 'error');
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
          showToast('Tokens refreshed successfully', 'success');
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
          showToast('Quotas updated successfully', 'success');
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

      let tokenBadge = '';
      if (evt.tokens && evt.tokens.total) {
        tokenBadge = ` <span class="code-chip" style="color:var(--accent); font-weight:700; border-color:rgba(59,130,246,0.3); background:rgba(59,130,246,0.1); margin-left:4px;">${formatTokenCount(evt.tokens.total)} tok</span>`;
      }

      row.innerHTML = `
        <td style="color:var(--text-muted); font-size:0.75rem;">${evt.time}</td>
        <td><span class="pill ${methodClass}">${evt.method}</span></td>
        <td><span class="code-chip" style="color:var(--accent); font-weight:600;">${modelLabel}</span></td>
        <td><span class="code-chip" title="${evt.path}">${evt.path.length > 28 ? evt.path.substring(0,28)+'...' : evt.path}</span></td>
        <td><b style="color:#fff;">${evt.account}</b></td>
        <td><b style="color: ${statusColor};">${evt.status_text || evt.status}</b></td>
        <td style="color:var(--text-muted);">${evt.latency_ms}ms</td>
        <td style="white-space:nowrap;">
          <button class="btn-inspect" onclick="openPayloadModal('${evt.id}')">
            <svg class="btn-svg" style="width:13px; height:13px;" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            <span>Inspect (${sizeLabel})</span>
          </button>
          ${tokenBadge}
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

      let tokenMeta = '';
      if (evt.tokens && evt.tokens.total) {
        tokenMeta = ` &bull; Tokens: <b style="color:var(--accent);">${evt.tokens.total.toLocaleString()}</b> (Prompt: ${evt.tokens.prompt.toLocaleString()} · Output: ${evt.tokens.completion.toLocaleString()})`;
      }

      document.getElementById('modalMeta').innerHTML = `
        Account: <b style="color:var(--accent);">${evt.account}</b> &bull; 
        Status: <b style="color:${statusColor}">${evt.status_text || evt.status}</b> &bull; 
        Latency: <b>${evt.latency_ms}ms</b>${tokenMeta} &bull; Time: <b>${evt.time}</b>
      `;
      document.getElementById('modalHeaderFlow').innerHTML = `
        <div style="margin-bottom:6px;"><span style="color:var(--text-muted);">Your Token:</span> <span class="code-chip">${evt.inbound_token}</span></div>
        <div style="margin-bottom:6px;"><span style="color:var(--accent);">➔ Selected Account:</span> <b style="color:#fff;">${evt.account}</b></div>
        <div><span style="color:var(--accent);">➔ Google Token:</span> <span class="code-chip">${evt.overridden_token}</span></div>
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
          document.getElementById('kpiSyncStatus').innerText = 'Reconnecting...';
        };
        evtSource.onopen = () => {
          document.getElementById('kpiSyncStatus').innerText = 'Live Connected';
        };
      } catch (err) {
        console.warn("SSE init error:", err);
      }
    }

    setAccountView(currentAccountView);
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
