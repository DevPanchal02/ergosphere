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

_sse_subscribers: Set[asyncio.Queue] = set()

async def broadcast_live_event(event_type: str, row: Optional[dict] = None):
    if not _sse_subscribers:
        return
    now = time.time()
    rows = analytics.read_day_rows(controller.get_today_str(now))
    payload = {
        "type": event_type,
        "row": row,
        "pool": key_manager.get_pool_status(now),
        "spend": {
            "day_usd": round(controller.ledger["day_usd"], 4),
            "daily_cap": controller.daily_cap,
            "run_usd": round(controller.ledger["run_usd"], 4),
            "run_cap": controller.run_cap
        },
        "active_rpm": analytics.get_rolling_rpm(rows, now)
    }
    dead = set()
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _sse_subscribers.difference_update(dead)

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

@app.get("/health")
async def health():
    now = time.time()
    healthy_count = sum(1 for q in key_manager.quarantined if not q)

    if key_manager.num_keys > 0 and healthy_count == 0:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "reason": "All Gemini keys quarantined"})

    pool = key_manager.get_pool_status(now)
    spend_blocked = bool(controller.check_spend(now))

    return JSONResponse(status_code=200, content={
        "status": "healthy",
        "keys_healthy": f"{healthy_count}/{key_manager.num_keys}",
        "keys_alive": f"{healthy_count}/{key_manager.num_keys}",
        "tpm_available": pool["pool_tpm_available"],
        "openrouter_configured": bool(config.OPENROUTER_KEY),
        "spend_capped": spend_blocked,
        "spend_today_usd": round(controller.ledger["day_usd"], 4)
    })

@app.get("/v1/models")
@app.get("/models")
async def list_models():
    model_ids = list(config.OPENROUTER_ROUTES.keys()) + ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": int(time.time()), "owned_by": "horizon-proxy"} for m in model_ids],
    }

# -- Fleet and key control API endpoints --
@app.post("/api/key/fallback")
async def set_key_fallback_route(request: Request):
    body = await request.json()
    key_label = body.get("key")
    fallback = bool(body.get("fallback", False))
    key_manager.set_key_fallback(key_label, fallback)
    await broadcast_live_event("heartbeat")
    return {"status": "ok", "key": key_label, "fallback": fallback}

@app.post("/api/key/unquarantine")
async def unquarantine_key_route(request: Request):
    body = await request.json()
    key_label = body.get("key")
    key_manager.unquarantine_key(key_label)
    await broadcast_live_event("heartbeat")
    return {"status": "ok", "key": key_label}

# -- Real-time SSE live stream --
@app.get("/api/live")
async def sse_live_feed(request: Request):
    queue = asyncio.Queue(maxsize=100)
    _sse_subscribers.add(queue)

    async def event_generator():
        try:
            now = time.time()
            init_payload = {
                "type": "heartbeat",
                "pool": key_manager.get_pool_status(now),
                "spend": {
                    "day_usd": round(controller.ledger["day_usd"], 4),
                    "daily_cap": controller.daily_cap,
                    "run_usd": round(controller.ledger["run_usd"], 4),
                    "run_cap": controller.run_cap
                },
                "active_rpm": 0
            }
            yield f"data: {json.dumps(init_payload)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=2.5)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    now = time.time()
                    rows = analytics.read_day_rows(controller.get_today_str(now))
                    hb = {
                        "type": "heartbeat",
                        "pool": key_manager.get_pool_status(now),
                        "spend": {
                            "day_usd": round(controller.ledger["day_usd"], 4),
                            "daily_cap": controller.daily_cap,
                            "run_usd": round(controller.ledger["run_usd"], 4),
                            "run_cap": controller.run_cap
                        },
                        "active_rpm": analytics.get_rolling_rpm(rows, now)
                    }
                    yield f"data: {json.dumps(hb)}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            return
        finally:
            _sse_subscribers.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )

