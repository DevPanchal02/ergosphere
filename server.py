import os
import re
import glob
import json
import time
import uuid
import asyncio
import logging
from typing import Optional
from collections import defaultdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import litellm

litellm.suppress_debug_info = True

# -- logging --
_fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
log = logging.getLogger("pacer")
log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
log.propagate = False
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)
if os.getenv("LOG_FILE"):
    try:
        _fh = RotatingFileHandler(os.environ["LOG_FILE"], maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        _fh.setFormatter(_fmt)
        log.addHandler(_fh)
    except OSError as e:
        log.warning("LOG_FILE unusable (%s); logging to stdout only", e)

# -- config --
app = FastAPI(title="Horizon Proxy")
security = HTTPBearer(auto_error=False)

MASTER_KEY = os.getenv("MASTER_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

_start_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
DIAGNOSTICS_FILE = os.path.join(LOGS_DIR, f"diagnostics_{_start_time}.jsonl")

GEMINI_KEYS = [(i, k) for i in range(1, 9) if (k := os.getenv(f"GEMINI_KEY_{i}"))]
KEY_COOLDOWN = float(os.getenv("GEMINI_KEY_COOLDOWN", "50"))
GEMINI_PARALLEL = int(os.getenv("GEMINI_PARALLEL", "2"))

# Spend governor (paid OpenRouter routes only; Gemini is free and never blocked).
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "3.00"))   # hard cap per local day, 0 = off
RUN_BUDGET_USD = float(os.getenv("RUN_BUDGET_USD", "1.50"))       # cap per "run", 0 = off
RUN_IDLE_SECONDS = float(os.getenv("RUN_IDLE_SECONDS", "900"))    # a run ends after this much inactivity
BUDGET_TZ = os.getenv("BUDGET_TZ", "America/Toronto")             # where "a day" starts and ends
MAX_OUT_TOKENS = int(os.getenv("MAX_OUT_TOKENS", "32000"))        # clamp per-request output on paid routes, 0 = off

key_index = 0
key_last_used = [0.0] * len(GEMINI_KEYS)
key_lock = asyncio.Lock()
gemini_gate = asyncio.Semaphore(GEMINI_PARALLEL)
stats = {"gemini_inflight": 0}

# Exact pricing per 1M tokens: (uncached_in, cached_in, out)
PRICING = {
    "openrouter/xiaomi/mimo-v2.6-pro": (0.435, 0.00435, 0.87),
    "openrouter/openai/gpt-6.1-sol":    (2.00,  0.10,    10.00),
    "gemini/gemini-3.5-flash-lite":    (0.00,  0.00,     0.00),
}

OPENROUTER_ROUTES = {
    "mimo-v2.6-pro": ("openrouter/xiaomi/mimo-v2.6-pro", None),
    "gpt-6.1-sol-medium": ("openrouter/openai/gpt-6.1-sol", "medium"),
    "gpt-6.1-sol-high":   ("openrouter/openai/gpt-6.1-sol", "high"),
    "gpt-6.1-sol-x-high": ("openrouter/openai/gpt-6.1-sol", "xhigh"),
}

# -- spend ledger --
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(BUDGET_TZ)
    _tz_note = BUDGET_TZ
except Exception:
    TZ = timezone.utc
    _tz_note = f"UTC (zone '{BUDGET_TZ}' unavailable; install tzdata in the image for local days)"


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def row_day(entry: dict) -> Optional[str]:
    """Local day of a diagnostics row (rows store UTC)."""
    try:
        dt = datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime("%Y-%m-%d")
    except (KeyError, ValueError, TypeError):
        return None


