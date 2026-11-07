import os
import time
import uuid
import json
import asyncio
from typing import Optional, Set
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse, HTMLResponse
import litellm

import config
import db
import analytics
import dashboard
from controller import controller
from key_manager import key_manager

# Suppress verbose debug output from LiteLLM
litellm.suppress_debug_info = True

# Active SSE client subscriber queues
_sse_subscribers: Set[asyncio.Queue] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initializes SQLite engine, restores fleet quarantine state, and preloads today's spend
    await db.init_db()
    await key_manager.init_fleet_state()
    controller.preload_spend()
    config.log.info("Horizon Proxy core services initialized")
    yield
    # Cleanly close database connections on shutdown
    await db.close_db()
    config.log.info("Horizon Proxy shut down cleanly")


app = FastAPI(title="Horizon Proxy", lifespan=lifespan)


async def broadcast_live_event(event_type: str, row: Optional[dict] = None):
    # Pushes real-time status and telemetry events to connected cockpit dashboard clients
    if not _sse_subscribers:
        return
    now = time.time()
    today_str = controller.get_today_str(now)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_iso = datetime.fromtimestamp(now - 60.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Fast indexed SQL count for active RPM
    try:
        active_rpm = await db.get_rolling_rpm(today_str, cutoff_iso)
    except Exception:
        active_rpm = 0

    payload = {
        "type": event_type,
        "row": row,
        "pool": key_manager.get_pool_status(now),
        "spend": {
            "day_usd": round(controller.ledger["day_usd"], 4),
            "daily_cap": controller.daily_cap,
            "run_usd": round(controller.ledger["run_usd"], 4),
            "run_cap": controller.run_cap,
        },
        "active_rpm": active_rpm,
    }
    dead = set()
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _sse_subscribers.difference_update(dead)


def estimate_tokens(body: dict) -> int:
    # Estimates input prompt tokens for pre-flight TPM reservations
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
    # Returns current UTC time formatted as standard ISO 8601 string
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Management & Control API Routes ---

@app.get("/health")
async def health():
    # Reports proxy health, active key status, and current spend cap posture
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
        "spend_today_usd": round(controller.ledger["day_usd"], 4),
    })


@app.get("/v1/models")
@app.get("/models")
async def list_models():
    # Returns available model identifiers in standard OpenAI format
    model_ids = list(config.OPENROUTER_ROUTES.keys()) + ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": int(time.time()), "owned_by": "horizon-proxy"} for m in model_ids],
    }


@app.post("/api/key/fallback")
async def set_key_fallback_route(request: Request):
    # Toggles Gemini 3.1 fallback routing for a specific key
    body = await request.json()
    key_label = body.get("key")
    fallback = bool(body.get("fallback", False))
    key_manager.set_key_fallback(key_label, fallback)
    await broadcast_live_event("heartbeat")
    return {"status": "ok", "key": key_label, "fallback": fallback}


@app.post("/api/key/unquarantine")
async def unquarantine_key_route(request: Request):
    # Revives a quarantined key in memory and persists change to SQLite
    body = await request.json()
    key_label = body.get("key")
    key_manager.unquarantine_key(key_label)
    await broadcast_live_event("heartbeat")
    return {"status": "ok", "key": key_label}


@app.post("/api/limits")
async def update_key_limits_route(request: Request):
    # Dynamically updates rate limiting and concurrency parameters
    body = await request.json()
    key_manager.update_limits(
        rpm=body.get("rpm"),
        tpm=body.get("tpm"),
        rpd=body.get("rpd"),
        max_parallel=body.get("max_parallel"),
    )
    await broadcast_live_event("heartbeat")
    return {"status": "ok", "limits": key_manager.get_pool_status(time.time())["limits"]}


@app.post("/api/spend/limits")
async def update_spend_limits_route(request: Request):
    # Updates daily and per-run spend limits in memory and SQLite spend_state
    body = await request.json()
    daily_usd = float(body.get("daily_usd", controller.daily_cap))
    run_usd = float(body.get("run_usd", controller.run_cap))
    controller.save_limits(daily_usd, run_usd)
    await broadcast_live_event("heartbeat")
    return {"status": "ok", "daily_cap": controller.daily_cap, "run_cap": controller.run_cap}