# -- Chat completions proxy --
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

    client_host = request.client.host if request.client else "unknown"
    caller_path, caller_os = analytics.detect_caller_and_os(dict(request.headers), body, client_host)

    tool_names = sorted({(t.get("function") or {}).get("name", "?") for t in (body.get("tools") or []) if isinstance(t, dict)})
    tools_in_count = len(body.get("tools") or [])

    config.log.info("[%s] -> %s stream=%s caller=[%s|%s] msgs=%d tools=%d",
                    rid, model, is_stream, caller_os, caller_path, len(body.get("messages") or []), tools_in_count)

    gemini_key_label = None
    gemini_reserved = 0
    gemini_recorded = False
    gate_held = False

    def finish_gemini(actual_tokens: Optional[int] = None):
        nonlocal gemini_recorded, gate_held
        if gate_held:
            gate_held = False
            key_manager.release_gate()
        if gemini_key_label and not gemini_recorded:
            gemini_recorded = True
            if actual_tokens is not None and actual_tokens > 0:
                key_manager.record_usage(gemini_key_label, gemini_reserved, actual_tokens)
            else:
                key_manager.release_reservation(gemini_key_label, gemini_reserved)

    # Route 1: OpenRouter
    if model in config.OPENROUTER_ROUTES:
        rejection = controller.check_spend(time.time())
        if rejection:
            config.log.warning("[%s] 402 SPEND CAP: %s", rid, rejection)
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

    # Route 2: Gemini
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

        await key_manager.acquire_gate()
        gate_held = True

    else:
        config.log.warning("[%s] unknown model: %s", rid, model)
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")

    if is_stream:
        body.setdefault("stream_options", {"include_usage": True})

    max_retries = key_manager.num_keys if model.startswith("gemini") else 1
    excluded_keys = set()
    response = None

    for _ in range(max_retries):
        if model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
            gemini_reserved = estimate_tokens(body)
            try:
                key_label, key, pace_wait, use_fallback = await key_manager.acquire_key(
                    estimated_tokens=gemini_reserved, excluded_keys=excluded_keys
                )
            except RuntimeError as ex:
                finish_gemini()
                err_msg = str(ex)
                duration = round(time.time() - t0, 2)
                diag_row = {
                    "ts": now_iso(), "rid": rid, "m": model, "caller": caller_path, "os": caller_os,
                    "key": "DEAD", "status": 503, "dur": duration, "in": 0, "c_in": 0,
                    "out": 0, "r_out": 0, "cost": 0.0, "tools_in": tools_in_count, "tool_names": tool_names,
                    "tools_called": [], "err": err_msg
                }
                analytics.log_diagnostic(diag_row)
                asyncio.create_task(broadcast_live_event("telemetry", diag_row))
                return JSONResponse(status_code=503, content={"error": {"message": err_msg, "type": "FleetExhausted"}})

            gemini_key_label = key_label
            target_backend_model = config.FALLBACK_GEMINI_MODEL if (use_fallback or model == "gemini-3.1-flash-lite") else config.PRIMARY_GEMINI_MODEL
            body["model"] = target_backend_model
            body["api_key"] = key

        try:
            response = await litellm.acompletion(**body)
            break
        except Exception as e:
            err_msg = str(e)
            if ("401" in err_msg or "403" in err_msg) and model.startswith("gemini"):
                config.log.warning("[%s] Gemini key %s failed (401/403). Quarantining and retrying...", rid, key_label)
                key_manager.quarantine_key(key_label, err_msg[:120])
                key_manager.record_error(key_label)
                excluded_keys.add(key_label)
                if gemini_key_label:
                    key_manager.release_reservation(gemini_key_label, gemini_reserved)
                    gemini_key_label = None
                continue

            if gemini_key_label:
                key_manager.record_error(gemini_key_label)
            finish_gemini()
            duration = round(time.time() - t0, 2)
            clean_err = analytics.clean_error_message(err_msg)
            status_code = getattr(e, "status_code", 500)
            try:
                status_code = int(status_code)
            except (ValueError, TypeError):
                status_code = 500

            config.log.error("[%s] !! %s key=%s status=%s %s | %.1fs | %s",
                             rid, model, key_label, status_code, type(e).__name__, duration, clean_err)

            diag_row = {
                "ts": now_iso(), "rid": rid, "m": model, "caller": caller_path, "os": caller_os,
                "key": key_label, "status": status_code, "dur": duration, "in": 0, "c_in": 0,
                "out": 0, "r_out": 0, "cost": 0.0, "tools_in": tools_in_count, "tool_names": tool_names,
                "tools_called": [], "err": clean_err
            }
            analytics.log_diagnostic(diag_row)
            asyncio.create_task(broadcast_live_event("telemetry", diag_row))
            return JSONResponse(status_code=status_code, content={"error": {"message": clean_err, "type": type(e).__name__}})

    if response is None:
        finish_gemini()
        raise HTTPException(status_code=503, detail="All healthy Gemini keys failed.")

    # Streaming completion
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
                config.log.info(
                    "[%s] <- 200 stream %s key=%s cost=$%.5f in=%d (c=%d) out=%d (r=%d) called=%s total=%.1fs",
                    rid, model, key_label, diag["cost"], diag["in"], diag["c_in"],
                    diag["out"], diag["r_out"], tool_list, total_time
                )

                diag_row = {
                    "ts": now_iso(), "rid": rid, "m": model, "caller": caller_path, "os": caller_os,
                    "key": key_label, "status": 200, "dur": total_time, "ttft": round(first_token_time or 0.0, 2),
                    "tools_in": tools_in_count, "tool_names": tool_names, "tools_called": tool_list,
                    **diag, "err": None
                }
                analytics.log_diagnostic(diag_row)
                asyncio.create_task(broadcast_live_event("telemetry", diag_row))
                analytics.capture_incident_payload(rid, diag["cost"], diag["in"], {"body": body, "usage": diag})

            finally:
                finish_gemini()

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # Non-streaming completion
    try:
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

        config.log.info(
            "[%s] <- 200 %s key=%s cost=$%.5f in=%d (c=%d) out=%d (r=%d) called=%s total=%.1fs",
            rid, model, key_label, diag["cost"], diag["in"], diag["c_in"],
            diag["out"], diag["r_out"], called_tools, total_time
        )

        diag_row = {
            "ts": now_iso(), "rid": rid, "m": model, "caller": caller_path, "os": caller_os,
            "key": key_label, "status": 200, "dur": total_time, "ttft": total_time,
            "tools_in": tools_in_count, "tool_names": tool_names, "tools_called": called_tools,
            **diag, "err": None
        }
        analytics.log_diagnostic(diag_row)
        asyncio.create_task(broadcast_live_event("telemetry", diag_row))
        analytics.capture_incident_payload(rid, diag["cost"], diag["in"], {"body": body, "response": data})

        return JSONResponse(content=data)
    finally:
        finish_gemini()