def read_rows(day: Optional[str] = None):
    """All diagnostics rows (optionally only one local day) across every diagnostics file."""
    rows = []
    for path in sorted(glob.glob(os.path.join(LOGS_DIR, "diagnostics_*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if day is None or row_day(e) == day:
                        rows.append(e)
        except OSError:
            continue
    return rows


ledger = {"day": day_of(time.time()), "day_usd": 0.0, "run_usd": 0.0, "run_last": 0.0, "run_n": 0}


def preload_ledger():
    """A proxy restart must not reset the daily cap: rebuild today's spend from disk."""
    rows = read_rows(ledger["day"])
    ledger["day_usd"] = round(sum(float(e.get("cost") or 0.0) for e in rows), 5)
    return len(rows)


def _roll(now: float):
    d = day_of(now)
    if d != ledger["day"]:
        ledger.update(day=d, day_usd=0.0)
    if ledger["run_last"] and now - ledger["run_last"] > RUN_IDLE_SECONDS:
        ledger.update(run_usd=0.0, run_n=0, run_last=0.0)


def budget_check(now: float) -> Optional[str]:
    """Return a refusal message if a paid request must be blocked, else touch the run clock."""
    _roll(now)
    if DAILY_BUDGET_USD and ledger["day_usd"] >= DAILY_BUDGET_USD:
        return (f"Daily cap ${DAILY_BUDGET_USD:.2f} reached (${ledger['day_usd']:.2f} spent today, {_tz_note}). "
                f"It resets at local midnight, or raise DAILY_BUDGET_USD and restart the proxy.")
    if RUN_BUDGET_USD and ledger["run_usd"] >= RUN_BUDGET_USD:
        return (f"Run cap ${RUN_BUDGET_USD:.2f} reached (${ledger['run_usd']:.2f} this run, {ledger['run_n']} requests). "
                f"A run ends after {RUN_IDLE_SECONDS / 60:.0f} idle minutes; restart the proxy to start a fresh run now.")
    ledger["run_last"] = now
    return None


def record_cost(cost: float, now: float):
    _roll(now)
    ledger["day_usd"] += cost
    ledger["run_usd"] += cost
    ledger["run_n"] += 1
    ledger["run_last"] = now


_preloaded = preload_ledger()
log.info(
    "pacer up | gemini_keys=%d [%s] cooldown=%.0fs parallel=%d | openrouter_key=%s | auth=%s | diag_file=%s",
    len(GEMINI_KEYS), ",".join(f"G{n}" for n, _ in GEMINI_KEYS), KEY_COOLDOWN, GEMINI_PARALLEL,
    "yes" if OPENROUTER_KEY else "MISSING", "on" if MASTER_KEY else "OFF", DIAGNOSTICS_FILE
)
dashboard_url = f"http://localhost:4000/{MASTER_KEY}" if MASTER_KEY else "http://localhost:4000/usage"
log.info("==================================================================")
log.info("📊 LIVE RECEIPT DASHBOARD: %s", dashboard_url)
log.info("==================================================================")
log.info(
    "budget | daily=$%.2f run=$%.2f idle=%.0fs max_out=%d tz=%s | today so far=$%.4f (%d rows preloaded) | logs dir must be a mounted volume or this resets on container recreate",
    DAILY_BUDGET_USD, RUN_BUDGET_USD, RUN_IDLE_SECONDS, MAX_OUT_TOKENS, _tz_note, ledger["day_usd"], _preloaded
)

# -- helpers --
_SECRETS = [s for s in [MASTER_KEY, OPENROUTER_KEY, *[k for _, k in GEMINI_KEYS]] if s]
_KEY_PATTERNS = re.compile(r"AIza[\w-]{30,}|sk-or-[\w-]+")


def scrub(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return _KEY_PATTERNS.sub("***", text)


def parse_usage_and_cost(u, target_model: str):
    """Extracts prompt, cached, completion, reasoning tokens, and calculates USD cost."""
    if not u:
        return {"in": 0, "c_in": 0, "out": 0, "r_out": 0, "cost": 0.0}
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))

    prompt_tok = get("prompt_tokens") or 0
    comp_tok = get("completion_tokens") or 0

    p_det = get("prompt_tokens_details")
    cached_tok = 0
    if p_det:
        cached_tok = (p_det.get("cached_tokens") if isinstance(p_det, dict) else getattr(p_det, "cached_tokens", 0)) or 0

    c_det = get("completion_tokens_details")
    reason_tok = 0
    if c_det:
        reason_tok = (c_det.get("reasoning_tokens") if isinstance(c_det, dict) else getattr(c_det, "reasoning_tokens", 0)) or 0

    prices = PRICING.get(target_model, (0.0, 0.0, 0.0))
    uncached_in = max(0, prompt_tok - cached_tok)
    cost = (uncached_in * prices[0] + cached_tok * prices[1] + comp_tok * prices[2]) / 1_000_000.0

    return {
        "in": prompt_tok,
        "c_in": cached_tok,
        "out": comp_tok,
        "r_out": reason_tok,
        "cost": round(cost, 5),
    }


def log_diagnostic(entry: dict):
    """Appends a single ultra-compact line of JSON to the diagnostics file."""
    try:
        line = json.dumps(entry, separators=(",", ":"))
        with open(DIAGNOSTICS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as ex:
        log.warning("failed to write diagnostic: %s", ex)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def verify_token(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if MASTER_KEY:
        if not credentials or credentials.credentials != MASTER_KEY:
            log.warning("401 invalid master key from %s", request.client.host if request.client else "?")
            raise HTTPException(status_code=401, detail="Invalid Master API Key")


async def verify_usage_access(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """/usage is opened in a browser, which cannot send a Bearer header, so ?key= is accepted too."""
    if not MASTER_KEY:
        return
    if credentials and credentials.credentials == MASTER_KEY:
        return
    if request.query_params.get("key") == MASTER_KEY:
        return
    raise HTTPException(status_code=401, detail="Invalid Master API Key")


async def get_next_gemini_key():
    global key_index
    if not GEMINI_KEYS:
        raise HTTPException(status_code=503, detail="No Gemini keys configured")
    async with key_lock:
        pos = key_index
        key_index = (key_index + 1) % len(GEMINI_KEYS)
        now = time.time()
        start_at = max(now, key_last_used[pos] + KEY_COOLDOWN)
        key_last_used[pos] = start_at
    wait = start_at - now
    if wait > 0:
        await asyncio.sleep(wait)
    n, key = GEMINI_KEYS[pos]
    return f"G{n}", key, max(wait, 0.0)


def error_response(rid, model, key_label, e, t0, tools_offered, tool_names):
    raw_status = getattr(e, "status_code", None)
    try:
        status = int(raw_status)
    except (ValueError, TypeError):
        status = 500
    msg = scrub(str(e))
    duration = round(time.time() - t0, 2)
    log.error(
        "[%s] !! %s key=%s status=%s %s | total=%.1fs | %s",
        rid, model, key_label, status, type(e).__name__, duration, msg[:300].replace("\n", " "),
    )
    log_diagnostic({
        "ts": now_iso(),
        "rid": rid, "m": model, "key": key_label, "status": status,
        "dur": duration, "tools_in": tools_offered, "tool_names": tool_names, "tools_called": [],
        "in": 0, "c_in": 0, "out": 0, "r_out": 0, "cost": 0.0,
        "err": f"{type(e).__name__}: {msg[:100]}"
    })
    return JSONResponse(status_code=status, content={"error": {"message": msg, "type": type(e).__name__}})


# -- usage report --
def _bucket():
    return {"reqs": 0, "usd": 0.0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "errors": 0}


def summarize(rows):
    by_model, by_agent = defaultdict(_bucket), defaultdict(_bucket)
    for e in rows:
        m = e.get("m", "?")
        names = e.get("tool_names")
        sig = ",".join(names) if names else f"{e.get('tools_in', 0)} tools"
        for key, table in ((m, by_model), (f"{m} | {sig}", by_agent)):
            b = table[key]
            b["reqs"] += 1
            b["usd"] += float(e.get("cost") or 0.0)
            b["in"] += int(e.get("in") or 0)
            b["cached"] += int(e.get("c_in") or 0)
            b["out"] += int(e.get("out") or 0)
            b["reasoning"] += int(e.get("r_out") or 0)
            if e.get("err"):
                b["errors"] += 1
    return by_model, by_agent


def _line(name, b):
    return (f"{name}\n    reqs={b['reqs']}  usd=${b['usd']:.4f}  in={b['in']:,}  cached={b['cached']:,}  "
            f"out={b['out']:,}  reasoning={b['reasoning']:,}  errors={b['errors']}")


def usage_text(rows) -> str:
    by_model, by_agent = summarize(rows)
    now = time.time()
    _roll(now)
    idle = (now - ledger["run_last"]) if ledger["run_last"] else None
    out = [
        f"TODAY {ledger['day']} ({_tz_note}): ${ledger['day_usd']:.4f} of ${DAILY_BUDGET_USD:.2f} daily cap",
        f"RUN: ${ledger['run_usd']:.4f} of ${RUN_BUDGET_USD:.2f} run cap, {ledger['run_n']} paid requests, "
        + (f"idle {idle:.0f}s (run resets after {RUN_IDLE_SECONDS:.0f}s)" if idle is not None else "no active run"),
        "",
        "BY MODEL",
    ]
    out += [_line(k, v) for k, v in sorted(by_model.items())] or ["    (no requests today)"]
    out += ["", "BY AGENT (model | tools it was offered) - the tool list tells planner, scout, builder and reviewer apart"]
    out += [_line(k, v) for k, v in sorted(by_agent.items())] or ["    (none)"]
    out += ["", "LAST 12 REQUESTS"]
    for e in rows[-12:]:
        out.append(f"    {e.get('ts', '?')[11:19]}Z {e.get('m', '?'):<22} in={e.get('in', 0):>7,} cached={e.get('c_in', 0):>7,} "
                   f"out={e.get('out', 0):>6,} ${float(e.get('cost') or 0):.4f} status={e.get('status')}")
    return "\n".join(out) + "\n"


# -- routes --
@app.get("/v1/models")
@app.get("/models")
async def list_models(auth: None = Depends(verify_token)):
    model_ids = list(OPENROUTER_ROUTES.keys()) + ["gemini-3.5-flash-lite"]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": int(time.time()), "owned_by": "horizon-proxy"} for m in model_ids],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, auth: None = Depends(verify_token)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model = body.get("model")
    rid = uuid.uuid4().hex[:6]
    t0 = time.time()
    is_stream = bool(body.get("stream", False))
    effort = "-"
    key_label = "-"
    gate_held = False
    target_backend_model = model

    tool_names = sorted({(t.get("function") or {}).get("name", "?") for t in (body.get("tools") or []) if isinstance(t, dict)})
    tools_in_count = len(body.get("tools") or [])
    log.info(
        "[%s] -> %s stream=%s msgs=%d tools=%d %s",
        rid, model, is_stream, len(body.get("messages") or []), tools_in_count, tool_names,
    )

    def release_gate():
        nonlocal gate_held
        if gate_held:
            gate_held = False
            stats["gemini_inflight"] -= 1
            gemini_gate.release()

    # Route 1: OpenRouter (paid; governed)
    if model in OPENROUTER_ROUTES:
        reason = budget_check(time.time())
        if reason:
            log.warning("[%s] 402 BUDGET %s", rid, reason)
            return JSONResponse(status_code=402, content={"error": {"message": reason, "type": "BudgetExceeded"}})
        if MAX_OUT_TOKENS:
            # Clamp existing parameters if set above the cap
            for k in ("max_tokens", "max_completion_tokens"):
                if isinstance(body.get(k), int) and body[k] > MAX_OUT_TOKENS:
                    body[k] = MAX_OUT_TOKENS
            # Enforce the cap if the client passed neither parameter
            if not any(isinstance(body.get(k), int) for k in ("max_tokens", "max_completion_tokens")):
                body["max_completion_tokens"] = MAX_OUT_TOKENS
        target, effort_val = OPENROUTER_ROUTES[model]
        target_backend_model = target
        body["model"] = target
        body["api_key"] = OPENROUTER_KEY
        if effort_val:
            effort = effort_val
            body.setdefault("extra_body", {}).setdefault("reasoning", {"effort": effort_val})

    # Route 2: Gemini
    elif model == "gemini-3.5-flash-lite":
        # Google Gemini API rejects requests ending with an assistant/model turn (HTTP 400).
        # Normalize trailing messages before passing to LiteLLM:
        msgs = body.get("messages") or []
        while msgs and msgs[-1].get("role") in ("assistant", "model"):
            last_content = msgs[-1].get("content")
            # If trailing assistant message has no text (or empty tool call), pop it
            if not last_content or (isinstance(last_content, str) and not last_content.strip()):
                msgs.pop()
            else:
                # If trailing assistant message has text, append a user continuation turn
                msgs.append({"role": "user", "content": "Continue."})
                break
        body["messages"] = msgs

        key_label, key, pace_wait = await get_next_gemini_key()
        tg = time.time()
        await gemini_gate.acquire()
        gate_held = True
        stats["gemini_inflight"] += 1
        gate_wait = time.time() - tg

        target_backend_model = "gemini/gemini-3.5-flash-lite"
        body["model"] = target_backend_model
        body["api_key"] = key
        log.info(
            "[%s] gemini key=%s pace_wait=%.1fs gate_wait=%.1fs inflight=%d",
            rid, key_label, pace_wait, gate_wait, stats["gemini_inflight"],
        )

    else:
        log.warning("[%s] unknown model: %s", rid, model)
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")

    if is_stream:
        body.setdefault("stream_options", {"include_usage": True})

    try:
        response = await litellm.acompletion(**body)
    except Exception as e:
        release_gate()
        return error_response(rid, model, key_label, e, t0, tools_in_count, tool_names)

    # -- Streaming Response --
    if is_stream:
        async def event_generator():
            first = None
            chunks = 0
            usage = None
            called_tools = set()
            try:
                async for chunk in response:
                    if first is None:
                        first = time.time() - t0
                    chunks += 1

                    choices = getattr(chunk, "choices", None)
                    if choices:
                        delta = getattr(choices[0], "delta", None)
                        for tc in getattr(delta, "tool_calls", None) or []:
                            fn = getattr(tc, "function", None)
                            if fn and (name := getattr(fn, "name", None)):
                                called_tools.add(name)

                    u = getattr(chunk, "usage", None)
                    if u:
                        usage = u
                    yield f"data: {chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"

                total_time = round(time.time() - t0, 2)
                diag = parse_usage_and_cost(usage, target_backend_model)
                tool_list = sorted(called_tools)
                if model in OPENROUTER_ROUTES:
                    record_cost(diag["cost"], time.time())

                log.info(
                    "[%s] <- 200 stream %s key=%s cost=$%.5f in=%d (c=%d) out=%d (r=%d) called=%s total=%.1fs | day=$%.3f run=$%.3f",
                    rid, model, key_label, diag["cost"], diag["in"], diag["c_in"],
                    diag["out"], diag["r_out"], tool_list, total_time, ledger["day_usd"], ledger["run_usd"],
                )
                log_diagnostic({
                    "ts": now_iso(),
                    "rid": rid, "m": model, "key": key_label, "status": 200,
                    "dur": total_time, "ttft": round(first or 0.0, 2),
                    "tools_in": tools_in_count, "tool_names": tool_names, "tools_called": tool_list,
                    **diag, "err": None
                })

            except asyncio.CancelledError:
                log.warning("[%s] client disconnected mid-stream %s after %.1fs", rid, model, time.time() - t0)
                raise
            except Exception as e:
                log.error("[%s] !! stream error %s %s | %s", rid, model, type(e).__name__, scrub(str(e))[:200])
                raise
            finally:
                release_gate()

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # -- Non-Streaming Response --
    release_gate()
    data = response.model_dump()
    total_time = round(time.time() - t0, 2)
    diag = parse_usage_and_cost(data.get("usage"), target_backend_model)
    if model in OPENROUTER_ROUTES:
        record_cost(diag["cost"], time.time())

    called_tools = []
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        called_tools = [tc.get("function", {}).get("name") for tc in msg.get("tool_calls") or [] if tc.get("function", {}).get("name")]

    log.info(
        "[%s] <- 200 %s key=%s cost=$%.5f in=%d (c=%d) out=%d (r=%d) called=%s total=%.1fs | day=$%.3f run=$%.3f",
        rid, model, key_label, diag["cost"], diag["in"], diag["c_in"],
        diag["out"], diag["r_out"], called_tools, total_time, ledger["day_usd"], ledger["run_usd"],
    )

    log_diagnostic({
        "ts": now_iso(),
        "rid": rid, "m": model, "key": key_label, "status": 200,
        "dur": total_time, "ttft": total_time,
        "tools_in": tools_in_count, "tool_names": tool_names, "tools_called": called_tools,
        **diag, "err": None
    })

    return JSONResponse(content=data)


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/usage")
@app.get("/{auth_key}")
async def usage(request: Request, auth_key: Optional[str] = None):
    """Accessible via http://localhost:4000/<MASTER_KEY> or http://localhost:4000/usage?key=<MASTER_KEY>"""
    # Verify access: matches path param, query param, or bearer token
    is_authed = False
    if not MASTER_KEY:
        is_authed = True
    elif auth_key == MASTER_KEY or request.query_params.get("key") == MASTER_KEY:
        is_authed = True
    elif request.headers.get("Authorization") == f"Bearer {MASTER_KEY}":
        is_authed = True

    if not is_authed:
        raise HTTPException(status_code=401, detail="Invalid Master API Key")

    rows = read_rows(ledger["day"])
    if request.query_params.get("fmt") == "json":
        by_model, by_agent = summarize(rows)
        return {
            "day": ledger["day"], "tz": _tz_note,
            "day_usd": round(ledger["day_usd"], 5), "daily_cap": DAILY_BUDGET_USD,
            "run_usd": round(ledger["run_usd"], 5), "run_cap": RUN_BUDGET_USD, "run_requests": ledger["run_n"],
            "by_model": by_model, "by_agent": by_agent,
        }
    return PlainTextResponse(usage_text(rows))


@app.get("/status")
async def status(auth: None = Depends(verify_token)):
    now = time.time()
    return {
        "gemini_inflight": stats["gemini_inflight"],
        "gemini_parallel": GEMINI_PARALLEL,
        "cooldown_s": KEY_COOLDOWN,
        "keys": [
            {"key": f"G{n}", "ready_in_s": round(max(0.0, key_last_used[i] + KEY_COOLDOWN - now), 1)}
            for i, (n, _) in enumerate(GEMINI_KEYS)
        ],
        "budget": {k: (round(v, 5) if isinstance(v, float) else v) for k, v in ledger.items()},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "4000")))