@app.get("/api/live")
async def sse_live_feed(request: Request):
    # Real-time Server-Sent Events stream powering the cockpit dashboard
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
                    "run_cap": controller.run_cap,
                },
                "active_rpm": 0,
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
                    cutoff_iso = datetime.fromtimestamp(now - 60.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    try:
                        active_rpm = await db.get_rolling_rpm(controller.ledger["day"], cutoff_iso)
                    except Exception:
                        active_rpm = 0

                    hb = {
                        "type": "heartbeat",
                        "pool": key_manager.get_pool_status(now),
                        "spend": {
                            "day_usd": round(controller.ledger["day_usd"], 4),
                            "daily_cap": controller.daily_cap,
                            "run_usd": round(controller.ledger["run_usd"], 4),
                            "run_cap": controller.run_cap,
                        },
                        "active_rpm": active_rpm,
                    }
                    yield f"data: {json.dumps(hb)}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            return
        finally:
            _sse_subscribers.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# --- Core Ingress Dispatch Endpoint ---

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Enforce Master Key authentication if configured in environment
    auth_header = request.headers.get("authorization", "")
    if config.MASTER_KEY and not analytics.verify_master_key(auth_header):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing Master Key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model = body.get("model")
    rid = uuid.uuid4().hex[:6]
    t0 = time.perf_counter()
    is_stream = bool(body.get("stream", False))
    key_label = "-"
    target_backend_model = model

    # Ingress identity ladder: detect caller role, project directory, and operating system
    client_host = request.client.host if request.client else "unknown"
    caller_type, caller_dir, caller_os = analytics.detect_caller_and_os(
        dict(request.headers), body, client_host
    )
    session_id, turn_index, tokens_by_role = analytics.derive_request_context(
        dict(request.headers), body
    )

    tool_names = sorted({(t.get("function") or {}).get("name", "?") for t in (body.get("tools") or []) if isinstance(t, dict)})
    tools_in_count = len(body.get("tools") or [])

    config.log.info(
        "[%s] -> %s stream=%s caller=[%s | %s | %s] session=%s turn=%d msgs=%d tools=%d",
        rid, model, is_stream, caller_type, caller_os, caller_dir,
        session_id[:8], turn_index, len(body.get("messages") or []), tools_in_count,
    )

    gemini_key_label = None
    gemini_reserved = 0
    gemini_recorded = False
    gate_held = False
    gate_wait_s = 0.0
    pace_wait_s = 0.0

    def finish_gemini(actual_tokens: Optional[int] = None):
        # Releases gate semaphore and updates key manager token reservations
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

    # Route 1: OpenRouter (Paid models with spend cap verification)
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

    # Route 2: Gemini Fleet (Free tier with concurrency gate)
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

        # Concurrency gate timer
        t_gate_start = time.perf_counter()
        await key_manager.acquire_gate()
        gate_wait_s = round(time.perf_counter() - t_gate_start, 3)
        gate_held = True

    else:
        config.log.warning("[%s] unknown model: %s", rid, model)
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")

    if is_stream:
        body.setdefault("stream_options", {"include_usage": True})

    max_retries = key_manager.num_keys if model.startswith("gemini") else 1
    excluded_keys = set()
    response = None

    for attempt in range(1, max_retries + 1):
        if model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
            gemini_reserved = estimate_tokens(body)
            try:
                key_label, key, pace_wait_s, use_fallback = await key_manager.acquire_key(
                    estimated_tokens=gemini_reserved, excluded_keys=excluded_keys
                )
            except RuntimeError as ex:
                finish_gemini()
                err_msg = str(ex)
                duration = round(time.perf_counter() - t0, 3)
                telemetry_row = {
                    "ts": now_iso(), "day": controller.ledger["day"], "rid": rid,
                    "session_id": session_id, "turn_index": turn_index,
                    "caller_type": caller_type, "caller_dir": caller_dir, "caller_os": caller_os,
                    "client_host": client_host, "model_requested": model, "model_dispatched": target_backend_model,
                    "is_stream": int(is_stream), "key_label": "DEAD", "key_attempts": attempt,
                    "duration_total_s": duration, "gate_wait_s": gate_wait_s, "pacing_wait_s": pace_wait_s,
                    "status_code": 503, "error_message": err_msg, "tokens_by_role": tokens_by_role,
                    "tools_in_count": tools_in_count, "tool_names_provided": tool_names,
                }
                await db.insert_request(telemetry_row)
                asyncio.create_task(broadcast_live_event("telemetry", telemetry_row))
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
            raw_status = getattr(e, "status_code", None)
            if raw_status is None and hasattr(e, "response") and hasattr(e.response, "status_code"):
                raw_status = e.response.status_code
            try:
                status_code = int(raw_status)
            except (ValueError, TypeError):
                status_code = 500

            clean_err = analytics.clean_error_message(err_msg)
            is_dead_key = (status_code in (401, 403) or "401" in err_msg or "403" in err_msg or "API_KEY_INVALID" in err_msg)

            if is_dead_key and model.startswith("gemini"):
                duration = round(time.perf_counter() - t0, 3)
                config.log.warning("[%s] Gemini key %s failed (%s). Quarantining and retrying...", rid, key_label, status_code)
                key_manager.quarantine_key(key_label, clean_err)
                key_manager.record_error(key_label)
                excluded_keys.add(key_label)

                telemetry_row = {
                    "ts": now_iso(), "day": controller.ledger["day"], "rid": rid,
                    "session_id": session_id, "turn_index": turn_index,
                    "caller_type": caller_type, "caller_dir": caller_dir, "caller_os": caller_os,
                    "client_host": client_host, "model_requested": model, "model_dispatched": target_backend_model,
                    "is_stream": int(is_stream), "key_label": key_label, "key_attempts": attempt,
                    "key_quarantined": 1, "quarantine_reason": clean_err,
                    "duration_total_s": duration, "gate_wait_s": gate_wait_s, "pacing_wait_s": pace_wait_s,
                    "status_code": status_code, "error_message": f"Key {key_label} quarantined: {clean_err}",
                    "tokens_by_role": tokens_by_role, "tools_in_count": tools_in_count, "tool_names_provided": tool_names,
                }
                await db.insert_request(telemetry_row)
                asyncio.create_task(broadcast_live_event("telemetry", telemetry_row))

                if gemini_key_label:
                    key_manager.release_reservation(gemini_key_label, gemini_reserved)
                    gemini_key_label = None
                continue

            if gemini_key_label:
                key_manager.record_error(gemini_key_label)
            finish_gemini()
            duration = round(time.perf_counter() - t0, 3)

            config.log.error("[%s] !! %s key=%s status=%s %s | %.2fs | %s",
                             rid, model, key_label, status_code, type(e).__name__, duration, clean_err)

            telemetry_row = {
                "ts": now_iso(), "day": controller.ledger["day"], "rid": rid,
                "session_id": session_id, "turn_index": turn_index,
                "caller_type": caller_type, "caller_dir": caller_dir, "caller_os": caller_os,
                "client_host": client_host, "model_requested": model, "model_dispatched": target_backend_model,
                "is_stream": int(is_stream), "key_label": key_label, "key_attempts": attempt,
                "duration_total_s": duration, "gate_wait_s": gate_wait_s, "pacing_wait_s": pace_wait_s,
                "status_code": status_code, "error_message": clean_err,
                "tokens_by_role": tokens_by_role, "tools_in_count": tools_in_count, "tool_names_provided": tool_names,
            }
            await db.insert_request(telemetry_row)
            asyncio.create_task(broadcast_live_event("telemetry", telemetry_row))
            return JSONResponse(status_code=status_code, content={"error": {"message": clean_err, "type": type(e).__name__}})

    if response is None:
        finish_gemini()
        raise HTTPException(status_code=503, detail="All healthy Gemini keys failed.")

    # --- Streaming Response Handling ---
    if is_stream:
        async def event_generator():
            usage = None
            called_tools = set()
            first_token_time = None
            accumulated_content = []
            finish_reason = "stop"

            try:
                async for chunk in response:
                    if first_token_time is None:
                        first_token_time = round(time.perf_counter() - t0, 3)
                    choices = getattr(chunk, "choices", None)
                    if choices:
                        choice = choices[0]
                        if fr := getattr(choice, "finish_reason", None):
                            finish_reason = fr
                        delta = getattr(choice, "delta", None)
                        if delta:
                            if c := getattr(delta, "content", None):
                                accumulated_content.append(c)
                            for tc in getattr(delta, "tool_calls", None) or []:
                                fn = getattr(tc, "function", None)
                                if fn and (name := getattr(fn, "name", None)):
                                    called_tools.add(name)

                    if u := getattr(chunk, "usage", None):
                        usage = u
                    yield f"data: {chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"

                total_time = round(time.perf_counter() - t0, 3)
                ttft_s = first_token_time or total_time
                diag = analytics.parse_usage_and_cost(usage, target_backend_model)
                finish_gemini(diag["in"] + diag["out"])

                if model in config.OPENROUTER_ROUTES:
                    controller.record_cost(diag["cost"], time.time())

                tool_list = sorted(called_tools)
                active_gen_time = max(0.001, total_time - ttft_s)
                tps = round(diag["out"] / active_gen_time, 1) if diag["out"] > 0 else 0.0

                config.log.info(
                    "[%s] <- 200 stream %s key=%s cost=$%.5f in=%d (c=%d) out=%d (r=%d) tps=%.1f called=%s total=%.2fs",
                    rid, model, key_label, diag["cost"], diag["in"], diag["c_in"],
                    diag["out"], diag["r_out"], tps, tool_list, total_time
                )

                telemetry_row = {
                    "ts": now_iso(), "day": controller.ledger["day"], "rid": rid,
                    "session_id": session_id, "turn_index": turn_index,
                    "caller_type": caller_type, "caller_dir": caller_dir, "caller_os": caller_os,
                    "client_host": client_host, "model_requested": model, "model_dispatched": target_backend_model,
                    "is_stream": 1, "key_label": key_label, "duration_total_s": total_time,
                    "gate_wait_s": gate_wait_s, "pacing_wait_s": pace_wait_s, "ttft_s": ttft_s,
                    "tokens_per_second": tps, "status_code": 200, "finish_reason": finish_reason,
                    "client_aborted": 0, "error_message": None,
                    "tokens_prompt_total": diag["in"], "tokens_prompt_cached": diag["c_in"],
                    "tokens_prompt_uncached": max(0, diag["in"] - diag["c_in"]),
                    "tokens_completion": diag["out"], "tokens_reasoning": diag["r_out"],
                    "tokens_by_role": tokens_by_role, "tools_in_count": tools_in_count,
                    "tool_names_provided": tool_names, "tools_called": tool_list,
                    "cost_usd": diag["cost"], "spend_accum_day_usd": controller.ledger["day_usd"],
                    "spend_accum_run_usd": controller.ledger["run_usd"],
                }
                await db.insert_request(telemetry_row)
                asyncio.create_task(broadcast_live_event("telemetry", telemetry_row))

                # Check incident blackbox capture thresholds
                if diag["cost"] >= config.HIGH_COST_THRESHOLD_USD or diag["in"] >= config.HIGH_TOKEN_THRESHOLD:
                    await db.save_incident(rid, diag["cost"], diag["in"], {
                        "headers": {k: config.scrub(v) for k, v in request.headers.items()},
                        "body": body,
                        "response": {"content": "".join(accumulated_content), "tools": tool_list},
                        "usage": diag,
                        "cost_breakdown": config.PRICING.get(target_backend_model, (0, 0, 0)),
                    })

            except (asyncio.CancelledError, GeneratorExit):
                # 499: Client disconnected or cancelled stream mid-generation
                total_time = round(time.perf_counter() - t0, 3)
                config.log.warning("[%s] 499 Client disconnected mid-stream %s after %.2fs", rid, model, total_time)
                diag = analytics.parse_usage_and_cost(usage, target_backend_model)

                abort_row = {
                    "ts": now_iso(), "day": controller.ledger["day"], "rid": rid,
                    "session_id": session_id, "turn_index": turn_index,
                    "caller_type": caller_type, "caller_dir": caller_dir, "caller_os": caller_os,
                    "client_host": client_host, "model_requested": model, "model_dispatched": target_backend_model,
                    "is_stream": 1, "key_label": key_label, "duration_total_s": total_time,
                    "gate_wait_s": gate_wait_s, "pacing_wait_s": pace_wait_s,
                    "ttft_s": first_token_time or total_time, "status_code": 499,
                    "finish_reason": "cancelled", "client_aborted": 1,
                    "error_message": "Client disconnected mid-stream",
                    "tokens_prompt_total": diag["in"], "tokens_prompt_cached": diag["c_in"],
                    "tokens_completion": diag["out"], "tokens_reasoning": diag["r_out"],
                    "tokens_by_role": tokens_by_role, "tools_in_count": tools_in_count,
                    "tool_names_provided": tool_names, "tools_called": sorted(called_tools),
                    "cost_usd": diag["cost"],
                }
                try:
                    await db.insert_request(abort_row)
                    asyncio.create_task(broadcast_live_event("telemetry", abort_row))
                except Exception:
                    pass
                raise
            finally:
                finish_gemini()

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # --- Non-Streaming Response Handling ---
    try:
        data = response.model_dump()
        total_time = round(time.perf_counter() - t0, 3)
        diag = analytics.parse_usage_and_cost(data.get("usage"), target_backend_model)
        finish_gemini(diag["in"] + diag["out"])

        if model in config.OPENROUTER_ROUTES:
            controller.record_cost(diag["cost"], time.time())

        called_tools = []
        finish_reason = "stop"
        choices = data.get("choices") or []
        if choices:
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or "stop"
            msg = choice.get("message") or {}
            called_tools = [
                tc.get("function", {}).get("name")
                for tc in msg.get("tool_calls") or []
                if tc.get("function", {}).get("name")
            ]

        active_gen_time = max(0.001, total_time)
        tps = round(diag["out"] / active_gen_time, 1) if diag["out"] > 0 else 0.0

        config.log.info(
            "[%s] <- 200 %s key=%s cost=$%.5f in=%d (c=%d) out=%d (r=%d) called=%s total=%.2fs",
            rid, model, key_label, diag["cost"], diag["in"], diag["c_in"],
            diag["out"], diag["r_out"], called_tools, total_time
        )

        telemetry_row = {
            "ts": now_iso(), "day": controller.ledger["day"], "rid": rid,
            "session_id": session_id, "turn_index": turn_index,
            "caller_type": caller_type, "caller_dir": caller_dir, "caller_os": caller_os,
            "client_host": client_host, "model_requested": model, "model_dispatched": target_backend_model,
            "is_stream": 0, "key_label": key_label, "duration_total_s": total_time,
            "gate_wait_s": gate_wait_s, "pacing_wait_s": pace_wait_s, "ttft_s": total_time,
            "tokens_per_second": tps, "status_code": 200, "finish_reason": finish_reason,
            "client_aborted": 0, "error_message": None,
            "tokens_prompt_total": diag["in"], "tokens_prompt_cached": diag["c_in"],
            "tokens_prompt_uncached": max(0, diag["in"] - diag["c_in"]),
            "tokens_completion": diag["out"], "tokens_reasoning": diag["r_out"],
            "tokens_by_role": tokens_by_role, "tools_in_count": tools_in_count,
            "tool_names_provided": tool_names, "tools_called": called_tools,
            "cost_usd": diag["cost"], "spend_accum_day_usd": controller.ledger["day_usd"],
            "spend_accum_run_usd": controller.ledger["run_usd"],
        }
        await db.insert_request(telemetry_row)
        asyncio.create_task(broadcast_live_event("telemetry", telemetry_row))

        if diag["cost"] >= config.HIGH_COST_THRESHOLD_USD or diag["in"] >= config.HIGH_TOKEN_THRESHOLD:
            await db.save_incident(rid, diag["cost"], diag["in"], {
                "headers": {k: config.scrub(v) for k, v in request.headers.items()},
                "body": body,
                "response": data,
                "usage": diag,
                "cost_breakdown": config.PRICING.get(target_backend_model, (0, 0, 0)),
            })

        return JSONResponse(content=data)
    finally:
        finish_gemini()


# --- Dashboard Views ---

@app.get("/")
@app.get("/usage")
async def dashboard_view(request: Request, day: Optional[str] = None):
    # Renders CLI text or HTML cockpit dashboard, supporting both sync and async renderers
    fmt = request.query_params.get("fmt")
    target_day = day or controller.get_today_str(time.time())

    user_agent = request.headers.get("user-agent", "").lower()
    if fmt == "text" or ("curl" in user_agent and fmt != "html"):
        cli_res = dashboard.render_cli_text(target_day)
        if asyncio.iscoroutine(cli_res):
            cli_res = await cli_res
        return PlainTextResponse(cli_res)

    if fmt == "json":
        rows = await db.get_day_requests(target_day)
        by_model, by_caller, by_agent, ranked_tools = analytics.summarize_rows(rows)
        day_entries = await db.get_available_days()
        available_days = [item["day"] for item in day_entries]
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
                "run_cap": controller.run_cap,
            },
            "by_model": by_model,
            "by_caller": by_caller,
            "by_agent": by_agent,
            "tool_calls_ranked": ranked_tools,
            "top_tools": ranked_tools,
            "pool": key_manager.get_pool_status(time.time()),
        }

    html_res = dashboard.render_html_dashboard(target_day)
    if asyncio.iscoroutine(html_res):
        html_res = await html_res
    return HTMLResponse(html_res)


@app.get("/status")
async def system_status():
    # Lightweight health and budget inspection endpoint
    now = time.time()
    return {
        "pool": key_manager.get_pool_status(now),
        "spend": {
            "day": controller.ledger["day"],
            "day_usd": round(controller.ledger["day_usd"], 4),
            "daily_cap": controller.daily_cap,
            "run_usd": round(controller.ledger["run_usd"], 4),
            "run_cap": controller.run_cap,
        },
    }


if __name__ == "__main__":
    import uvicorn
    try:
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "4000")))
    except (KeyboardInterrupt, SystemExit):
        pass