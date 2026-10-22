import os
import re
import glob
import json
import time
import uuid
import asyncio
import logging
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict, deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse, HTMLResponse
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

# Load all Gemini keys from env (GEMINI_KEY_1 .. GEMINI_KEY_16+)
def _load_gemini_keys():
    found = {}
    for k, v in os.environ.items():
        m = re.fullmatch(r"GEMINI_KEY_(\d+)", k)
        if m and v.strip():
            found[int(m.group(1))] = v.strip()
    for i in range(1, 65):
        if i not in found and (val := os.getenv(f"GEMINI_KEY_{i}")):
            if val.strip():
                found[i] = val.strip()
    return [(i, found[i]) for i in sorted(found.keys())]

GEMINI_KEYS = _load_gemini_keys()
KEY_COOLDOWN = float(os.getenv("GEMINI_KEY_COOLDOWN", "50"))
GEMINI_PARALLEL = int(os.getenv("GEMINI_PARALLEL", "2"))
GEMINI_KEY_TPM = int(os.getenv("GEMINI_KEY_TPM", "250000"))  # 250k tokens per min per key
TPM_WINDOW = 60.0

# Spend governor (paid OpenRouter routes only; Gemini is free and never blocked).
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "3.00"))   # hard cap per local day, 0 = off
RUN_BUDGET_USD = float(os.getenv("RUN_BUDGET_USD", "1.50"))       # cap per "run", 0 = off
RUN_IDLE_SECONDS = float(os.getenv("RUN_IDLE_SECONDS", "900"))    # a run ends after this much inactivity
BUDGET_TZ = os.getenv("BUDGET_TZ", "America/Toronto")             # where "a day" starts and ends
MAX_OUT_TOKENS = int(os.getenv("MAX_OUT_TOKENS", "32000"))        # clamp per-request output on paid routes, 0 = off

key_index = 0
key_last_used = [0.0] * len(GEMINI_KEYS)
key_tokens = [deque() for _ in range(len(GEMINI_KEYS))]
key_inflight_tokens = [0] * len(GEMINI_KEYS)
KEY_LABEL_TO_POS = {f"G{n}": i for i, (n, _) in enumerate(GEMINI_KEYS)}

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


# -- Stateful incremental file cache --
_STATEFUL_LOG_CACHE: Dict[str, Dict[str, Any]] = {}


