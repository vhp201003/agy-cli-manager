from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import sys
import time
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

_current = Path(__file__).resolve().parent
_parent = _current.parent
for _p in (str(_current), str(_parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uuid
import httpx

try:
    from agy_cli_manager.proxy.scheduler import WarmupScheduler
    from agy_cli_manager.proxy.token_manager import TokenManager
    from agy_cli_manager.proxy.db import get_telemetry_db
    from agy_cli_manager.proxy.proxy_server import (
        REQUEST_LOGS,
        register_event_listener,
        unregister_event_listener,
        broadcast_event,
        _extract_usage_metadata,
        _format_body_for_log,
    )
    from agy_cli_manager.proxy.translators.openai import (
        convert_openai_messages_to_gemini,
        convert_openai_tools_to_gemini,
        convert_gemini_response_to_openai,
        convert_gemini_error_to_openai,
    )
    from agy_cli_manager.proxy.translators.streaming import stream_gemini_to_openai
except ImportError:
    try:
        from .scheduler import WarmupScheduler
        from .token_manager import TokenManager
        from .db import get_telemetry_db
        from .proxy_server import (
            REQUEST_LOGS,
            register_event_listener,
            unregister_event_listener,
            broadcast_event,
            _extract_usage_metadata,
            _format_body_for_log,
        )
        from .translators.openai import (
            convert_openai_messages_to_gemini,
            convert_openai_tools_to_gemini,
            convert_gemini_response_to_openai,
            convert_gemini_error_to_openai,
        )
        from .translators.streaming import stream_gemini_to_openai
    except ImportError:
        try:
            from scheduler import WarmupScheduler
        except ImportError:
            WarmupScheduler = None
        from token_manager import TokenManager
        from db import get_telemetry_db
        from proxy_server import (
            REQUEST_LOGS,
            register_event_listener,
            unregister_event_listener,
            broadcast_event,
            _extract_usage_metadata,
            _format_body_for_log,
        )
        from translators.openai import (
            convert_openai_messages_to_gemini,
            convert_openai_tools_to_gemini,
            convert_gemini_response_to_openai,
            convert_gemini_error_to_openai,
        )
        from translators.streaming import stream_gemini_to_openai

logger = logging.getLogger("AgyProxy.FastAPI")

tm_instance = TokenManager()
active_subscribers: list[asyncio.Queue] = []
main_loop: asyncio.AbstractEventLoop | None = None


def warmup_event_broadcaster(event: dict) -> None:
    global main_loop
    if main_loop and main_loop.is_running():
        for q in list(active_subscribers):
            main_loop.call_soon_threadsafe(q.put_nowait, event)


warmup_scheduler = (
    WarmupScheduler(tm_instance, tm_instance.manager_root, on_event=warmup_event_broadcaster)
    if WarmupScheduler is not None
    else None
)


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

    telemetry_db = get_telemetry_db()
    telemetry_db.start()
    try:
        await asyncio.to_thread(telemetry_db.prune_retention)
    except Exception:
        pass

    cron_task = asyncio.create_task(cron_token_refresh_task(interval_seconds=600))
    warmup_task = (
        asyncio.create_task(warmup_scheduler.run_loop())
        if warmup_scheduler is not None
        else None
    )
    try:
        yield
    finally:
        if warmup_scheduler is not None:
            await warmup_scheduler.stop()
        if warmup_task is not None:
            warmup_task.cancel()
            try:
                await warmup_task
            except (asyncio.CancelledError, Exception):
                pass
        cron_task.cancel()
        unregister_event_listener(proxy_event_listener)
        telemetry_db.flush()
        telemetry_db.close()


app = FastAPI(
    title="AGY Multi-Account Router & Live Proxy",
    description="Real-time Inspection, Header Interception, and Dual Quota Tracking for Antigravity CLI",
    version="2.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ca.crt")
async def get_ca_certificate():
    from agy_cli_manager.proxy.cert_manager import CertManager
    cm = CertManager()
    if cm.ca_crt_path.is_file():
        return Response(
            content=cm.ca_crt_path.read_text(encoding="utf-8"),
            media_type="application/x-x509-ca-cert",
            headers={"Content-Disposition": 'attachment; filename="ca.crt"'},
        )
    return JSONResponse(status_code=404, content={"error": "CA certificate not found"})


FAVICON_SVG = """<svg width="32" height="32" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" shape-rendering="geometricPrecision">
  <defs>
    <clipPath id="delta-clip">
      <polygon points="8,2 8,14 14,14"/>
    </clipPath>
  </defs>
  <polygon points="8,2 2,14 8,14" fill="#EDEDED"/>
  <polygon points="8,2 8,14 14,14" fill="#171717" stroke="rgba(255,255,255,0.25)" stroke-width="1"/>
  <rect x="11" y="11" width="3" height="3" fill="#0070F3" clip-path="url(#delta-clip)"/>
</svg>"""


@app.get("/favicon.svg")
@app.get("/favicon.ico")
async def favicon():
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.get("/api/status")
async def get_status():
    status = tm_instance.get_status()
    total_accs = len(status)
    active_accs = sum(1 for a in status.values() if a["is_active"])

    telemetry_db = get_telemetry_db()
    stats = await asyncio.to_thread(telemetry_db.query_aggregate_stats)

    return {
        "pool_size": total_accs,
        "available_accounts": active_accs,
        "proxy_port": 8899,
        "accounts": status,
        "total_requests_intercepted": stats["total_requests"],
        "failover_count": stats["failover_count"],
        "success_rate": stats["success_rate"],
        "tokens": {
            "total": stats["total_tokens"],
            "prompt": stats["prompt_tokens"],
            "completion": stats["completion_tokens"],
        },
        "avg_latency_ms": stats["avg_latency_ms"],
        "timestamp": time.time(),
    }


@app.get("/api/logs")
async def get_logs(
    limit: int = 100,
    offset: int = 0,
    account: str | None = None,
    model: str | None = None,
    status: int | None = None,
):
    telemetry_db = get_telemetry_db()
    logs, _ = await asyncio.to_thread(
        telemetry_db.query_logs,
        limit=limit,
        offset=offset,
        account=account,
        model=model,
        status=status,
    )
    return logs


@app.get("/api/analytics/tokens")
async def get_token_analytics(range_days: int = Query(default=7, ge=1, le=90)):
    telemetry_db = get_telemetry_db()
    return await asyncio.to_thread(telemetry_db.query_token_analytics, range_days=range_days)


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


@app.get("/api/schedule/warmup")
async def get_warmup_schedule():
    if warmup_scheduler is None:
        return JSONResponse({"error": "WarmupScheduler not initialized"}, status_code=503)
    return JSONResponse(warmup_scheduler.get_status())


@app.post("/api/schedule/warmup")
async def update_warmup_schedule(request: Request):
    if warmup_scheduler is None:
        return JSONResponse({"error": "WarmupScheduler not initialized"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

    enabled = body.get("enabled")
    times = body.get("times")
    max_retries = body.get("max_retries")

    kwargs = {}
    if enabled is not None:
        kwargs["enabled"] = bool(enabled)
    if times is not None:
        if isinstance(times, list):
            kwargs["times"] = [str(t).strip() for t in times if str(t).strip()]
        elif isinstance(times, str):
            kwargs["times"] = [t.strip() for t in times.split(",") if t.strip()]
    if max_retries is not None:
        try:
            kwargs["max_retries"] = int(max_retries)
        except (ValueError, TypeError):
            pass

    updated = warmup_scheduler.update_config(**kwargs)
    status = updated if isinstance(updated, dict) else warmup_scheduler.get_status()
    return JSONResponse(status)


@app.post("/api/schedule/warmup/trigger")
async def trigger_warmup_schedule():
    if warmup_scheduler is None:
        return JSONResponse({"error": "WarmupScheduler not initialized"}, status_code=503)
    results = await asyncio.to_thread(warmup_scheduler.trigger_manual)
    return JSONResponse({
        "message": "Manual warmup cycle triggered",
        "results": results,
        "status": warmup_scheduler.get_status(),
    })


@app.post("/api/accounts/{name}/warmup")
async def warmup_account(name: str, model: str | None = None):
    ok, msg = tm_instance.warmup_account(name, model=model)
    tm_instance.fetch_account_quota(name)
    return {"success": ok, "message": msg, "account": tm_instance.get_status().get(name)}


@app.post("/api/accounts/{name}/clear-cooldown")
async def clear_cooldown(name: str):
    if name not in tm_instance.get_account_names():
        return JSONResponse({"error": "Account not found"}, status_code=404)
    tm_instance.clear_cooldown(name)
    return {"message": f"Cleared cooldown for {name}", "status": tm_instance.get_status().get(name)}


@app.post("/api/accounts/{name}/clear-quarantine")
async def clear_quarantine(name: str):
    if name not in tm_instance.get_account_names():
        return JSONResponse({"error": "Account not found"}, status_code=404)
    tm_instance.clear_quarantine(name)
    return {"message": f"Cleared quarantine for {name}", "status": tm_instance.get_status().get(name)}


@app.post("/api/accounts/{name}/quarantine")
async def quarantine_account(name: str, request: Request):
    if name not in tm_instance.get_account_names():
        return JSONResponse({"error": "Account not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str(body.get("reason", "manual_quarantine"))
    try:
        duration = int(body.get("duration_seconds", 1800))
    except (ValueError, TypeError):
        duration = 1800
    duration = max(60, min(604800, duration))
    tm_instance.quarantine_account(name, reason=reason, duration_seconds=duration)
    return {"message": f"Quarantined {name} for {duration}s", "status": tm_instance.get_status().get(name)}




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


_SUPPORTED_MODELS = [
    {"id": "gemini-3.8-flash", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.8-flash-high", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.8-flash-medium", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.8-flash-low", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "claude-sonnet-4-6", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "claude-opus-4-6-thinking", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "gpt-oss-120b-medium", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gemini-2.5-flash", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-2.5-pro", "object": "model", "created": 1700000000, "owned_by": "google"},
]

_MODEL_MAP = {
    "gemini-3.8-flash": "gemini-2.5-flash",
    "gemini-3.8-flash-high": "gemini-2.5-flash",
    "gemini-3.8-flash-medium": "gemini-2.5-flash",
    "gemini-3.8-flash-low": "gemini-2.5-flash",
    "gemini-1.5-pro-latest": "gemini-2.5-flash",
    "gemini-1.5-pro": "gemini-2.5-flash",
    "gemini-1.5-flash-latest": "gemini-2.5-flash",
    "gemini-1.5-flash": "gemini-2.5-flash",
    "gemini-1.0-pro": "gemini-2.5-flash",
    "gemini-pro": "gemini-2.5-flash",
    "gemini-pro-latest": "gemini-2.5-flash",
    "claude-3-7-sonnet": "claude-sonnet-4-6",
    "claude-3-5-sonnet": "claude-sonnet-4-6",
}

_CLOUDCODE_HOST = "daily-cloudcode-pa.googleapis.com"
_CLOUDCODE_PROJECT = "aicode-consumers"

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": _SUPPORTED_MODELS,
    }

@app.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    for m in _SUPPORTED_MODELS:
        if m["id"] == model_id:
            return m
    return JSONResponse(
        {"error": {"message": f"Model '{model_id}' not found", "type": "invalid_request_error"}},
        status_code=404,
    )

def resolve_model_name(requested: str | None) -> str:
    target = (requested or "gemini-2.5-flash").replace("-latest", "")
    m = _MODEL_MAP.get(target, target)
    valid_prefixes = ("gemini", "claude", "gpt")
    if not any(m.lower().startswith(p) for p in valid_prefixes):
        return "gemini-2.5-flash"
    return m

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        
    requested_model = body.get("model") or "gemini-2.5-flash"
    upstream_model = resolve_model_name(requested_model)
        
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

    if upstream_model.startswith("gemini"):
        effort = body.get("reasoning_effort")
        if not effort:
            if requested_model.endswith("-low"):
                effort = "low"
            elif requested_model.endswith("-medium"):
                effort = "medium"
            elif requested_model.endswith("-high"):
                effort = "high"

        if effort:
            effort_str = str(effort).lower()
            budget_map = {
                "none": 0,
                "off": 0,
                "minimal": 1024,
                "low": 1024,
                "medium": 8192,
                "high": 24576,
                "xhigh": 24576,
                "max": 24576,
            }
            budget = budget_map.get(effort_str)
            if budget is not None:
                gemini_payload.setdefault("generationConfig", {})["thinkingConfig"] = {"thinkingBudget": budget}
    
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
    cloudcode_payload = {"project": _CLOUDCODE_PROJECT, "model": upstream_model, "request": gemini_payload}

    if not is_stream:
        async with httpx.AsyncClient(timeout=180.0, trust_env=False) as client:
            for attempt in range(max_attempts):
                acc_name, access_token = tm_instance.get_token_by_session(session_key, model_name=upstream_model)
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

                    if resp.status_code == 401:
                        refreshed, ref_msg = tm_instance.refresh_account_token(acc_name)
                        if not refreshed:
                            tm_instance.quarantine_account(acc_name, reason=f"auth_refresh_failed: {ref_msg}", duration_seconds=1800)
                        if attempt < max_attempts - 1:
                            await resp.aclose()
                            continue
                    if resp.status_code == 429:
                        tm_instance.mark_429(acc_name, cooldown_seconds=600)
                        if attempt < max_attempts - 1:
                            await resp.aclose()
                            continue
                    if resp.status_code == 403:
                        tm_instance.quarantine_account(acc_name, reason="forbidden_403", duration_seconds=1800)
                        if attempt < max_attempts - 1:
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
                                    if "functionCall" in part or "text" in part:
                                        combined_parts.append(part)
                                if "finishReason" in cand:
                                    finish_reason = cand["finishReason"]
                            if "usageMetadata" in resp_obj:
                                usage = resp_obj["usageMetadata"]
                    else:
                        resp_dict = gemini_resp_array.get("response", gemini_resp_array) if isinstance(gemini_resp_array, dict) else {}
                        candidates = resp_dict.get("candidates", [])
                        if candidates:
                            combined_parts = candidates[0].get("content", {}).get("parts", [])
                            if "finishReason" in candidates[0]:
                                finish_reason = candidates[0]["finishReason"]
                        if "usageMetadata" in resp_dict:
                            usage = resp_dict["usageMetadata"]

                    gemini_resp = {
                        "candidates": [{"content": {"parts": combined_parts or [{"text": ""}]}, "finishReason": finish_reason}],
                        "usageMetadata": usage,
                    }
                    openai_resp = convert_gemini_response_to_openai(gemini_resp, requested_model)
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
                    "model": requested_model,
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
        acc_name, access_token = tm_instance.get_token_by_session(session_key, model_name=upstream_model)
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

            if resp.status_code == 401:
                refreshed, ref_msg = tm_instance.refresh_account_token(acc_name)
                if not refreshed:
                    tm_instance.quarantine_account(acc_name, reason=f"auth_refresh_failed: {ref_msg}", duration_seconds=1800)
                if attempt < max_attempts - 1:
                    continue
            if resp.status_code == 429:
                tm_instance.mark_429(acc_name, cooldown_seconds=600)
                if attempt < max_attempts - 1:
                    continue
            if resp.status_code == 403:
                tm_instance.quarantine_account(acc_name, reason="forbidden_403", duration_seconds=1800)
                if attempt < max_attempts - 1:
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
            async for openai_chunk in stream_gemini_to_openai(tee_stream(), model_name=requested_model):
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
                "model": requested_model,
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

    return StreamingResponse(generator(), media_type="text/event-stream")



DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AGY Multi-Account Router & Quota Telemetry</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
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
    .brand-wrap { display: flex; align-items: center; gap: 12px; }
    .header-logo { flex-shrink: 0; }
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

    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 11px 13px; position: relative; transition: border-color 0.2s; }
    .card:hover { border-color: var(--border-hover); }
    .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 7px; gap: 6px; }
    .acc-email { font-size: 0.82rem; font-weight: 600; color: #fff; font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .badge { font-size: 0.65rem; padding: 2px 6px; border-radius: 4px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; font-family: var(--font-mono); white-space: nowrap; flex-shrink: 0; }
    .badge.active { background: rgba(34, 197, 94, 0.08); color: var(--green); border: 1px solid rgba(34, 197, 94, 0.25); }
    .badge.sticky { background: rgba(56, 189, 248, 0.08); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.25); }
    .badge.cooldown { background: rgba(245, 158, 11, 0.08); color: var(--yellow); border: 1px solid rgba(245, 158, 11, 0.25); }
    .badge.danger { background: rgba(239, 68, 68, 0.08); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.25); }
    
    /* Concentric Radial Quota System - Layout B */
    .quota-radial-box {
      background: var(--sub-card);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 6px;
      padding: 7px 9px;
      margin-top: 6px;
      display: flex;
      flex-direction: column;
      gap: 7px;
    }
    .radial-row {
      display: flex;
      align-items: center;
      gap: 9px;
    }
    .radial-gauge-wrap {
      width: 48px;
      height: 48px;
      position: relative;
      flex-shrink: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: help;
    }
    .radial-gauge-wrap svg {
      width: 48px;
      height: 48px;
      transform: rotate(-90deg);
      display: block;
    }
    .radial-center-val {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.67rem;
      font-weight: 700;
      font-family: var(--font-mono);
      letter-spacing: -0.03em;
      pointer-events: none;
    }
    .radial-info-col {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 2px;
      font-family: var(--font-mono);
    }
    .radial-title-row {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      font-size: 0.68rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .radial-metric-line {
      font-size: 0.67rem;
      color: #d4d4d8;
      display: flex;
      align-items: center;
      gap: 4px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      line-height: 1.25;
    }
    .radial-metric-tag {
      font-size: 0.62rem;
      font-weight: 600;
      width: 19px;
      flex-shrink: 0;
    }
    .radial-metric-val {
      font-weight: 600;
    }
    .radial-metric-sub {
      color: var(--text-muted);
      font-size: 0.63rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .radial-legend-hint {
      font-size: 0.59rem;
      color: #52525b;
      font-family: var(--font-mono);
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 5px;
      padding-top: 2px;
      border-top: 1px dashed rgba(255, 255, 255, 0.04);
    }
    .radial-legend-dot {
      width: 4px;
      height: 4px;
      border-radius: 50%;
      display: inline-block;
    }
    
    .meta-row { display: flex; justify-content: space-between; align-items: center; font-size: 0.72rem; color: var(--text-muted); margin-top: 7px; padding-top: 6px; border-top: 1px solid rgba(255,255,255,0.04); font-family: var(--font-mono); }

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

    /* Daily Token Analytics Component */
    .analytics-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 24px; overflow: hidden; transition: border-color 0.2s; }
    .analytics-card:hover { border-color: var(--border-hover); }
    .analytics-header { padding: 12px 18px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; background: #111114; }
    .analytics-title { font-size: 0.92rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px; font-family: var(--font-sans); }
    .analytics-badge { font-size: 0.68rem; background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.25); color: var(--accent); padding: 2px 7px; border-radius: 12px; font-family: var(--font-mono); font-weight: 600; }
    .analytics-controls { display: flex; align-items: center; gap: 8px; }
    .range-selector { display: inline-flex; background: #08080a; border: 1px solid var(--border); border-radius: 6px; padding: 2px; }
    .range-btn { background: transparent; border: none; color: var(--text-muted); padding: 4px 10px; font-size: 0.74rem; font-weight: 600; border-radius: 4px; cursor: pointer; transition: all 0.15s; font-family: var(--font-mono); height: 26px; }
    .range-btn.active { background: #222228; color: #fff; }
    .range-btn:hover:not(.active) { color: #d4d4d8; }
    .collapse-btn { background: #0c0c0e; border: 1px solid var(--border); color: var(--text-muted); width: 28px; height: 28px; padding: 0; border-radius: 6px; display: inline-flex; align-items: center; justify-content: center; cursor: pointer; transition: all 0.15s; }
    .collapse-btn:hover { background: #18181b; color: #fff; }
    .collapse-btn svg { transition: transform 0.2s ease; }
    .collapse-btn.collapsed svg { transform: rotate(-90deg); }

    .analytics-body { padding: 16px 18px; display: flex; flex-direction: column; gap: 16px; }
    .analytics-body.collapsed { display: none; }

    .chart-container { background: #08080a; border: 1px solid rgba(255, 255, 255, 0.04); border-radius: 8px; padding: 14px 16px; display: flex; flex-direction: column; gap: 14px; }
    .chart-legend { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 0.74rem; font-family: var(--font-mono); color: var(--text-muted); }
    .legend-items { display: flex; gap: 16px; align-items: center; }
    .legend-item { display: flex; align-items: center; gap: 6px; }
    .legend-dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
    .legend-dot.prompt { background: var(--accent); }
    .legend-dot.completion { background: var(--purple); }

    .bars-wrapper { display: flex; align-items: flex-end; gap: 10px; height: 140px; padding: 10px 4px 0 4px; border-bottom: 1px solid var(--border); position: relative; overflow-x: auto; }
    .bar-col { flex: 1; min-width: 24px; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; position: relative; cursor: pointer; }
    .bar-col:hover .bar-stack { filter: brightness(1.2); }
    .bar-col:hover .bar-tooltip { opacity: 1; visibility: visible; transform: translateX(-50%) translateY(-8px); }
    .bar-val-top { font-size: 0.65rem; font-family: var(--font-mono); color: var(--text-muted); margin-bottom: 4px; white-space: nowrap; font-weight: 500; }
    .bar-stack { width: 100%; max-width: 38px; background: rgba(255, 255, 255, 0.03); border-radius: 4px 4px 0 0; overflow: hidden; display: flex; flex-direction: column-reverse; transition: height 0.3s cubic-bezier(0.16, 1, 0.3, 1), filter 0.15s; min-height: 2px; }
    .bar-seg-prompt { background: var(--accent); width: 100%; transition: height 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
    .bar-seg-completion { background: var(--purple); width: 100%; transition: height 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
    .bar-label-bot { font-size: 0.68rem; font-family: var(--font-mono); color: var(--text-muted); margin-top: 6px; white-space: nowrap; }
    .bar-label-bot.is-today { color: #fff; font-weight: 700; }

    .bar-tooltip { position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%) translateY(0); background: #141418; border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; font-size: 0.72rem; font-family: var(--font-mono); white-space: nowrap; z-index: 50; opacity: 0; visibility: hidden; pointer-events: none; transition: opacity 0.15s ease, transform 0.15s ease; box-shadow: 0 10px 25px rgba(0, 0, 0, 0.6); display: flex; flex-direction: column; gap: 3px; }
    .bar-tooltip-title { font-weight: 700; color: #fff; border-bottom: 1px solid rgba(255, 255, 255, 0.08); padding-bottom: 3px; margin-bottom: 2px; }

    .daily-table-wrap { overflow-x: auto; border-radius: 6px; border: 1px solid rgba(255, 255, 255, 0.05); }
    .daily-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.78rem; }
    .daily-table th { color: var(--text-muted); padding: 8px 12px; border-bottom: 1px solid var(--border); background: #0f0f12; font-weight: 600; font-family: var(--font-sans); }
    .daily-table td { padding: 8px 12px; border-bottom: 1px solid rgba(255, 255, 255, 0.04); vertical-align: middle; font-family: var(--font-mono); }
    .daily-table tr:hover { background: rgba(255, 255, 255, 0.02); }

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
    /* Vercel Geist Schedule Action Indicator */
    .schedule-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #71717a;
      flex-shrink: 0;
      transition: background 0.2s ease, box-shadow 0.2s ease;
      display: inline-block;
    }
    .schedule-dot.enabled {
      background: #10b981;
      box-shadow: 0 0 6px rgba(16, 185, 129, 0.5);
    }
    .schedule-dot.retrying {
      background: #f59e0b;
      box-shadow: 0 0 6px rgba(245, 166, 35, 0.5);
      animation: pulse 1.2s infinite;
    }
    .schedule-dot.disabled {
      background: #71717a;
    }
    .toggle-slider {
      position: absolute;
      cursor: pointer;
      top: 0; left: 0; right: 0; bottom: 0;
      background-color: #27272a;
      transition: .2s;
      border-radius: 22px;
      border: 1px solid var(--border);
    }
    .toggle-slider:before {
      position: absolute;
      content: "";
      height: 14px;
      width: 14px;
      left: 3px;
      bottom: 3px;
      background-color: #fff;
      transition: .2s;
      border-radius: 50%;
    }
    input:checked + .toggle-slider {
      background-color: #0070f3;
      border-color: #0070f3;
    }
    input:checked + .toggle-slider:before {
      transform: translateX(18px);
    }
  </style>
</head>
<body>
  <div id="toastContainer"></div>
  <!-- Header -->
  <div class="header">
    <div class="brand-wrap">
      <svg class="header-logo" width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" shape-rendering="geometricPrecision" title="AGY Zero-G Prism">
        <defs>
          <linearGradient id="v-spec" x1="16" y1="5" x2="26" y2="22" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#FFFFFF" stop-opacity="0.28"/>
            <stop offset="100%" stop-color="#FFFFFF" stop-opacity="0.04"/>
          </linearGradient>
          <linearGradient id="v-flank" x1="6" y1="22" x2="16" y2="22" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#FFFFFF" stop-opacity="0.10"/>
            <stop offset="100%" stop-color="#FFFFFF" stop-opacity="0.01"/>
          </linearGradient>
          <linearGradient id="v-cyan" x1="21" y1="17.5" x2="31" y2="14.5" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#00DFD8"/>
            <stop offset="100%" stop-color="#00DFD8" stop-opacity="0"/>
          </linearGradient>
          <linearGradient id="v-blue" x1="21" y1="19.5" x2="31" y2="19.5" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#0070F3"/>
            <stop offset="100%" stop-color="#0070F3" stop-opacity="0"/>
          </linearGradient>
          <linearGradient id="v-purp" x1="21" y1="21.5" x2="31" y2="24.5" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#7928CA"/>
            <stop offset="100%" stop-color="#7928CA" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <line x1="3" y1="6" x2="12" y2="15" stroke="#FFFFFF" stroke-width="1" stroke-opacity="0.75" stroke-linecap="round"/>
        <polygon points="6,22 16,26 26,22 16,21" fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>
        <polygon points="16,5 6,22 16,21" fill="url(#v-flank)" stroke="rgba(255,255,255,0.18)" stroke-width="1"/>
        <polygon points="16,5 16,21 26,22" fill="url(#v-spec)" stroke="rgba(255,255,255,0.32)" stroke-width="1"/>
        <line x1="16" y1="5" x2="16" y2="21" stroke="#FFFFFF" stroke-width="1" stroke-opacity="0.6"/>
        <line x1="21" y1="17.5" x2="31" y2="14.5" stroke="url(#v-cyan)" stroke-width="1" stroke-linecap="round"/>
        <line x1="21" y1="19.5" x2="31" y2="19.5" stroke="url(#v-blue)" stroke-width="1" stroke-linecap="round"/>
        <line x1="21" y1="21.5" x2="31" y2="24.5" stroke="url(#v-purp)" stroke-width="1" stroke-linecap="round"/>
      </svg>
      <div class="title-group">
        <h1><div class="pulse-dot"></div> AGY Multi-Account Router</h1>
        <div class="subtitle">Proxy Port: 8899 | Real-Time Telemetry & Quota Inspector</div>
      </div>
    </div>
    <div class="actions">
      <!-- Connect Client Button -->
      <button onclick="openConnectModal()" class="primary" id="btnConnect" title="Connect Developer Client to this Gateway">
        <svg class="btn-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
        <span>Connect Client</span>
      </button>

      <!-- Auto-Warmup Schedule Action Button -->
      <button onclick="openScheduleModal()" id="schedulePill" title="Automated 5H Quota Warmup Scheduler">
        <svg class="btn-svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        <span class="schedule-dot disabled" id="scheduleDot"></span>
        <span id="schedulePillText">Auto 5H: Loading...</span>
      </button>

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

  <!-- Daily Token Breakdown & Analytics Section -->
  <div class="analytics-card" id="analyticsCard">
    <div class="analytics-header">
      <div class="analytics-title">
        <svg class="btn-svg" viewBox="0 0 24 24" style="color:var(--accent);"><rect x="3" y="12" width="4" height="9" rx="1"/><rect x="10" y="7" width="4" height="14" rx="1"/><rect x="17" y="3" width="4" height="18" rx="1"/></svg>
        <span>Daily Token Consumption</span>
        <span class="analytics-badge" id="analyticsRangeSummary">Last 7 Days</span>
      </div>
      <div class="analytics-controls">
        <div class="range-selector">
          <button class="range-btn active" onclick="setTokenRange(7, this)">7 Days</button>
          <button class="range-btn" onclick="setTokenRange(14, this)">14 Days</button>
          <button class="range-btn" onclick="setTokenRange(30, this)">30 Days</button>
        </div>
        <button class="collapse-btn" id="btnToggleAnalytics" onclick="toggleAnalyticsCollapse()" title="Collapse / Expand">
          <svg class="btn-svg" id="collapseSvg" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </div>

    <div class="analytics-body" id="analyticsBody">
      <div class="chart-container">
        <div class="chart-legend">
          <div class="legend-items">
            <span class="legend-item"><span class="legend-dot prompt"></span> Prompt (Input)</span>
            <span class="legend-item"><span class="legend-dot completion"></span> Completion (Output)</span>
          </div>
          <div class="chart-summary-stat" id="chartSummaryStat">
            Period Total: <b id="periodTotalTokens" style="color:var(--accent);">0 tok</b> · <span id="periodTotalReqs">0 reqs</span>
          </div>
        </div>
        <div class="bars-wrapper" id="barsWrapper">
          <div style="width:100%; text-align:center; color:var(--text-muted); font-size:0.75rem; padding-top:40px;">Loading daily token metrics...</div>
        </div>
      </div>

      <div class="daily-table-wrap">
        <table class="daily-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Requests</th>
              <th>Input (Prompt)</th>
              <th>Output (Completion)</th>
              <th>Total Tokens</th>
              <th>Avg / Req</th>
            </tr>
          </thead>
          <tbody id="dailyTableBody">
            <tr><td colspan="6" style="text-align:center; padding:18px; color:var(--text-muted);">Loading daily breakdown...</td></tr>
          </tbody>
        </table>
      </div>
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

    function escapeHtml(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
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

    function formatResetTime(isoString) {
      if (!isoString) return { exact: "Ready", rel: "", full: "Ready (Full quota available)", isReady: true };
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return { exact: "Ready", rel: "", full: "Ready", isReady: true };
      const diff = d.getTime() - Date.now();
      if (diff <= 0) return { exact: "Ready", rel: "Now", full: "Quota reset has arrived", isReady: true };

      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const dd = String(d.getDate()).padStart(2, '0');
      const mo = String(d.getMonth() + 1).padStart(2, '0');
      const yyyy = d.getFullYear();

      const hours = Math.floor(diff / (1000 * 60 * 60));
      const mins = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
      let rel = "";
      if (hours >= 24) {
        const days = Math.floor(hours / 24);
        const remH = hours % 24;
        rel = `${days}d ${remH}h`;
      } else if (hours > 0) {
        rel = `${hours}h ${mins}m`;
      } else {
        rel = `${mins}m`;
      }

      const now = new Date();
      const isToday = d.toDateString() === now.toDateString();
      const exact = isToday ? `${hh}:${mm}` : `${dd}/${mo} ${hh}:${mm}`;
      const full = `${hh}:${mm} · ${dd}/${mo}/${yyyy} (in ${rel})`;

      return { exact, rel, full, isToday, isReady: false };
    }

    function getBarColor(pct, defaultColor) {
      if (pct < 15) return 'var(--red)';
      if (pct < 45) return 'var(--yellow)';
      return defaultColor;
    }

    function renderConcentricRingSvg(p5h, pWk, color5h, colorWk) {
      const p5 = Math.max(0, Math.min(100, Number(p5h) || 0));
      const pw = Math.max(0, Math.min(100, Number(pWk) || 0));
      const cOuter = 122.52;
      const offOuter = cOuter - (p5 / 100) * cOuter;
      const cInner = 84.82;
      const offInner = cInner - (pw / 100) * cInner;

      return `<svg viewBox="0 0 48 48">
        <circle cx="24" cy="24" r="19.5" fill="none" stroke="rgba(255,255,255,0.07)" stroke-width="3.2"/>
        <circle cx="24" cy="24" r="19.5" fill="none" stroke="${color5h}" stroke-width="3.2" stroke-linecap="round" stroke-dasharray="122.52" stroke-dashoffset="${offOuter.toFixed(2)}" style="transition:stroke-dashoffset 0.4s cubic-bezier(0.16,1,0.3,1);"/>
        <circle cx="24" cy="24" r="13.5" fill="none" stroke="rgba(255,255,255,0.07)" stroke-width="2.6"/>
        <circle cx="24" cy="24" r="13.5" fill="none" stroke="${colorWk}" stroke-width="2.6" stroke-linecap="round" stroke-dasharray="84.82" stroke-dashoffset="${offInner.toFixed(2)}" style="transition:stroke-dashoffset 0.4s cubic-bezier(0.16,1,0.3,1);"/>
      </svg>`;
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

    let currentTokenRangeDays = 7;
    let isAnalyticsCollapsed = localStorage.getItem('agy_analytics_collapsed') === 'true';

    function toggleAnalyticsCollapse() {
      isAnalyticsCollapsed = !isAnalyticsCollapsed;
      localStorage.setItem('agy_analytics_collapsed', isAnalyticsCollapsed);
      updateAnalyticsCollapseUI();
    }

    function updateAnalyticsCollapseUI() {
      const body = document.getElementById('analyticsBody');
      const btn = document.getElementById('btnToggleAnalytics');
      if (!body || !btn) return;
      if (isAnalyticsCollapsed) {
        body.classList.add('collapsed');
        btn.classList.add('collapsed');
      } else {
        body.classList.remove('collapsed');
        btn.classList.remove('collapsed');
      }
    }

    async function setTokenRange(days, btn) {
      currentTokenRangeDays = days;
      document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      const badge = document.getElementById('analyticsRangeSummary');
      if (badge) badge.innerText = `Last ${days} Days`;
      await fetchTokenAnalytics();
    }

    async function fetchTokenAnalytics() {
      try {
        const res = await fetch(`/api/analytics/tokens?range_days=${currentTokenRangeDays}`);
        if (!res.ok) return;
        const data = await res.json();
        renderTokenAnalytics(data);
      } catch (err) {
        console.warn("fetchTokenAnalytics error:", err);
      }
    }

    function renderTokenAnalytics(data) {
      const timeline = data.timeline || [];
      const summary = data.summary || {};

      const elPeriodTok = document.getElementById('periodTotalTokens');
      const elPeriodReq = document.getElementById('periodTotalReqs');
      if (elPeriodTok) elPeriodTok.innerText = `${formatTokenCount(summary.total_tokens || 0)} tok`;
      if (elPeriodReq) elPeriodReq.innerText = `${(summary.requests || 0).toLocaleString()} reqs`;

      const barsWrapper = document.getElementById('barsWrapper');
      if (barsWrapper) {
        barsWrapper.innerHTML = '';
        const _now = new Date();
        const todayStr = `${_now.getFullYear()}-${String(_now.getMonth() + 1).padStart(2, '0')}-${String(_now.getDate()).padStart(2, '0')}`;
        const maxTokens = Math.max(...timeline.map(d => d.total_tokens || 0), 1);

        timeline.forEach(d => {
          const tot = d.total_tokens || 0;
          const prompt = d.prompt_tokens || 0;
          const compl = d.completion_tokens || 0;
          const reqs = d.requests || 0;

          const totalHeightPct = tot > 0 ? Math.max(8, Math.round((tot / maxTokens) * 100)) : 2;
          const promptRatio = tot > 0 ? (prompt / tot) : 0.5;
          const complRatio = tot > 0 ? (compl / tot) : 0.5;

          const isToday = d.date === todayStr;
          const dateParts = d.date.split('-');
          const shortDate = isToday ? 'Today' : (dateParts.length >= 3 ? `${dateParts[1]}/${dateParts[2]}` : d.date);

          const col = document.createElement('div');
          col.className = 'bar-col';

          const promptPctLabel = tot > 0 ? Math.round(promptRatio * 100) : 0;
          const complPctLabel = tot > 0 ? (compl > 0 && Math.round(complRatio * 100) === 0 ? '<1' : Math.round(complRatio * 100)) : 0;

          col.innerHTML = `
            <div class="bar-val-top">${tot > 0 ? formatTokenCount(tot) : ''}</div>
            <div class="bar-stack" style="height: ${totalHeightPct}%;">
              <div class="bar-seg-prompt" style="height: ${tot > 0 ? Math.round(promptRatio * 100) : 0}%;"></div>
              <div class="bar-seg-completion" style="height: ${tot > 0 ? Math.round(complRatio * 100) : 0}%;"></div>
            </div>
            <div class="bar-label-bot ${isToday ? 'is-today' : ''}">${shortDate}</div>
            <div class="bar-tooltip">
              <div class="bar-tooltip-title">${d.date} ${isToday ? '(Today)' : ''}</div>
              <div>Total: <b style="color:#fff;">${tot.toLocaleString()}</b> tokens (${reqs} reqs)</div>
              <div>Input: <b style="color:var(--accent);">${prompt.toLocaleString()}</b> (${promptPctLabel}%)</div>
              <div>Output: <b style="color:var(--purple);">${compl.toLocaleString()}</b> (${complPctLabel}%)</div>
              ${reqs > 0 ? `<div style="color:var(--text-muted); font-size:0.68rem; margin-top:2px;">Avg: ${Math.round(tot / reqs).toLocaleString()} tok/req</div>` : ''}
            </div>
          `;
          barsWrapper.appendChild(col);
        });
      }

      const tbody = document.getElementById('dailyTableBody');
      if (tbody) {
        tbody.innerHTML = '';
        const reversed = [...timeline].reverse();
        const activeRows = reversed.filter(d => (d.requests > 0 || d.total_tokens > 0));

        if (activeRows.length === 0) {
          tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:18px; color:var(--text-muted);">No token consumption recorded in this period yet.</td></tr>`;
        } else {
          activeRows.forEach(d => {
            const tot = d.total_tokens || 0;
            const prompt = d.prompt_tokens || 0;
            const compl = d.completion_tokens || 0;
            const reqs = d.requests || 0;
            const avg = reqs > 0 ? Math.round(tot / reqs) : 0;
            const promptPct = tot > 0 ? Math.round((prompt / tot) * 100) : 0;
            const complPct = tot > 0 ? (compl > 0 && Math.round((compl / tot) * 100) === 0 ? '<1' : Math.round((compl / tot) * 100)) : 0;

            const tr = document.createElement('tr');
            tr.innerHTML = `
              <td><b style="color:#fff;">${d.date}</b></td>
              <td>${reqs.toLocaleString()} reqs</td>
              <td><span style="color:var(--accent); font-weight:600;">${prompt.toLocaleString()}</span> <span style="color:var(--text-muted); font-size:0.7rem;">(${promptPct}%)</span></td>
              <td><span style="color:var(--purple); font-weight:600;">${compl.toLocaleString()}</span> <span style="color:var(--text-muted); font-size:0.7rem;">(${complPct}%)</span></td>
              <td><b style="color:#fff;">${tot.toLocaleString()}</b></td>
              <td style="color:var(--text-muted);">${avg.toLocaleString()} tok</td>
            `;
            tbody.appendChild(tr);
          });
        }
      }
    }

    async function fetchStatus(btn = null) {
      const doFetch = async () => {
        const res = await fetch('/api/status');
        const data = await res.json();
        accountsCache = data.accounts || {};
        renderAccounts(accountsCache);
        updateKPIs(data);
        await fetchTokenAnalytics();
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

      if (acc.in_quarantine) {
        badgeClass = 'danger';
        badgeText = `QUARANTINE (${acc.quarantine_remaining_sec}s)`;
        isExhaustedOrLow = true;
      } else if (acc.in_cooldown) {
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
        g5hExhausted, c5hExhausted,
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
        const { name, acc, emailDisplay, nameDisplay, badgeClass, badgeText, isGeminiHealthy, isClaudeHealthy, gWeekly, gWeeklyExhausted, g5h, g5hEffectivePct, cWeekly, cWeeklyExhausted, c5h, c5hEffectivePct, g5hExhausted, c5hExhausted } = item;
        const g5hColor = getBarColor(g5hEffectivePct, 'var(--accent)');
        const c5hColor = getBarColor(c5hEffectivePct, 'var(--purple)');
        const gWkColor = gWeeklyExhausted ? 'var(--red)' : (gWeekly.percent < 25 ? 'var(--yellow)' : '#10b981');
        const cWkColor = cWeeklyExhausted ? 'var(--red)' : (cWeekly.percent < 25 ? 'var(--yellow)' : '#10b981');
        const g5hInfo = formatResetTime(g5h.reset_time);
        const gWkInfo = formatResetTime(gWeekly.reset_time);
        const c5hInfo = formatResetTime(c5h.reset_time);
        const cWkInfo = formatResetTime(cWeekly.reset_time);

        const g5hValText = gWeeklyExhausted ? '0%' : (g5h.percent <= 5.0 && g5h.reset_time ? '≤5%' : Number(g5hEffectivePct).toFixed(0) + '%');
        const gWkValText = gWeeklyExhausted ? '≤5%' : Number(gWeekly.percent).toFixed(0) + '%';
        const c5hValText = cWeeklyExhausted ? '0%' : (c5h.percent <= 5.0 && c5h.reset_time ? '≤5%' : Number(c5hEffectivePct).toFixed(0) + '%');
        const cWkValText = cWeeklyExhausted ? '≤5%' : Number(cWeekly.percent).toFixed(0) + '%';

        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="card-header">
            <div style="min-width: 0; flex: 1; padding-right: 8px;">
              <div class="acc-email" title="${emailDisplay}">
                <span>${emailDisplay}</span>
              </div>
              <div style="display: flex; align-items: center; gap: 8px; margin-top: 4px; flex-wrap: wrap;">
                <span class="code-chip" style="color:var(--accent);">Profile: ${name}</span>
                ${nameDisplay ? `<span style="font-size: 0.74rem; color: var(--text-muted);">${nameDisplay}</span>` : ''}
                ${acc.priority_tier === 1 ? `<span class="code-chip" style="color:#10b981; border-color:rgba(16,185,129,0.3);" title="Priority 1: Draining soonest expiring 5H quota">DRAIN: ${acc.time_until_5h_reset_formatted || 'Active'}</span>` : ''}
              </div>
            </div>
            <div class="badge ${badgeClass}">${badgeText}</div>
          </div>

          <!-- Concentric Radial Quota Box (Layout B) -->
          <div class="quota-radial-box">
            <!-- Row 1: Gemini -->
            <div class="radial-row">
              <div class="radial-gauge-wrap" title="Gemini Quota&#10;Outer (5H): ${g5hValText} (Reset: ${g5hInfo.full})&#10;Inner (Weekly): ${gWkValText} (Reset: ${gWkInfo.full})">
                ${renderConcentricRingSvg(g5hEffectivePct, gWeekly.percent, g5hColor, gWkColor)}
                <div class="radial-center-val" style="color:${g5hColor};">${g5hValText}</div>
              </div>
              <div class="radial-info-col">
                <div class="radial-title-row">
                  <span style="color:var(--accent);">Gemini</span>
                  <span style="color:${g5hExhausted ? 'var(--red)' : 'var(--text-muted)'}; font-size:0.62rem;">${g5hExhausted ? 'Exhausted' : (g5hInfo.rel ? `in ${g5hInfo.rel}` : 'Ready')}</span>
                </div>
                <div class="radial-metric-line" title="Gemini 5H Reset: ${g5hInfo.full}">
                  <span class="radial-metric-tag" style="color:var(--accent);">5h</span>
                  <span class="radial-metric-val" style="color:${g5hColor};">${g5hValText}</span>
                  <span class="radial-metric-sub">· ${g5hInfo.exact}${g5hInfo.rel ? ` (${g5hInfo.rel})` : ''}</span>
                </div>
                <div class="radial-metric-line" title="Gemini Weekly Reset: ${gWkInfo.full}">
                  <span class="radial-metric-tag" style="color:#10b981;">Wk</span>
                  <span class="radial-metric-val" style="color:${gWkColor};">${gWkValText}</span>
                  <span class="radial-metric-sub">· ${gWkInfo.exact}${gWkInfo.rel ? ` (${gWkInfo.rel})` : ''}</span>
                </div>
              </div>
            </div>

            <div style="height:1px; background:rgba(255,255,255,0.04); margin:1px 0;"></div>

            <!-- Row 2: Claude / GPT -->
            <div class="radial-row">
              <div class="radial-gauge-wrap" title="Claude & GPT Quota&#10;Outer (5H): ${c5hValText} (Reset: ${c5hInfo.full})&#10;Inner (Weekly): ${cWkValText} (Reset: ${cWkInfo.full})">
                ${renderConcentricRingSvg(c5hEffectivePct, cWeekly.percent, c5hColor, cWkColor)}
                <div class="radial-center-val" style="color:${c5hColor};">${c5hValText}</div>
              </div>
              <div class="radial-info-col">
                <div class="radial-title-row">
                  <span style="color:var(--purple);">Claude · GPT</span>
                  <span style="color:${c5hExhausted ? 'var(--red)' : 'var(--text-muted)'}; font-size:0.62rem;">${c5hExhausted ? 'Exhausted' : (c5hInfo.rel ? `in ${c5hInfo.rel}` : 'Ready')}</span>
                </div>
                <div class="radial-metric-line" title="Claude 5H Reset: ${c5hInfo.full}">
                  <span class="radial-metric-tag" style="color:var(--purple);">5h</span>
                  <span class="radial-metric-val" style="color:${c5hColor};">${c5hValText}</span>
                  <span class="radial-metric-sub">· ${c5hInfo.exact}${c5hInfo.rel ? ` (${c5hInfo.rel})` : ''}</span>
                </div>
                <div class="radial-metric-line" title="Claude Weekly Reset: ${cWkInfo.full}">
                  <span class="radial-metric-tag" style="color:#10b981;">Wk</span>
                  <span class="radial-metric-val" style="color:${cWkColor};">${cWkValText}</span>
                  <span class="radial-metric-sub">· ${cWkInfo.exact}${cWkInfo.rel ? ` (${cWkInfo.rel})` : ''}</span>
                </div>
              </div>
            </div>

            <!-- Micro Legend Hint -->
            <div class="radial-legend-hint">
              <span><span class="radial-legend-dot" style="background:var(--accent);"></span> Outer: 5h</span>
              <span>·</span>
              <span><span class="radial-legend-dot" style="background:#10b981;"></span> Inner: Wk</span>
            </div>
          </div>

          <!-- Bottom Footer Row: Exp & Actions -->
          <div class="meta-row">
            <span>Exp: <b>${acc.token_expires_in_min}m</b></span>
            <div style="display:flex; gap:6px;">
              ${acc.in_quarantine ? `
                <button onclick="clearQuarantineSingle('${name}', this)" class="card-btn" style="border-color:var(--red); color:var(--red);" title="Release from Quarantine (${acc.quarantine_reason || ''})">
                  <span>Un-quarantine</span>
                </button>
              ` : ''}
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
        const g5hInfo = formatResetTime(g5h.reset_time);
        const gWkInfo = formatResetTime(gWeekly.reset_time);
        const c5hInfo = formatResetTime(c5h.reset_time);
        const cWkInfo = formatResetTime(cWeekly.reset_time);

        tr.innerHTML = `
          <td>
            <div style="font-weight:600; color:#fff;">${emailDisplay}</div>
            <div style="display:flex; align-items:center; gap:6px; margin-top:3px; flex-wrap:wrap;">
              <span class="code-chip" style="color:var(--accent); font-size:0.7rem;">${name}</span>
              ${nameDisplay ? `<span style="font-size:0.72rem; color:var(--text-muted);">${nameDisplay}</span>` : ''}
              ${acc.priority_tier === 1 ? `<span class="code-chip" style="color:#10b981; border-color:rgba(16,185,129,0.3); font-size:0.7rem;" title="Priority 1: Draining soonest expiring 5H quota">DRAIN: ${acc.time_until_5h_reset_formatted || 'Active'}</span>` : ''}
            </div>
          </td>
          <td>
            <span class="badge ${badgeClass}">${badgeText}</span>
          </td>
          <td>
            <div class="bar-compact-wrap" title="Gemini 5h: ${Number(g5hEffectivePct).toFixed(1)}% (Reset: ${g5hInfo.full}) | Weekly: ${Number(gWeekly.percent).toFixed(1)}% (Reset: ${gWkInfo.full})">
              <div class="bar-compact">
                <div class="bar-compact-fill" style="width:${g5hEffectivePct}%; background:${g5hColor};"></div>
              </div>
              <span style="font-weight:600; font-size:0.76rem; width:46px; text-align:right;">${Number(g5hEffectivePct).toFixed(1)}%</span>
            </div>
            <span class="sub-tag" style="display:flex; justify-content:space-between; gap:6px;">
              <span>Wk: <b>${gWeeklyExhausted ? 'Exhausted' : Number(gWeekly.percent).toFixed(1) + '%'}</b></span>
              <span class="tooltip-anchor" title="Gemini 5h: ${g5hInfo.full} | Weekly: ${gWkInfo.full}">5h: ${g5hInfo.exact} · Wk: ${gWkInfo.exact}</span>
            </span>
          </td>
          <td>
            <div class="bar-compact-wrap" title="Claude 5h: ${Number(c5hEffectivePct).toFixed(1)}% (Reset: ${c5hInfo.full}) | Weekly: ${Number(cWeekly.percent).toFixed(1)}% (Reset: ${cWkInfo.full})">
              <div class="bar-compact">
                <div class="bar-compact-fill" style="width:${c5hEffectivePct}%; background:${c5hColor};"></div>
              </div>
              <span style="font-weight:600; font-size:0.76rem; width:46px; text-align:right;">${cWeeklyExhausted ? '0.0%' : Number(c5hEffectivePct).toFixed(1) + '%'}</span>
            </div>
            <span class="sub-tag" style="display:flex; justify-content:space-between; gap:6px;">
              <span>Wk: <b>${cWeeklyExhausted ? 'Exhausted' : Number(cWeekly.percent).toFixed(1) + '%'}</b></span>
              <span class="tooltip-anchor" title="Claude 5h: ${c5hInfo.full} | Weekly: ${cWkInfo.full}">5h: ${c5hInfo.exact} · Wk: ${cWkInfo.exact}</span>
            </span>
          </td>
          <td style="text-align:right;">
            <div style="display:inline-flex; align-items:center; gap:10px;">
              <span class="sub-tag" style="margin:0;">Exp: <b>${acc.token_expires_in_min}m</b></span>
              ${acc.in_quarantine ? `
                <button onclick="clearQuarantineSingle('${name}', this)" class="card-btn" style="border-color:var(--red); color:var(--red);" title="Release from Quarantine">
                  <span>Un-quarantine</span>
                </button>
              ` : ''}
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

    async function clearQuarantineSingle(name, btn) {
      await handleActionWithButton(btn, async () => {
        try {
          const res = await fetch(`/api/accounts/${name}/clear-quarantine`, { method: 'POST' });
          const data = await res.json();
          showToast(`${name}: ${data.message || 'Released from quarantine'}`, 'success');
          await fetchStatus();
        } catch (e) {
          showToast(`${name} un-quarantine error: ${e.message}`, 'error');
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

      const safeTime = escapeHtml(evt.time || '');
      const safeMethod = escapeHtml(evt.method || 'GET');
      const safeModel = escapeHtml(modelLabel);
      const safePath = escapeHtml(evt.path || '/');
      const safeShortPath = escapeHtml(evt.path && evt.path.length > 28 ? evt.path.substring(0, 28) + '...' : (evt.path || '/'));
      const safeAccount = escapeHtml(evt.account || '');
      const safeStatusText = escapeHtml(evt.status_text || String(evt.status || 200));
      const safeLatency = escapeHtml(String(evt.latency_ms || 0));
      const safeId = escapeHtml(String(evt.id || ''));
      const safeSizeLabel = escapeHtml(sizeLabel);

      row.innerHTML = `
        <td style="color:var(--text-muted); font-size:0.75rem;">${safeTime}</td>
        <td><span class="pill ${methodClass}">${safeMethod}</span></td>
        <td><span class="code-chip" style="color:var(--accent); font-weight:600;">${safeModel}</span></td>
        <td><span class="code-chip" title="${safePath}">${safeShortPath}</span></td>
        <td><b style="color:#fff;">${safeAccount}</b></td>
        <td><b style="color: ${statusColor};">${safeStatusText}</b></td>
        <td style="color:var(--text-muted);">${safeLatency}ms</td>
        <td style="white-space:nowrap;">
          <button class="btn-inspect" onclick="openPayloadModal('${safeId}')">
            <svg class="btn-svg" style="width:13px; height:13px;" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            <span>Inspect (${safeSizeLabel})</span>
          </button>
          ${tokenBadge}
        </td>
      `;

      if (tbody.firstChild) {
        tbody.insertBefore(row, tbody.firstChild);
      } else {
        tbody.appendChild(row);
      }
      if (tbody.children.length > 300) {
        const last = tbody.lastElementChild;
        if (last && last.id) {
          const oldId = last.id.replace('row-', '');
          renderedIds.delete(oldId);
          logDataMap.delete(oldId);
        }
        if (last) last.remove();
      }
      applyFilters();
    }

    function openPayloadModal(id) {
      const evt = logDataMap.get(id);
      if (!evt) return;
      currentModalEvt = evt;

      document.getElementById('modalTitle').innerHTML = `
        <span class="pill ${escapeHtml((evt.method||'POST').toLowerCase())}">${escapeHtml(evt.method)}</span>
        <span style="color:var(--accent);">${escapeHtml(evt.model || 'API')}</span> &bull;
        <span style="color:var(--text-muted); font-size:0.85rem;">${escapeHtml(evt.path)}</span>
      `;
      let statusColor = evt.status >= 400 ? 'var(--red)' : 'var(--green)';
      if (evt.status === 429) statusColor = 'var(--yellow)';

      let tokenMeta = '';
      if (evt.tokens && evt.tokens.total) {
        tokenMeta = ` &bull; Tokens: <b style="color:var(--accent);">${evt.tokens.total.toLocaleString()}</b> (Prompt: ${evt.tokens.prompt.toLocaleString()} · Output: ${evt.tokens.completion.toLocaleString()})`;
      }

      document.getElementById('modalMeta').innerHTML = `
        Account: <b style="color:var(--accent);">${escapeHtml(evt.account)}</b> &bull; 
        Status: <b style="color:${statusColor}">${escapeHtml(evt.status_text || String(evt.status))}</b> &bull; 
        Latency: <b>${escapeHtml(String(evt.latency_ms))}ms</b>${tokenMeta} &bull; Time: <b>${escapeHtml(evt.time)}</b>
      `;
      document.getElementById('modalHeaderFlow').innerHTML = `
        <div style="margin-bottom:6px;"><span style="color:var(--text-muted);">Your Token:</span> <span class="code-chip">${escapeHtml(evt.inbound_token)}</span></div>
        <div style="margin-bottom:6px;"><span style="color:var(--accent);">➔ Selected Account:</span> <b style="color:#fff;">${escapeHtml(evt.account)}</b></div>
        <div><span style="color:var(--accent);">➔ Google Token:</span> <span class="code-chip">${escapeHtml(evt.overridden_token)}</span></div>
      `;

      // Tab 1: Request
      let reqHtml = '<span class="json-null">(empty body)</span>';
      if (evt.body) {
        try {
          const parsed = JSON.parse(evt.body);
          reqHtml = syntaxHighlightJson(parsed);
        } catch (e) {
          reqHtml = `<span class="json-string">${escapeHtml(evt.body)}</span>`;
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
          respHtml = `<span class="json-string">${escapeHtml(evt.response_preview)}</span>`;
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
      if (e.key === 'Escape') {
        closeModal();
        closeScheduleModal();
        closeConnectModal();
      }
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

    let fetchStatusDebounceTimer = null;
    function scheduleFetchStatus() {
      if (fetchStatusDebounceTimer) clearTimeout(fetchStatusDebounceTimer);
      fetchStatusDebounceTimer = setTimeout(() => {
        fetchStatus();
      }, 1000);
    }

    function formatTime12h(timeStr) {
      if (!timeStr) return "05:00 AM";
      const parts = timeStr.split(':');
      if (parts.length < 2) return timeStr;
      let h = parseInt(parts[0], 10);
      const m = parts[1].padStart(2, '0');
      const ampm = h >= 12 ? 'PM' : 'AM';
      h = h % 12;
      if (h === 0) h = 12;
      return `${String(h).padStart(2, '0')}:${m} ${ampm}`;
    }

    function formatRemaining(sec) {
      if (sec === undefined || sec === null || sec < 0) return "--";
      if (sec === 0) return "due now";
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      if (h > 0) return `${h}h ${m}m`;
      return `${m}m`;
    }

    let warmupScheduleCache = null;

    async function fetchWarmupSchedule() {
      try {
        const res = await fetch('/api/schedule/warmup');
        if (!res.ok) return;
        const status = await res.json();
        warmupScheduleCache = status;
        renderWarmupScheduleUI(status);
      } catch (err) {
        console.warn("fetchWarmupSchedule error:", err);
      }
    }

    function renderWarmupScheduleUI(status) {
      const pill = document.getElementById('schedulePill');
      const dot = document.getElementById('scheduleDot');
      const text = document.getElementById('schedulePillText');
      if (!pill || !dot || !text) return;

      dot.className = 'schedule-dot';

      if (!status.enabled) {
        dot.classList.add('disabled');
        text.innerText = 'Auto 5H: Off';
      } else if (status.retry_state && status.retry_state.in_retry) {
        dot.classList.add('retrying');
        const attempt = status.retry_state.attempt || 1;
        const maxRetries = status.retry_state.max_retries || 3;
        const sec = status.retry_state.next_retry_sec || 30;
        text.innerText = `Auto 5H: Retry ${attempt}/${maxRetries} (${sec}s)`;
      } else {
        dot.classList.add('enabled');
        const firstTime = (status.times && status.times[0]) || '05:00';
        const time12 = formatTime12h(firstTime);
        const rem = formatRemaining(status.next_run_in_sec);
        text.innerText = `Auto 5H: ${time12} (${rem})`;
      }

      const toggle = document.getElementById('scheduleEnabledToggle');
      const timesInput = document.getElementById('scheduleTimesInput');
      const nextRunEl = document.getElementById('modalNextRunText');
      const lastRunEl = document.getElementById('modalLastRunText');
      const retryStateEl = document.getElementById('modalRetryStateText');

      if (toggle && document.activeElement !== timesInput) {
        toggle.checked = Boolean(status.enabled);
        if (timesInput && !timesInput.value) {
          timesInput.value = (status.times || ['05:00']).join(', ');
        }
      }
      if (nextRunEl) {
        nextRunEl.innerText = status.next_run ? `${status.next_run} (${formatRemaining(status.next_run_in_sec)})` : '--';
      }
      if (lastRunEl) {
        lastRunEl.innerText = status.last_status ? status.last_status.toUpperCase() : 'NEVER RUN';
      }
      if (retryStateEl) {
        if (status.retry_state && status.retry_state.in_retry) {
          retryStateEl.innerText = `Retrying ${status.retry_state.attempt}/${status.retry_state.max_retries}`;
          retryStateEl.style.color = 'var(--yellow)';
        } else {
          retryStateEl.innerText = 'Idle (Normal)';
          retryStateEl.style.color = 'var(--text-muted)';
        }
      }
    }

    function openScheduleModal() {
      const modal = document.getElementById('scheduleModal');
      if (modal) {
        modal.classList.add('active');
        if (warmupScheduleCache) {
          const timesInput = document.getElementById('scheduleTimesInput');
          if (timesInput) timesInput.value = (warmupScheduleCache.times || ['05:00']).join(', ');
        }
        fetchWarmupSchedule();
      }
    }

    function closeScheduleModal() {
      const modal = document.getElementById('scheduleModal');
      if (modal) modal.classList.remove('active');
    }

    async function saveScheduleConfig(btn) {
      await handleActionWithButton(btn, async () => {
        try {
          const enabled = document.getElementById('scheduleEnabledToggle').checked;
          const rawTimes = document.getElementById('scheduleTimesInput').value;
          const times = rawTimes.split(',').map(t => t.trim()).filter(Boolean);

          const res = await fetch('/api/schedule/warmup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled, times })
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const status = await res.json();
          warmupScheduleCache = status;
          renderWarmupScheduleUI(status);
          showToast('Schedule settings saved successfully', 'success');
          closeScheduleModal();
        } catch (e) {
          showToast(`Failed to save schedule: ${e.message}`, 'error');
        }
      });
    }

    async function triggerWarmupNow(btn) {
      await handleActionWithButton(btn, async () => {
        try {
          showToast('Triggering warmup cycle...', 'info');
          const res = await fetch('/api/schedule/warmup/trigger', { method: 'POST' });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const data = await res.json();
          showToast('Manual warmup cycle triggered', 'success');
          await fetchWarmupSchedule();
          scheduleFetchStatus();
        } catch (e) {
          showToast(`Warmup trigger failed: ${e.message}`, 'error');
        }
      });
    }

    function handleWarmupSseEvent(data) {
      if (data.type === 'warmup_retry') {
        const attempt = data.attempt || 1;
        const maxR = data.max_retries || 3;
        const nextSec = data.next_retry_sec || 30;
        showToast(`Auto 5H: Retrying ${attempt}/${maxR} in ${nextSec}s`, 'warning');
      } else if (data.type === 'warmup_completed') {
        const succ = data.status === 'success' || data.success === true;
        showToast(`Auto 5H: Warmup cycle ${succ ? 'completed' : 'finished with warnings'}`, succ ? 'success' : 'warning');
        scheduleFetchStatus();
      } else if (data.type === 'warmup_scheduled' || data.type === 'warmup_started') {
        showToast(`Auto 5H: Warmup cycle started`, 'info');
      }
      fetchWarmupSchedule();
    }

    function initSSE() {
      try {
        const evtSource = new EventSource('/api/stream');
        evtSource.addEventListener('request', (e) => {
          const data = JSON.parse(e.data);
          // SSE Discriminator safeguard
          if (data.type === 'warmup_retry' || data.type === 'warmup_completed' || data.type === 'warmup_scheduled' || data.type === 'warmup_started') {
            handleWarmupSseEvent(data);
            return;
          }
          appendLogRow(data, true);
          scheduleFetchStatus();
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
    updateAnalyticsCollapseUI();
    fetchStatus();
    pollLogs();
    initSSE();
    fetchWarmupSchedule();

    setInterval(pollLogs, 1500);
    setInterval(fetchStatus, 8000);
    setInterval(fetchWarmupSchedule, 15000);

    function openConnectModal() {
      const origin = window.location.origin;
      const cmd = `agy-client connect ${origin}`;
      const elem = document.getElementById('connectCmdText');
      if (elem) elem.innerText = cmd;
      const modal = document.getElementById('connectModal');
      if (modal) modal.style.display = 'flex';
    }

    function closeConnectModal() {
      const modal = document.getElementById('connectModal');
      if (modal) modal.style.display = 'none';
    }

    function copyConnectCmd(btn) {
      const text = document.getElementById('connectCmdText').innerText;
      navigator.clipboard.writeText(text).then(() => {
        const orig = btn.innerText;
        btn.innerText = 'Copied!';
        setTimeout(() => btn.innerText = orig, 1800);
      });
    }
  </script>

  <!-- Modal Dialog: Auto-Warmup Schedule Settings -->
  <div class="modal-backdrop" id="scheduleModal" onclick="if(event.target===this)closeScheduleModal()">
    <div class="modal-box" style="max-width: 500px;">
      <div class="modal-header">
        <div class="modal-title">
          <svg class="btn-svg" viewBox="0 0 24 24" style="color:var(--accent);"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          <span>Automated 5H Quota Scheduler</span>
        </div>
        <button class="modal-close" onclick="closeScheduleModal()">
          <svg class="btn-svg" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body" style="gap: 16px;">
        <div style="font-size: 0.8rem; color: var(--text-muted); line-height: 1.5;">
          Automatically fire a 1-token probe across all accounts at scheduled local times so Google Antigravity quota buckets reset to 100% early.
        </div>

        <!-- Toggle Row -->
        <div style="display: flex; justify-content: space-between; align-items: center; padding: 12px 14px; background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 8px;">
          <div>
            <div style="font-size: 0.82rem; font-weight: 600; color: #fff;">Enable Auto-Warmup</div>
            <div style="font-size: 0.72rem; color: var(--text-muted);">Background daemon runs in proxy process</div>
          </div>
          <label style="position: relative; display: inline-block; width: 42px; height: 22px; cursor: pointer;">
            <input type="checkbox" id="scheduleEnabledToggle" style="opacity: 0; width: 0; height: 0;">
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- Times Input -->
        <div style="display: flex; flex-direction: column; gap: 6px;">
          <label style="font-size: 0.74rem; font-weight: 600; text-transform: uppercase; color: var(--text-muted); font-family: var(--font-mono);">
            Scheduled Times (HH:MM, comma-separated)
          </label>
          <input type="text" id="scheduleTimesInput" placeholder="05:00" style="background: #08080a; border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-size: 0.82rem; font-family: var(--font-mono); color: #fff; outline: none;">
          <span style="font-size: 0.7rem; color: var(--text-muted);">Default: 05:00 (Local time). Supports multiple e.g. 05:00, 12:00</span>
        </div>

        <!-- Status Card -->
        <div style="display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; background: #08080a; border: 1px solid var(--border); border-radius: 8px; font-family: var(--font-mono); font-size: 0.75rem;">
          <div style="display: flex; justify-content: space-between;">
            <span style="color: var(--text-muted);">Next Run:</span>
            <b id="modalNextRunText" style="color: var(--accent);">--</b>
          </div>
          <div style="display: flex; justify-content: space-between;">
            <span style="color: var(--text-muted);">Last Run Status:</span>
            <span id="modalLastRunText" style="color: #fff;">--</span>
          </div>
          <div style="display: flex; justify-content: space-between;">
            <span style="color: var(--text-muted);">Retry State:</span>
            <span id="modalRetryStateText" style="color: var(--text-muted);">Idle</span>
          </div>
        </div>

        <!-- Action Buttons -->
        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px; gap: 10px;">
          <button onclick="triggerWarmupNow(this)" id="btnTriggerModal" style="height: 32px; font-size: 0.76rem;" title="Run immediate probe cycle">
            <svg class="btn-svg" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            <span>Run Warmup Now</span>
          </button>
          <div style="display: flex; gap: 8px;">
            <button onclick="closeScheduleModal()" style="height: 32px; font-size: 0.76rem;">Cancel</button>
            <button onclick="saveScheduleConfig(this)" class="primary" style="height: 32px; font-size: 0.76rem;">
              <span>Save Schedule</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Connect Client Modal -->
  <div id="connectModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75); z-index:9999; align-items:center; justify-content:center; backdrop-filter:blur(4px);" onclick="if(event.target===this)closeConnectModal()">
    <div style="background:#1e222b; border:1px solid #2d3342; border-radius:12px; width:90%; max-width:540px; padding:24px; box-shadow:0 20px 40px rgba(0,0,0,0.6); font-family:inherit; color:#fff;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
        <h3 style="margin:0; font-size:1.15rem; font-weight:600; display:flex; align-items:center; gap:8px;">
          <svg class="btn-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
          <span>Connect Developer Client</span>
        </h3>
        <button onclick="closeConnectModal()" style="background:none; border:none; color:#888; font-size:1.4rem; cursor:pointer; line-height:1;">&times;</button>
      </div>
      <p style="color:#aaa; font-size:0.9rem; margin-top:0; margin-bottom:16px; line-height:1.5;">
        Run this 1-liner in your terminal to connect <code>agy-run</code> directly to this gateway:
      </p>
      <div style="background:#11141a; border:1px solid #333a4d; border-radius:8px; padding:12px 14px; display:flex; align-items:center; justify-content:space-between; margin-bottom:16px; font-family:monospace; font-size:0.9rem;">
        <span id="connectCmdText" style="color:#4ade80; overflow-x:auto; white-space:nowrap; margin-right:8px;">agy-client connect http://...</span>
        <button onclick="copyConnectCmd(this)" style="background:#2563eb; color:#fff; border:none; padding:6px 12px; border-radius:6px; cursor:pointer; font-size:0.8rem; font-weight:600; flex-shrink:0;">Copy</button>
      </div>
      <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid #2d3342; padding-top:14px; font-size:0.85rem; color:#888;">
        <span>Manual Root CA Certificate:</span>
        <a href="/ca.crt" download="ca.crt" style="color:#38bdf8; text-decoration:none; font-weight:500; display:inline-flex; align-items:center; gap:4px;">
          <svg class="btn-svg" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          <span>Download ca.crt</span>
        </a>
      </div>
    </div>
  </div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)