# -- Dashboard endpoints --
@app.get("/")
@app.get("/usage")
async def dashboard_view(request: Request, day: Optional[str] = None):
    fmt = request.query_params.get("fmt")
    target_day = day or controller.get_today_str(time.time())

    user_agent = request.headers.get("user-agent", "").lower()
    if fmt == "text" or ("curl" in user_agent and fmt != "html"):
        return PlainTextResponse(dashboard.render_cli_text(target_day))

    if fmt == "json":
        rows = analytics.read_day_rows(target_day)
        by_model, by_caller, by_agent, ranked_tools = analytics.summarize_rows(rows)
        available_days = [item["day"] for item in dashboard.get_available_log_days()]
        return {
            "day": target_day,
            "tz": config.TZ_NOTE,
            "day_usd": round(controller.ledger["day_usd"], 4),
            "daily_cap": controller.daily_cap,
            "available_days": available_days,
            "spend": {
                "day_usd": round(controller.ledger["day_usd"], 4),
                "daily_cap": controller.daily_cap,
                "run_usd": round(controller.ledger["run_usd"], 4),
                "run_cap": controller.run_cap
            },
            "by_model": by_model,
            "by_caller": by_caller,
            "by_agent": by_agent,
            "tool_calls_ranked": ranked_tools,
            "top_tools": ranked_tools,
            "pool": key_manager.get_pool_status(time.time())
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