def get_all_cached_rows() -> List[dict]:
    """Statefully reads diagnostics files incrementally; only parses new appended bytes."""
    all_rows = []
    files = sorted(glob.glob(os.path.join(LOGS_DIR, "diagnostics_*.jsonl")))
    for path in files:
        try:
            stat = os.stat(path)
            size = stat.st_size
            mtime = stat.st_mtime
            cache = _STATEFUL_LOG_CACHE.get(path)

            if cache and cache["size"] == size and cache["mtime"] == mtime:
                all_rows.extend(cache["rows"])
                continue

            rows = cache["rows"] if (cache and size >= cache["size"]) else []
            seek_pos = cache["size"] if (cache and size >= cache["size"]) else 0

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if seek_pos > 0:
                    f.seek(seek_pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue

            _STATEFUL_LOG_CACHE[path] = {"size": size, "mtime": mtime, "rows": rows}
            all_rows.extend(rows)
        except OSError:
            continue
    return all_rows


def read_rows(day: Optional[str] = None) -> List[dict]:
    """Returns diagnostics rows filtered for the specific local day."""
    rows = get_all_cached_rows()
    if day is None:
        return rows
    return [e for e in rows if row_day(e) == day]


def get_available_days() -> List[str]:
    """Returns all unique local dates present across logs, newest first."""
    rows = get_all_cached_rows()
    days = {d for e in rows if (d := row_day(e))}
    today = day_of(time.time())
    days.add(today)
    return sorted(days, reverse=True)


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
    "pacer up | gemini_keys=%d [%s] cooldown=%.0fs tpm=%d parallel=%d | openrouter_key=%s | auth=%s | diag_file=%s",
    len(GEMINI_KEYS), ",".join(f"G{n}" for n, _ in GEMINI_KEYS), KEY_COOLDOWN, GEMINI_KEY_TPM, GEMINI_PARALLEL,
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


def format_human_time(iso_ts: str) -> dict:
    """Returns local 12h clock time and relative time from an ISO timestamp."""
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone(TZ)
        now_dt = datetime.now(TZ)
        diff_sec = max(0, int((now_dt - local_dt).total_seconds()))

        if diff_sec < 5:
            rel = "just now"
        elif diff_sec < 60:
            rel = f"{diff_sec}s ago"
        elif diff_sec < 3600:
            rel = f"{diff_sec // 60}m ago"
        elif diff_sec < 86400:
            rel = f"{diff_sec // 3600}h ago"
        else:
            rel = f"{diff_sec // 86400}d ago"

        return {
            "local_time": local_dt.strftime("%I:%M:%S %p"),
            "local_date": local_dt.strftime("%b %d"),
            "relative": rel,
            "iso": iso_ts,
        }
    except Exception:
        return {"local_time": iso_ts or "?", "local_date": "", "relative": "", "iso": iso_ts or ""}


def fmt_compact(n: float) -> str:
    """Formats numbers with a single suffix (e.g. 1.25M, 42.1k), avoiding duplicate letters."""
    try:
        val = float(n or 0)
    except (ValueError, TypeError):
        return "0"
    if abs(val) >= 1_000_000:
        formatted = f"{val / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{formatted}M"
    if abs(val) >= 1_000:
        formatted = f"{val / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}k"
    return f"{int(val):,}" if val.is_integer() else f"{val:.1f}"


async def verify_token(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if MASTER_KEY:
        if not credentials or credentials.credentials != MASTER_KEY:
            log.warning("401 invalid master key from %s", request.client.host if request.client else "?")
            raise HTTPException(status_code=401, detail="Invalid Master API Key")


# -- Rate Limiting & TPM Tracking Helpers --
def _clean_key_tokens(pos: int, now: float):
    dq = key_tokens[pos]
    cutoff = now - TPM_WINDOW
    while dq and dq[0][0] <= cutoff:
        dq.popleft()


def get_key_tpm_usage(pos: int, now: float) -> int:
    _clean_key_tokens(pos, now)
    return sum(tok for _, tok in key_tokens[pos]) + key_inflight_tokens[pos]


def get_key_tpm_wait(pos: int, now: float, needed_tokens: int = 0) -> float:
    if GEMINI_KEY_TPM <= 0:
        return 0.0
    _clean_key_tokens(pos, now)
    dq = key_tokens[pos]
    used = sum(tok for _, tok in dq) + key_inflight_tokens[pos]

    if used + needed_tokens <= GEMINI_KEY_TPM or used == 0:
        return 0.0

    tokens_to_expire = (used + needed_tokens) - GEMINI_KEY_TPM
    freed = 0
    for ts, count in dq:
        freed += count
        if freed >= tokens_to_expire:
            return max(0.0, (ts + TPM_WINDOW) - now)

    return TPM_WINDOW


def get_key_wait(pos: int, now: float, needed_tokens: int = 0) -> float:
    cd_wait = max(0.0, (key_last_used[pos] + KEY_COOLDOWN) - now)
    tpm_wait = get_key_tpm_wait(pos, now, needed_tokens)
    return max(cd_wait, tpm_wait)


def estimate_tokens(body: dict) -> int:
    msgs = body.get("messages") or []
    try:
        return litellm.token_counter(model="gemini/gemini-3.5-flash-lite", messages=msgs)
    except Exception:
        pass
    chars = sum(len(str(m.get("content") or "")) for m in msgs if isinstance(m, dict))
    tools = body.get("tools") or []
    if tools:
        chars += len(str(tools))
    return max(1, chars // 4)


def record_gemini_usage(pos: int, reserved: int, actual: int):
    now = time.time()
    if pos < len(key_inflight_tokens):
        key_inflight_tokens[pos] = max(0, key_inflight_tokens[pos] - reserved)
        if actual > 0:
            key_tokens[pos].append((now, actual))


def release_gemini_usage(pos: int, reserved: int):
    if pos < len(key_inflight_tokens):
        key_inflight_tokens[pos] = max(0, key_inflight_tokens[pos] - reserved)


async def get_next_gemini_key(estimated_tokens: int = 0):
    global key_index
    if not GEMINI_KEYS:
        raise HTTPException(status_code=503, detail="No Gemini keys configured")

    n_keys = len(GEMINI_KEYS)
    async with key_lock:
        now = time.time()
        chosen_pos = None
        wait_time = 0.0

        for offset in range(n_keys):
            candidate_pos = (key_index + offset) % n_keys
            w = get_key_wait(candidate_pos, now, estimated_tokens)
            if w <= 0.0:
                chosen_pos = candidate_pos
                wait_time = 0.0
                key_index = (chosen_pos + 1) % n_keys
                break

        if chosen_pos is None:
            all_waits = [(get_key_wait(i, now, estimated_tokens), i) for i in range(n_keys)]
            all_waits.sort(key=lambda item: (item[0], (item[1] - key_index) % n_keys))
            wait_time, chosen_pos = all_waits[0]
            key_index = (chosen_pos + 1) % n_keys

        start_at = now + wait_time
        key_last_used[chosen_pos] = start_at
        if estimated_tokens > 0:
            key_inflight_tokens[chosen_pos] += estimated_tokens

    if wait_time > 0:
        await asyncio.sleep(wait_time)

    n, key = GEMINI_KEYS[chosen_pos]
    return f"G{n}", key, max(wait_time, 0.0)


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
        "err": f"{type(e).__name__}: {msg[:200]}"
    })
    return JSONResponse(status_code=status, content={"error": {"message": msg, "type": type(e).__name__}})


# -- usage calculations --
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
            if e.get("status", 200) != 200 or e.get("err"):
                b["errors"] += 1
    return by_model, by_agent


def summarize_tools(rows) -> List[Tuple[str, int]]:
    """Calculates tool calls ranked by invocation count."""
    counts = defaultdict(int)
    for e in rows:
        for name in (e.get("tools_called") or []):
            if name and name != "?":
                counts[name] += 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)


