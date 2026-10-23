import os
import time
import uuid
import json
import asyncio
from typing import Optional, Set
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse, HTMLResponse
import litellm

import config
import analytics
import dashboard
from controller import controller
from key_manager import key_manager

litellm.suppress_debug_info = True

app = FastAPI(title="Horizon Proxy")

# Active SSE subscriber queues for real-time dashboard telemetry
_sse_subscribers: Set[asyncio.Queue] = set()

async def broadcast_telemetry(row: dict):
    if not _sse_subscribers:
        return
    payload = {"type": "telemetry", "row": row}
    dead = set()
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _sse_subscribers.difference_update(dead)

# Estimates prompt token weight for reservation tracking before calling Gemini
def estimate_tokens(body: dict) -> int:
    msgs = body.get("messages") or []
    try:
        return litellm.token_counter(model=config.PRIMARY_GEMINI_MODEL, messages=msgs)
    except Exception:
        chars = sum(len(str(m.get("content") or "")) for m in msgs if isinstance(m, dict))
        tools = body.get("tools") or []
        if tools:
            chars += len(str(tools))
        return max(1, chars // 4)

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# -- health & model routes --
@app.get("/health")
async def health():
    now = time.time()
    healthy_count = sum(1 for q in key_manager.quarantined if not q)

    # Return 503 if all configured keys are quarantined
    if key_manager.num_keys > 0 and healthy_count == 0:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "All Gemini keys are quarantined"}
        )

    pool = key_manager.get_pool_status(now)
    spend_blocked = bool(controller.check_spend(now))

    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "keys_alive": f"{healthy_count}/{key_manager.num_keys}",
            "tpm_available": pool["pool_tpm_available"],
            "openrouter_configured": bool(config.OPENROUTER_KEY),
            "spend_capped": spend_blocked,
            "spend_today_usd": round(controller.ledger["day_usd"], 4)
        }
    )

@app.get("/v1/models")
@app.get("/models")
async def list_models():
    model_ids = list(config.OPENROUTER_ROUTES.keys()) + ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": int(time.time()), "owned_by": "horizon-proxy"} for m in model_ids],
    }