def summarize_keys(rows, now: float):
    """Calculates simplified human-readable stats for each key on the selected day."""
    key_stats = {}
    for i, (n, _) in enumerate(GEMINI_KEYS):
        lbl = f"G{n}"
        cd_left = max(0.0, key_last_used[i] + KEY_COOLDOWN - now)
        tpm_used = get_key_tpm_usage(i, now)
        tpm_left = get_key_tpm_wait(i, now, 0)

        if tpm_left > 0:
            status_text = f"TPM {tpm_left:.0f}s"
            status_type = "warn"
        elif cd_left > 0:
            status_text = f"Wait {cd_left:.0f}s"
            status_type = "busy"
        else:
            status_text = "READY"
            status_type = "ready"

        key_stats[lbl] = {
            "key": lbl,
            "reqs": 0,
            "errors": 0,
            "tokens": 0,
            "cost": 0.0,
            "status_text": status_text,
            "status_type": status_type,
            "tpm_used": tpm_used,
            "tpm_limit": GEMINI_KEY_TPM,
            "tpm_pct": min(100, int((tpm_used / GEMINI_KEY_TPM) * 100)) if GEMINI_KEY_TPM else 0,
        }

    for e in rows:
        lbl = e.get("key", "-")
        if lbl not in key_stats:
            key_stats[lbl] = {
                "key": lbl, "reqs": 0, "errors": 0, "tokens": 0, "cost": 0.0,
                "status_text": "ACTIVE", "status_type": "ready",
                "tpm_used": 0, "tpm_limit": 0, "tpm_pct": 0,
            }
        s = key_stats[lbl]
        s["reqs"] += 1
        s["tokens"] += int(e.get("in") or 0) + int(e.get("out") or 0)
        s["cost"] += float(e.get("cost") or 0.0)
        if e.get("status", 200) != 200 or e.get("err"):
            s["errors"] += 1

    return key_stats


def _line(name, b):
    return (f"{name}\n    reqs={b['reqs']}  usd=${b['usd']:.4f}  in={b['in']:,}  cached={b['cached']:,}  "
            f"out={b['out']:,}  reasoning={b['reasoning']:,}  errors={b['errors']}")


def usage_text(rows, day_label: str) -> str:
    """Enhanced plaintext view for CLI/curl."""
    now = time.time()
    by_model, by_agent = summarize(rows)
    key_stats = summarize_keys(rows, now)
    ranked_tools = summarize_tools(rows)

    total_reqs = sum(b["reqs"] for b in by_model.values())
    total_cost = sum(b["usd"] for b in by_model.values())
    total_errs = sum(b["errors"] for b in by_model.values())
    total_in = sum(b["in"] for b in by_model.values())
    total_out = sum(b["out"] for b in by_model.values())

    out = [
        "==================================================================================================",
        f"HORIZON PROXY RECEIPT | {day_label} ({_tz_note})",
        "==================================================================================================",
        f"DAY SPEND: ${total_cost:.4f} of ${DAILY_BUDGET_USD:.2f} daily cap | {total_reqs} reqs, {total_errs} errors",
        f"TOTAL TOKENS: {fmt_compact(total_in)} in, {fmt_compact(total_out)} out",
        "",
        "--- KEYS SUMMARY (G1..G16) ---",
    ]

    for k in sorted(key_stats.keys(), key=lambda x: (0 if x.startswith("G") else 1, int(x[1:]) if x[1:].isdigit() else 999)):
        ks = key_stats[k]
        out.append(
            f"  {ks['key']:<5} | reqs={ks['reqs']:<5} | tok={fmt_compact(ks['tokens']):<7} | "
            f"err={ks['errors']:<3} | TPM={fmt_compact(ks['tpm_used'])}/{fmt_compact(ks['tpm_limit'])} ({ks['tpm_pct']}%) | [{ks['status_text']}]"
        )

    out += ["", "--- BY MODEL ---"]
    out += [_line(k, v) for k, v in sorted(by_model.items())] or ["    (no requests recorded)"]

    out += ["", "--- TOOL CALLS RANKED ---"]
    if ranked_tools:
        for idx, (name, count) in enumerate(ranked_tools, 1):
            out.append(f"  #{idx:<2} {name:<32} {count:>5} calls")
    else:
        out.append("    (no tool calls recorded)")

    # 1. Last 8 Requests FIRST
    out += ["", f"--- LAST 8 REQUESTS ({total_reqs:,} total today) ---"]
    req_8 = rows[-8:]
    if req_8:
        for e in reversed(req_8):
            ht = format_human_time(e.get("ts", ""))
            out.append(
                f"  {ht['local_time']} ({ht['relative']:<8}) {e.get('m', '?'):<22} {e.get('key', '-'):<4} "
                f"in={fmt_compact(e.get('in', 0)):>6} out={fmt_compact(e.get('out', 0)):>5} "
                f"${float(e.get('cost') or 0):.4f} status={e.get('status')} dur={e.get('dur', 0)}s"
            )
    else:
        out.append("    (no requests recorded)")

    # 2. Last 8 Errors THEN
    err_rows = [e for e in rows if e.get("status", 200) != 200 or e.get("err")][-8:]
    out += ["", f"--- LAST 8 ERRORS ({total_errs} total today) ---"]
    if err_rows:
        for e in reversed(err_rows):
            ht = format_human_time(e.get("ts", ""))
            err_msg = (e.get("err") or "error").replace("\n", " ")[:80]
            out.append(f"  {ht['local_time']} ({ht['relative']:<8}) {e.get('m', '?'):<22} {e.get('key', '-'):<4} status={e.get('status')} | {err_msg}")
    else:
        out.append("    (no errors recorded for this day)")

    out += ["", "--- BY AGENT (model | tools offered) ---"]
    out += [_line(k, v) for k, v in sorted(by_agent.items())] or ["    (none)"]

    return "\n".join(out) + "\n"


def usage_html(rows, day_label: str, available_days: List[str], auth_param: str) -> str:
    """Industrial high-density cockpit dashboard with tight column boundaries and zero wasted space."""
    now = time.time()
    by_model, by_agent = summarize(rows)
    key_stats = summarize_keys(rows, now)
    ranked_tools = summarize_tools(rows)

    total_reqs = sum(b["reqs"] for b in by_model.values())
    total_cost = sum(b["usd"] for b in by_model.values())
    total_errs = sum(b["errors"] for b in by_model.values())
    total_in = sum(b["in"] for b in by_model.values())
    total_cached = sum(b["cached"] for b in by_model.values())
    total_out = sum(b["out"] for b in by_model.values())
    cache_pct = (total_cached / total_in * 100) if total_in else 0.0

    today = day_of(now)
    is_today = (day_label == today)
    cap_pct = min(100.0, (ledger["day_usd"] / DAILY_BUDGET_USD * 100)) if (DAILY_BUDGET_USD and is_today) else 0.0

    # Dropdown for available days
    day_options = []
    for d in available_days[:30]:
        selected = "selected" if d == day_label else ""
        label = f"Today ({d})" if d == today else d
        day_options.append(f'<option value="{d}" {selected}>{label}</option>')
    day_dropdown_html = "\n".join(day_options)

    # Key Sidebar Cards HTML
    key_cards = []
    sorted_keys = sorted(key_stats.keys(), key=lambda x: (0 if x.startswith("G") else 1, int(x[1:]) if x[1:].isdigit() else 999))
    for k in sorted_keys:
        ks = key_stats[k]
        badge_cls = "badge-ready" if ks["status_type"] == "ready" else ("badge-busy" if ks["status_type"] == "busy" else "badge-warn")
        tpm_meter = ""
        if ks["tpm_limit"] > 0:
            meter_color = "#ef4444" if ks["tpm_pct"] >= 90 else ("#f59e0b" if ks["tpm_pct"] >= 65 else "#10b981")
            tpm_meter = f"""
            <div class="tpm-track">
                <div class="tpm-bar" style="width: {ks['tpm_pct']}%; background: {meter_color};"></div>
            </div>
            <div class="key-sub text-muted">TPM: {fmt_compact(ks['tpm_used'])} / {fmt_compact(ks['tpm_limit'])} ({ks['tpm_pct']}%)</div>
            """
        key_cards.append(f"""
        <div class="key-card">
            <div class="key-card-header">
                <span class="key-name">{ks['key']}</span>
                <span class="badge {badge_cls}">{ks['status_text']}</span>
            </div>
            <div class="key-metrics">
                <span><b>{ks['reqs']}</b> reqs</span>
                <span><b>{fmt_compact(ks['tokens'])}</b> tok</span>
                <span class="{'text-danger' if ks['errors'] else 'text-muted'}"><b>{ks['errors']}</b> errs</span>
            </div>
            {tpm_meter}
        </div>
        """)
    key_cards_html = "\n".join(key_cards)

    # Models Table HTML (tight-table)
    model_rows = []
    for m, b in sorted(by_model.items(), key=lambda x: x[1]["reqs"], reverse=True):
        m_err_rate = (b["errors"] / b["reqs"] * 100) if b["reqs"] else 0.0
        model_rows.append(f"""
        <tr>
            <td class="font-mono col-expand"><b>{m}</b></td>
            <td class="num-cell">{b['reqs']:,}</td>
            <td class="num-cell font-mono">${b['usd']:.4f}</td>
            <td class="num-cell">{fmt_compact(b['in'])} <small class="text-muted">({fmt_compact(b['cached'])} c)</small></td>
            <td class="num-cell">{fmt_compact(b['out'])}</td>
            <td class="num-cell {'text-danger font-bold' if b['errors'] else ''}">{b['errors']} <small>({m_err_rate:.1f}%)</small></td>
        </tr>
        """)
    model_table_html = "\n".join(model_rows) or "<tr><td colspan='6' class='text-muted'>No requests recorded</td></tr>"

    # Tool Calls Ranked HTML (tight-fit)
    total_tool_invocations = sum(cnt for _, cnt in ranked_tools)
    tool_rows = []
    for idx, (name, cnt) in enumerate(ranked_tools, 1):
        pct = (cnt / total_tool_invocations * 100) if total_tool_invocations else 0.0
        tool_rows.append(f"""
        <div class="tool-row">
            <span class="rank-pill">#{idx}</span>
            <span class="font-mono tool-name"><b>{name}</b></span>
            <span class="tool-count font-mono"><b>{cnt:,}</b> <small class="text-muted">({pct:.1f}%)</small></span>
        </div>
        """)
    tool_table_html = "\n".join(tool_rows) or "<div class='text-muted' style='padding: 6px;'>No tool calls recorded</div>"

    # 1. Last 8 Requests HTML (tight-table with minimum column spacing)
    req_items = []
    for e in reversed(rows[-8:]):
        ht = format_human_time(e.get("ts", ""))
        status = e.get("status", 200)
        s_badge = "badge-ready" if status == 200 else "badge-err"
        req_items.append(f"""
        <tr>
            <td class="text-muted whitespace-nowrap"><b class="text-light">{ht['local_time']}</b> <small>({ht['relative']})</small></td>
            <td><span class="badge {s_badge}">{status}</span></td>
            <td class="font-mono col-expand">{e.get('m', '?')}</td>
            <td><span class="badge badge-neutral">{e.get('key', '-')}</span></td>
            <td class="num-cell">{fmt_compact(e.get('in', 0))} <small class="text-muted">({fmt_compact(e.get('c_in', 0))})</small></td>
            <td class="num-cell">{fmt_compact(e.get('out', 0))}</td>
            <td class="num-cell font-mono">${float(e.get('cost') or 0):.4f}</td>
            <td class="num-cell text-muted"><small>{e.get('dur', 0):.2f}s</small></td>
        </tr>
        """)
    requests_table_html = "\n".join(req_items) or "<tr><td colspan='8' class='text-muted'>No requests recorded</td></tr>"

    # 2. Last 8 Errors HTML (Error message takes the bulk of the room)
    err_rows_list = [e for e in rows if e.get("status", 200) != 200 or e.get("err")][-8:]
    error_items = []
    for e in reversed(err_rows_list):
        ht = format_human_time(e.get("ts", ""))
        err_msg = scrub(e.get("err") or "Request failed").replace("\n", " ")
        error_items.append(f"""
        <tr>
            <td class="text-muted whitespace-nowrap"><b class="text-light">{ht['local_time']}</b> <small>({ht['relative']})</small></td>
            <td><span class="badge badge-err">{e.get('status', 'ERR')}</span></td>
            <td class="font-mono whitespace-nowrap">{e.get('m', '?')}</td>
            <td><span class="badge badge-neutral">{e.get('key', '-')}</span></td>
            <td class="font-mono col-expand text-light-dim"><small>{err_msg}</small></td>
            <td class="num-cell text-muted"><small>{e.get('dur', 0):.2f}s</small></td>
        </tr>
        """)
    errors_table_html = "\n".join(error_items) or "<tr><td colspan='6' class='text-muted'>No errors recorded for this day 🎉</td></tr>"

    # Agent Breakdown HTML (tight-table)
    agent_rows = []
    for sig, b in sorted(by_agent.items(), key=lambda x: x[1]["reqs"], reverse=True):
        agent_rows.append(f"""
        <tr>
            <td class="font-mono col-expand"><small>{sig}</small></td>
            <td class="num-cell">{b['reqs']:,}</td>
            <td class="num-cell font-mono">${b['usd']:.4f}</td>
            <td class="num-cell">{fmt_compact(b['in'])}</td>
            <td class="num-cell">{fmt_compact(b['out'])}</td>
            <td class="num-cell {'text-danger' if b['errors'] else ''}">{b['errors']}</td>
        </tr>
        """)
    agent_table_html = "\n".join(agent_rows) or "<tr><td colspan='6' class='text-muted'>No agents active</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Horizon Proxy | {day_label}</title>
    <style>
        :root {{
            --bg: #080c14;
            --surface: #0e1422;
            --surface-hover: #141c2e;
            --border: #1f293d;
            --border-light: #2d3b55;
            --text: #f1f5f9;
            --text-muted: #94a3b8;
            --primary: #38bdf8;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            padding: 5px 8px;
            font-size: 14px;
            line-height: 1.35;
        }}
        a {{ color: var(--primary); text-decoration: none; }}

        /* Top HUD Ribbon - Zero wasted horizontal space */
        .hud-ribbon {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 4px 10px;
            margin-bottom: 5px;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .hud-brand {{
            display: flex;
            align-items: center;
            gap: 8px;
            white-space: nowrap;
        }}
        .brand-title {{
            font-size: 15px;
            font-weight: 800;
            color: #fff;
            letter-spacing: -0.02em;
        }}
        .day-select {{
            background: #141d2f;
            color: #fff;
            border: 1px solid var(--border-light);
            border-radius: 3px;
            padding: 2px 6px;
            font-size: 12.5px;
            font-weight: 700;
            cursor: pointer;
            outline: none;
        }}
        .refresh-pill {{
            display: flex;
            align-items: center;
            gap: 4px;
            font-size: 11.5px;
            color: var(--text-muted);
            cursor: pointer;
        }}

        /* Operational Telemetry - Fills middle of top bar */
        .hud-telemetry {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 12px;
            flex-wrap: wrap;
        }}
        .tele-stat {{ display: inline-flex; align-items: center; gap: 4px; }}
        .tele-k {{ color: var(--text-muted); font-size: 10px; font-weight: 800; letter-spacing: 0.05em; text-transform: uppercase; }}
        .tele-v {{ color: #fff; font-weight: 700; font-family: var(--font-mono); }}
        .tele-sep {{ color: var(--border-light); font-weight: 300; }}

        /* Metrics Group (Right HUD) */
        .hud-metrics {{
            display: flex;
            align-items: center;
            gap: 6px;
            white-space: nowrap;
        }}
        .metric-pill {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            background: #131b2e;
            border: 1px solid var(--border);
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 12.5px;
        }}
        .metric-pill-label {{ color: var(--text-muted); font-size: 10px; text-transform: uppercase; font-weight: 800; }}
        .metric-pill-val {{ font-weight: 800; color: #fff; font-family: var(--font-mono); }}
        .metric-pill.highlight-spend {{
            background: rgba(56, 189, 248, 0.16);
            border: 1px solid #38bdf8;
        }}
        .metric-pill.highlight-spend .metric-pill-label {{ color: #7dd3fc; }}
        .metric-pill.highlight-spend .metric-pill-val {{ color: #38bdf8; font-size: 13.5px; }}

        /* Bento Grid Layout - Ultra Snug 5px Gap */
        .bento-layout {{
            display: grid;
            grid-template-columns: 240px 1fr;
            gap: 5px;
        }}
        @media (max-width: 1024px) {{
            .bento-layout {{ grid-template-columns: 1fr; }}
        }}
        .bento-sidebar {{ display: flex; flex-direction: column; gap: 5px; }}
        .bento-main {{ display: flex; flex-direction: column; gap: 5px; }}

        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 6px 8px;
        }}
        .card-header {{
            font-size: 11px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-muted);
            margin-bottom: 5px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            padding-bottom: 4px;
        }}

        .grid-2col {{
            display: grid;
            grid-template-columns: 1.25fr 1fr;
            gap: 5px;
        }}
        .grid-2col-equal {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 5px;
        }}
        @media (max-width: 1280px) {{
            .grid-2col, .grid-2col-equal {{ grid-template-columns: 1fr; }}
        }}

        /* Key cards */
        .key-list {{ display: flex; flex-direction: column; gap: 4px; max-height: calc(100vh - 145px); overflow-y: auto; padding-right: 3px; }}
        .key-card {{
            background: rgba(255, 255, 255, 0.012);
            border: 1px solid var(--border);
            border-radius: 3px;
            padding: 4px 6px;
        }}
        .key-card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 2px; }}
        .key-name {{ font-weight: 800; font-family: var(--font-mono); font-size: 13.5px; color: #fff; }}
        .key-metrics {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--text-muted); }}
        .tpm-track {{ background: #162035; height: 3px; border-radius: 2px; overflow: hidden; margin-top: 3px; }}
        .tpm-bar {{ height: 100%; }}
        .key-sub {{ font-size: 9.5px; margin-top: 2px; font-family: var(--font-mono); }}

        /* Badges */
        .badge {{
            font-size: 10px;
            padding: 1px 5px;
            border-radius: 3px;
            font-weight: 800;
            text-transform: uppercase;
            font-family: var(--font-mono);
            letter-spacing: 0.03em;
        }}
        .badge-ready {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }}
        .badge-busy {{ background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); }}
        .badge-warn {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }}
        .badge-err {{ background: rgba(239, 68, 68, 0.25); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.45); }}
        .badge-neutral {{ background: #182236; color: #cbd5e1; border: 1px solid var(--border-light); }}

        /* Tool calls tight layout */
        .tool-list-container {{ display: flex; flex-direction: column; max-height: 145px; overflow-y: auto; }}
        .tool-row {{
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 3px 4px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.03);
            font-size: 12.5px;
        }}
        .tool-row:hover {{ background: var(--surface-hover); }}
        .rank-pill {{
            background: #182236;
            color: #38bdf8;
            font-size: 10px;
            font-weight: 800;
            padding: 1px 4px;
            border-radius: 2px;
            font-family: var(--font-mono);
            min-width: 24px;
            text-align: center;
        }}
        .tool-name {{ color: #fff; margin-right: auto; }}
        .tool-count {{ font-family: var(--font-mono); white-space: nowrap; color: #cbd5e1; }}

        /* Tight Industrial Tables - Zero Column Gaps */
        .tight-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        .tight-table th, .tight-table td {{
            padding: 3px 6px;
            text-align: left;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            width: 1%;
            white-space: nowrap;
        }}
        .tight-table th {{
            color: var(--text-muted);
            font-weight: 800;
            text-transform: uppercase;
            font-size: 10px;
            letter-spacing: 0.05em;
        }}
        .tight-table tr:hover td {{ background: var(--surface-hover); }}
        .tight-table td.col-expand, .tight-table th.col-expand {{
            width: auto;
            white-space: normal;
        }}
        .tight-table td.num-cell, .tight-table th.num-cell {{
            text-align: right;
            font-family: var(--font-mono);
        }}

        .font-mono {{ font-family: var(--font-mono); }}
        .font-bold {{ font-weight: 800; }}
        .text-danger {{ color: var(--danger); }}
        .text-muted {{ color: var(--text-muted); }}
        .text-light {{ color: #ffffff; }}
        .text-light-dim {{ color: #cbd5e1; }}
        .whitespace-nowrap {{ white-space: nowrap; }}
        .table-responsive {{ overflow-x: auto; }}
        
        .progress-bar-bg {{
            background: #162035;
            height: 5px;
            border-radius: 2px;
            overflow: hidden;
            margin: 4px 0;
        }}
        .progress-bar-fill {{ height: 100%; transition: width 0.3s; }}
    </style>
</head>
<body>
    <!-- Top HUD Telemetry Ribbon -->
    <header class="hud-ribbon">
        <div class="hud-brand">
            <span class="brand-title">⚡ Horizon Proxy</span>
            <select class="day-select" onchange="window.location.href='?day='+this.value+'{auth_param}'">
                {day_dropdown_html}
            </select>
            <label class="refresh-pill">
                <input type="checkbox" id="autoRefresh" checked>
                <span>5s auto</span>
            </label>
        </div>

        <!-- Middle Telemetry: Zero Dead Space -->
        <div class="hud-telemetry">
            <span class="tele-stat"><span class="tele-k">CACHE RATIO:</span> <b class="tele-v">{cache_pct:.1f}%</b></span>
            <span class="tele-sep">|</span>
            <span class="tele-stat"><span class="tele-k">TOTAL TOKENS:</span> <b class="tele-v">{fmt_compact(total_in + total_out)}</b></span>
            <span class="tele-sep">|</span>
            <span class="tele-stat"><span class="tele-k">GATE:</span> <b class="tele-v">{stats['gemini_inflight']}/{GEMINI_PARALLEL} inflight</b></span>
            <span class="tele-sep">|</span>
            <span class="tele-stat"><span class="tele-k">COOLDOWN:</span> <b class="tele-v">{int(KEY_COOLDOWN)}s</b></span>
            <span class="tele-sep">|</span>
            <span class="tele-stat"><span class="tele-k">RUN:</span> <b class="tele-v">${ledger['run_usd']:.4f}</b> <small class="text-muted">({ledger['run_n']} reqs)</small></span>
        </div>

        <!-- Right Metrics -->
        <div class="hud-metrics">
            <div class="metric-pill">
                <span class="metric-pill-label">Prompt:</span>
                <span class="metric-pill-val">{fmt_compact(total_in)}</span>
            </div>
            <div class="metric-pill">
                <span class="metric-pill-label">Cached:</span>
                <span class="metric-pill-val">{fmt_compact(total_cached)}</span>
            </div>
            <div class="metric-pill">
                <span class="metric-pill-label">Out:</span>
                <span class="metric-pill-val">{fmt_compact(total_out)}</span>
            </div>
            <div class="metric-pill highlight-spend">
                <span class="metric-pill-label">Paid Spend:</span>
                <span class="metric-pill-val">${total_cost:.4f}</span>
            </div>
        </div>
    </header>

    <div class="bento-layout">
        <!-- Sidebar: Keys & Budget -->
        <aside class="bento-sidebar">
            <div class="card">
                <div class="card-header">
                    <span>Daily Spend Cap ({_tz_note})</span>
                    <span class="text-light font-mono font-bold">{cap_pct:.1f}%</span>
                </div>
                <div style="font-size: 15px; font-weight: 800; color: #fff; font-family: var(--font-mono);">
                    ${ledger['day_usd']:.4f} <small class="text-muted" style="font-weight: 400; font-size: 11px;">/ ${DAILY_BUDGET_USD:.2f}</small>
                </div>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" style="width: {cap_pct}%; background: {'#ef4444' if cap_pct > 90 else '#38bdf8'};"></div>
                </div>
            </div>

            <div class="card" style="flex: 1;">
                <div class="card-header">
                    <span>Keys Telemetry ({len(GEMINI_KEYS)})</span>
                    <span class="badge badge-neutral">250k TPM</span>
                </div>
                <div class="key-list">
                    {key_cards_html}
                </div>
            </div>
        </aside>

        <!-- Main Cockpit Area -->
        <main class="bento-main">
            <!-- Row 1: Models Breakdown & Ranked Tool Calls -->
            <div class="grid-2col">
                <div class="card">
                    <div class="card-header">
                        <span>Models Breakdown</span>
                        <span class="badge badge-neutral">{len(by_model)} Models</span>
                    </div>
                    <div class="table-responsive">
                        <table class="tight-table">
                            <thead>
                                <tr>
                                    <th class="col-expand">Model</th>
                                    <th class="num-cell">Reqs</th>
                                    <th class="num-cell">Cost</th>
                                    <th class="num-cell">Prompt</th>
                                    <th class="num-cell">Completion</th>
                                    <th class="num-cell">Errors</th>
                                </tr>
                            </thead>
                            <tbody>
                                {model_table_html}
                            </tbody>
                        </table>
                    </div>
                </div>

                <div class="card">
                    <div class="card-header">
                        <span>Ranked Tool Calls</span>
                        <span class="badge badge-neutral">{total_tool_invocations:,} Invocations</span>
                    </div>
                    <div class="tool-list-container">
                        {tool_table_html}
                    </div>
                </div>
            </div>

            <!-- Row 2: Last 8 Requests & Last 8 Errors Side-by-Side (Cockpit Mode) -->
            <div class="grid-2col-equal">
                <!-- Last 8 Requests -->
                <div class="card">
                    <div class="card-header">
                        <span>Last 8 Requests</span>
                        <span class="badge badge-neutral">{total_reqs:,} Total</span>
                    </div>
                    <div class="table-responsive">
                        <table class="tight-table">
                            <thead>
                                <tr>
                                    <th>Time</th>
                                    <th>Status</th>
                                    <th class="col-expand">Model</th>
                                    <th>Key</th>
                                    <th class="num-cell">In (Cached)</th>
                                    <th class="num-cell">Out</th>
                                    <th class="num-cell">Cost</th>
                                    <th class="num-cell">Dur</th>
                                </tr>
                            </thead>
                            <tbody>
                                {requests_table_html}
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- Last 8 Errors -->
                <div class="card">
                    <div class="card-header">
                        <span>Last 8 Errors</span>
                        <span class="badge badge-neutral">{total_errs} Total</span>
                    </div>
                    <div class="table-responsive">
                        <table class="tight-table">
                            <thead>
                                <tr>
                                    <th>Time</th>
                                    <th>Status</th>
                                    <th>Model</th>
                                    <th>Key</th>
                                    <th class="col-expand">Reason</th>
                                    <th class="num-cell">Dur</th>
                                </tr>
                            </thead>
                            <tbody>
                                {errors_table_html}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- Row 3: Agent Tool Signatures -->
            <div class="card">
                <div class="card-header">
                    <span>Agent Tool Signatures</span>
                    <span class="badge badge-neutral">{len(by_agent)} Tool Signatures</span>
                </div>
                <div class="table-responsive">
                    <table class="tight-table">
                        <thead>
                            <tr>
                                <th class="col-expand">Signature (Model | Tools)</th>
                                <th class="num-cell">Reqs</th>
                                <th class="num-cell">Cost</th>
                                <th class="num-cell">Input</th>
                                <th class="num-cell">Output</th>
                                <th class="num-cell">Errors</th>
                            </tr>
                        </thead>
                        <tbody>
                            {agent_table_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </main>
    </div>

    <script>
        const chk = document.getElementById('autoRefresh');
        setInterval(() => {{
            if (chk && chk.checked && !document.hidden) {{
                window.location.reload();
            }}
        }}, 5000);
    </script>
</body>
</html>
"""


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

    gemini_pos = None
    gemini_reserved = 0
    gemini_recorded = False

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

    def finish_gemini(actual: Optional[int] = None):
        nonlocal gemini_recorded
        if gemini_pos is not None and not gemini_recorded:
            gemini_recorded = True
            if actual is not None and actual > 0:
                record_gemini_usage(gemini_pos, gemini_reserved, actual)
            else:
                release_gemini_usage(gemini_pos, gemini_reserved)

    # Route 1: OpenRouter (paid; governed)
    if model in OPENROUTER_ROUTES:
        reason = budget_check(time.time())
        if reason:
            log.warning("[%s] 402 BUDGET %s", rid, reason)
            return JSONResponse(status_code=402, content={"error": {"message": reason, "type": "BudgetExceeded"}})
        if MAX_OUT_TOKENS:
            for k in ("max_tokens", "max_completion_tokens"):
                if isinstance(body.get(k), int) and body[k] > MAX_OUT_TOKENS:
                    body[k] = MAX_OUT_TOKENS
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
        key_label, key, pace_wait = await get_next_gemini_key(estimated_tokens=gemini_reserved)
        gemini_pos = KEY_LABEL_TO_POS.get(key_label)

        tg = time.time()
        await gemini_gate.acquire()
        gate_held = True
        stats["gemini_inflight"] += 1
        gate_wait = time.time() - tg

        target_backend_model = "gemini/gemini-3.5-flash-lite"
        body["model"] = target_backend_model
        body["api_key"] = key
        tpm_used = get_key_tpm_usage(gemini_pos, time.time()) if gemini_pos is not None else 0
        log.info(
            "[%s] gemini key=%s pace_wait=%.1fs gate_wait=%.1fs inflight=%d tpm=%d/%d",
            rid, key_label, pace_wait, gate_wait, stats["gemini_inflight"], tpm_used, GEMINI_KEY_TPM,
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
        finish_gemini()
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
                finish_gemini(diag["in"] + diag["out"])

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
                finish_gemini()
                release_gate()

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # -- Non-Streaming Response --
    release_gate()
    data = response.model_dump()
    total_time = round(time.time() - t0, 2)
    diag = parse_usage_and_cost(data.get("usage"), target_backend_model)
    finish_gemini(diag["in"] + diag["out"])

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
    is_authed = False
    if not MASTER_KEY:
        is_authed = True
    elif auth_key == MASTER_KEY or request.query_params.get("key") == MASTER_KEY:
        is_authed = True
    elif request.headers.get("Authorization") == f"Bearer {MASTER_KEY}":
        is_authed = True

    if not is_authed:
        raise HTTPException(status_code=401, detail="Invalid Master API Key")

    available_days = get_available_days()
    selected_day = request.query_params.get("day") or ledger["day"]
    if selected_day not in available_days and available_days:
        selected_day = available_days[0]

    rows = read_rows(selected_day)
    fmt = request.query_params.get("fmt")

    if fmt == "json":
        by_model, by_agent = summarize(rows)
        return {
            "day": selected_day,
            "tz": _tz_note,
            "day_usd": round(sum(float(e.get("cost") or 0.0) for e in rows), 5),
            "daily_cap": DAILY_BUDGET_USD,
            "available_days": available_days,
            "by_model": by_model,
            "by_agent": by_agent,
            "tool_calls_ranked": summarize_tools(rows),
            "keys": summarize_keys(rows, time.time()),
        }

    if fmt == "text" or ("curl" in request.headers.get("user-agent", "").lower() and fmt != "html"):
        return PlainTextResponse(usage_text(rows, selected_day))

    auth_param = f"&key={MASTER_KEY}" if (MASTER_KEY and request.query_params.get("key")) else ""
    return HTMLResponse(usage_html(rows, selected_day, available_days, auth_param))


@app.get("/status")
async def status(auth: None = Depends(verify_token)):
    now = time.time()
    return {
        "gemini_inflight": stats["gemini_inflight"],
        "gemini_parallel": GEMINI_PARALLEL,
        "cooldown_s": KEY_COOLDOWN,
        "tpm_limit": GEMINI_KEY_TPM,
        "keys": [
            {
                "key": f"G{n}",
                "ready_in_s": round(get_key_wait(i, now, 0), 1),
                "cooldown_ready_in_s": round(max(0.0, key_last_used[i] + KEY_COOLDOWN - now), 1),
                "tpm_used": get_key_tpm_usage(i, now),
                "tpm_limit": GEMINI_KEY_TPM,
            }
            for i, (n, _) in enumerate(GEMINI_KEYS)
        ],
        "budget": {k: (round(v, 5) if isinstance(v, float) else v) for k, v in ledger.items()},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "4000")))