# -- dynamic runtime tuning endpoints --
@app.post("/api/budget")
async def update_budget(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    daily = float(body.get("daily_usd", controller.daily_cap))
    run = float(body.get("run_usd", controller.run_cap))
    controller.save_limits(daily, run)
    return {"status": "ok", "daily_usd": controller.daily_cap, "run_usd": controller.run_cap}

@app.post("/api/limits")
async def update_limits(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    key_manager.update_limits(
        rpm=body.get("rpm"),
        tpm=body.get("tpm"),
        rpd=body.get("rpd"),
        max_parallel=body.get("max_parallel")
    )
    return {"status": "ok", "limits": key_manager.get_pool_status(time.time())["limits"]}

# -- sse real-time telemetry stream --
@app.get("/api/live")
async def sse_live_feed(request: Request):
    queue = asyncio.Queue(maxsize=100)
    _sse_subscribers.add(queue)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            # Clean exit on Ctrl+C shutdown or client disconnect
            return
        finally:
            _sse_subscribers.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# -- core completions proxy --
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model = body.get("model")
    rid = uuid.uuid4().hex[:6]
    t0 = time.time()
    is_stream = bool(body.get("stream", False))
    key_label = "-"
    target_backend_model = model

    # Caller extraction and path sanitization
    raw_caller = request.headers.get("X-Caller-ID") or body.get("user") or (request.client.host if request.client else "unknown")
    caller_path, caller_os = analytics.sanitize_caller(raw_caller)

    tool_names = sorted({(t.get("function") or {}).get("name", "?") for t in (body.get("tools") or []) if isinstance(t, dict)})
    tools_in_count = len(body.get("tools") or [])
    
    gemini_key_label = None
    gemini_reserved = 0
    gemini_recorded = False

    def finish_gemini(actual_tokens: Optional[int] = None):
        nonlocal gemini_recorded
        if gemini_key_label and not gemini_recorded:
            gemini_recorded = True
            if actual_tokens is not None and actual_tokens > 0:
                key_manager.record_usage(gemini_key_label, gemini_reserved, actual_tokens)
            else:
                key_manager.release_reservation(gemini_key_label, gemini_reserved)

    # Route 1: OpenRouter (Paid routes governed by spend controller)
    if model in config.OPENROUTER_ROUTES:
        rejection = controller.check_spend(time.time())
        if rejection:
            config.log.warning("[%s] 402 SPEND CAP %s", rid, rejection)
            return JSONResponse(status_code=402, content={"error": {"message": rejection, "type": "SpendCapExceeded"}})

        if config.MAX_OUT_TOKENS:
            for k in ("max_tokens", "max_completion_tokens"):
                if isinstance(body.get(k), int) and body[k] > config.MAX_OUT_TOKENS:
                    body[k] = config.MAX_OUT_TOKENS
            if not any(isinstance(body.get(k), int) for k in ("max_tokens", "max_completion_tokens")):
                body["max_completion_tokens"] = config.MAX_OUT_TOKENS

        target, effort_val = config.OPENROUTER_ROUTES[model]
        target_backend_model = target
        body["model"] = target
        body["api_key"] = config.OPENROUTER_KEY
        if effort_val:
            body.setdefault("extra_body", {}).setdefault("reasoning", {"effort": effort_val})

    # Route 2: Gemini (16-key balancer with 3.1 fallback)
    elif model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
        msgs = body.get("messages") or []
        while msgs and msgs[-1].get("role") in ("assistant", "model"):
            last_content = msgs[-1].get("content")
            if not last_content or (isinstance(last_content, str) and not last_content.strip()):
                msgs.pop()
            else:
                msgs.append({"role": "user", "content": "Continue."})
                break
        body["messages"] = msgs

        gemini_reserved = estimate_tokens(body)
        key_label, key, pace_wait, use_fallback = await key_manager.acquire_key(estimated_tokens=gemini_reserved)
        gemini_key_label = key_label

        target_backend_model = config.FALLBACK_GEMINI_MODEL if (use_fallback or model == "gemini-3.1-flash-lite") else config.PRIMARY_GEMINI_MODEL
        body["model"] = target_backend_model
        body["api_key"] = key

    else:
        config.log.warning("[%s] unknown model: %s", rid, model)
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")

    if is_stream:
        body.setdefault("stream_options", {"include_usage": True})

    try:
        response = await litellm.acompletion(**body)
    except Exception as e:
        finish_gemini()
        err_msg = str(e)
        
        if "401" in err_msg or "403" in err_msg:
            key_manager.quarantine_key(key_label, err_msg[:100])

        duration = round(time.time() - t0, 2)
        clean_err = analytics.clean_error_message(err_msg)
        status_code = getattr(e, "status_code", 500)
        try:
            status_code = int(status_code)
        except (ValueError, TypeError):
            status_code = 500

        diag_row = {
            "ts": now_iso(), "rid": rid, "m": model, "caller": caller_path, "os": caller_os,
            "key": key_label, "status": status_code, "dur": duration, "in": 0, "c_in": 0,
            "out": 0, "r_out": 0, "cost": 0.0, "tools_called": [], "err": clean_err
        }
        analytics.log_diagnostic(diag_row)
        asyncio.create_task(broadcast_telemetry(diag_row))
        return JSONResponse(status_code=status_code, content={"error": {"message": clean_err, "type": type(e).__name__}})

    # Streaming completion handler
    if is_stream:
        async def event_generator():
            usage = None
            called_tools = set()
            first_token_time = None
            try:
                async for chunk in response:
                    if first_token_time is None:
                        first_token_time = time.time() - t0
                    choices = getattr(chunk, "choices", None)
                    if choices:
                        delta = getattr(choices[0], "delta", None)
                        for tc in getattr(delta, "tool_calls", None) or []:
                            fn = getattr(tc, "function", None)
                            if fn and (name := getattr(fn, "name", None)):
                                called_tools.add(name)

                    if u := getattr(chunk, "usage", None):
                        usage = u
                    yield f"data: {chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"

                total_time = round(time.time() - t0, 2)
                diag = analytics.parse_usage_and_cost(usage, target_backend_model)
                finish_gemini(diag["in"] + diag["out"])

                if model in config.OPENROUTER_ROUTES:
                    controller.record_cost(diag["cost"], time.time())

                tool_list = sorted(called_tools)
                diag_row = {
                    "ts": now_iso(), "rid": rid, "m": model, "caller": caller_path, "os": caller_os,
                    "key": key_label, "status": 200, "dur": total_time, "ttft": round(first_token_time or 0.0, 2),
                    "tools_in": tools_in_count, "tool_names": tool_names, "tools_called": tool_list,
                    **diag, "err": None
                }
                analytics.log_diagnostic(diag_row)
                asyncio.create_task(broadcast_telemetry(diag_row))
                analytics.capture_incident_payload(rid, diag["cost"], diag["in"], {"body": body, "usage": diag})

            finally:
                finish_gemini()

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # Non-streaming completion handler
    data = response.model_dump()
    total_time = round(time.time() - t0, 2)
    diag = analytics.parse_usage_and_cost(data.get("usage"), target_backend_model)
    finish_gemini(diag["in"] + diag["out"])

    if model in config.OPENROUTER_ROUTES:
        controller.record_cost(diag["cost"], time.time())

    called_tools = []
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        called_tools = [tc.get("function", {}).get("name") for tc in msg.get("tool_calls") or [] if tc.get("function", {}).get("name")]

    diag_row = {
        "ts": now_iso(), "rid": rid, "m": model, "caller": caller_path, "os": caller_os,
        "key": key_label, "status": 200, "dur": total_time, "ttft": total_time,
        "tools_in": tools_in_count, "tool_names": tool_names, "tools_called": called_tools,
        **diag, "err": None
    }
    analytics.log_diagnostic(diag_row)
    asyncio.create_task(broadcast_telemetry(diag_row))
    analytics.capture_incident_payload(rid, diag["cost"], diag["in"], {"body": body, "response": data})

    return JSONResponse(content=data)

# -- dashboard and status routes --
@app.get("/")
@app.get("/usage")
async def dashboard_view(request: Request, day: Optional[str] = None):
    fmt = request.query_params.get("fmt")
    target_day = day or controller.get_today_str(time.time())

    if fmt == "text":
        return PlainTextResponse(dashboard.render_cli_text(target_day))
    
    if fmt == "json":
        rows = analytics.read_day_rows(target_day)
        by_model, by_caller, ranked_tools = analytics.summarize_rows(rows)
        return {
            "day": target_day,
            "spend": {
                "day_usd": round(controller.ledger["day_usd"], 4),
                "daily_cap": controller.daily_cap,
                "run_usd": round(controller.ledger["run_usd"], 4),
                "run_cap": controller.run_cap
            },
            "by_model": by_model,
            "by_caller": by_caller,
            "top_tools": ranked_tools
        }

    return HTMLResponse(dashboard.render_html_dashboard(target_day))

@app.get("/status")
async def system_status():
    now = time.time()
    return {
        "pool": key_manager.get_pool_status(now),
        "spend": {
            "day": controller.ledger["day"],
            "day_usd": round(controller.ledger["day_usd"], 4),
            "daily_cap": controller.daily_cap,
            "run_usd": round(controller.ledger["run_usd"], 4),
            "run_cap": controller.run_cap
        }
    }

if __name__ == "__main__":
    import uvicorn
    try:
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "4000")))
    except (KeyboardInterrupt, SystemExit):
        pass