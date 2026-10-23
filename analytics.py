import os
import re
import json
import time
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional, Tuple, Dict, Any, List

import config

# In-memory cache for parsed log rows: path -> {"mtime": mtime, "size": size, "rows": [...]}
_FILE_CACHE: Dict[str, Dict[str, Any]] = {}

def detect_caller_and_os(headers: dict, body: dict, client_host: str) -> Tuple[str, str]:
    h = {k.lower(): v for k, v in headers.items()}
    raw_caller = (
        h.get("x-agent-id") or
        h.get("x-caller-id") or
        h.get("x-agent-role") or
        h.get("x-role") or
        h.get("x-opencode-agent") or
        h.get("x-project-path") or
        h.get("x-workspace-path") or
        body.get("agent_id") or
        body.get("agent") or
        body.get("role") or
        body.get("user") or
        ""
    ).strip()

    user_agent = h.get("user-agent", "")
    client_os = h.get("x-client-os") or h.get("x-host-os") or ""
    detected_os = "unknown"

    if client_os:
        detected_os = client_os.lower()
    elif re.match(r"^[A-Za-z]:[\\/]", raw_caller) or "\\" in raw_caller or "windows" in user_agent.lower():
        detected_os = "windows"
    elif raw_caller.startswith("/Users/") or "macintosh" in user_agent.lower() or "darwin" in user_agent.lower():
        detected_os = "macos"
    elif raw_caller.startswith(("/home/", "/root/")) or "linux" in user_agent.lower():
        detected_os = "linux"

    lower_caller = raw_caller.lower()
    for role in ("builder", "planner", "scout", "explorer", "architect", "coder", "reviewer"):
        if role in lower_caller:
            return role, detected_os

    for msg in (body.get("messages") or []):
        if isinstance(msg, dict) and msg.get("role") in ("system", "user"):
            content = str(msg.get("content") or "").lower()
            for role in ("builder", "planner", "scout", "explorer"):
                if f"you are the {role}" in content or f"role: {role}" in content:
                    return role, detected_os

    if raw_caller and raw_caller != client_host:
        cleaned = raw_caller.replace("\\", "/")
        cleaned = re.sub(r"^[A-Za-z]:/(?:Users|home)/[^/]+/", "", cleaned)
        cleaned = re.sub(r"^/(?:Users|home)/[^/]+/", "", cleaned)

        for anchor in ("event-horizon/", "projects/", ".opencode/", "workspace/", "repos/"):
            if anchor in cleaned:
                return cleaned[cleaned.index(anchor):], detected_os

        parts = [p for p in cleaned.split("/") if p]
        if len(parts) > 2:
            return "/".join(parts[-3:]), detected_os
        if cleaned:
            return cleaned, detected_os

    return "agent", detected_os

def clean_error_message(msg: str) -> str:
    clean = config.scrub(str(msg))
    if "401" in clean or "403" in clean:
        return "401/403: Invalid/Revoked API Key"
    if "429" in clean or "ResourceExhausted" in clean:
        if "Tokens per minute" in clean or "TPM" in clean:
            return "429: TPM limit reached"
        if "Requests per minute" in clean or "RPM" in clean:
            return "429: RPM limit reached"
        return "429: Rate limited"
    if "400" in clean and "model turn" in clean:
        return "400: Turn order violation"
    if "503" in clean or "Overloaded" in clean:
        return "503: Service overloaded"

    m = re.search(r'"message":\s*"([^"]+)"', clean)
    if m:
        return m.group(1)[:120]
    return clean.split("\n")[0][:120]

def row_day(entry: dict) -> Optional[str]:
    ts_str = entry.get("ts")
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(ts_str)
        return dt.astimezone(config.TZ).strftime("%Y-%m-%d")
    except Exception:
        return ts_str[:10]

def row_timestamp(entry: dict) -> float:
    ts_str = entry.get("ts")
    if not ts_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0

def get_all_log_files() -> List[str]:
    found = []
    for root, dirs, files in os.walk(config.LOGS_DIR):
        if "incidents" in root:
            continue
        for f in files:
            if f.endswith(".jsonl") or (f.startswith("diagnostics") and not f.endswith(".tmp") and not f.endswith(".json")):
                found.append(os.path.join(root, f))
    return sorted(found)

def get_today_log_path(day_str: str) -> str:
    return os.path.join(config.LOGS_DIR, f"diagnostics_{day_str}.jsonl")

def log_diagnostic(entry: dict):
    local_day = row_day(entry) or datetime.now(config.TZ).strftime("%Y-%m-%d")
    target_path = get_today_log_path(local_day)
    try:
        line = json.dumps(entry, separators=(",", ":"))
        with open(target_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        config.log.warning("Could not write diagnostic row: %s", e)

def capture_incident_payload(rid: str, cost: float, prompt_tokens: int, payload: dict):
    if cost < config.HIGH_COST_THRESHOLD_USD and prompt_tokens < config.HIGH_TOKEN_THRESHOLD:
        return

    day_str = datetime.now(config.TZ).strftime("%Y-%m-%d")
    target_dir = os.path.join(config.INCIDENTS_DIR, day_str)
    os.makedirs(target_dir, exist_ok=True)

    file_name = f"{rid}_usd{cost:.3f}_tok{prompt_tokens}.json"
    file_path = os.path.join(target_dir, file_name)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        config.log.info("Incident blackbox saved: %s (cost=$%.4f, tokens=%d)", file_name, cost, prompt_tokens)
    except OSError as e:
        config.log.warning("Could not save incident payload: %s", e)

def get_all_rows_cached() -> List[dict]:
    all_files = get_all_log_files()
    combined_rows = []
    seen = set()

    for path in all_files:
        try:
            stat = os.stat(path)
            size, mtime = stat.st_size, stat.st_mtime
            cached = _FILE_CACHE.get(path)

            if cached and cached["size"] == size and cached["mtime"] == mtime:
                rows = cached["rows"]
            else:
                rows = []
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if stripped := line.strip():
                            try:
                                rows.append(json.loads(stripped))
                            except ValueError:
                                continue
                _FILE_CACHE[path] = {"size": size, "mtime": mtime, "rows": rows}

            for r in rows:
                key = (r.get("ts"), r.get("rid"))
                if key not in seen:
                    seen.add(key)
                    combined_rows.append(r)
        except OSError:
            continue

    combined_rows.sort(key=lambda r: r.get("ts", ""))
    return combined_rows

def read_day_rows(day_str: str) -> list[dict]:
    """
    Two-pass loader:
    Pass 1 matches rows strictly by local date.
    Pass 2 fallback loads files named after the date (e.g. diagnostics_2026-10-17...) if 0 rows matched by timestamp.
    """
    all_rows = get_all_rows_cached()
    matched = [r for r in all_rows if row_day(r) == day_str]

    if not matched:
        all_files = get_all_log_files()
        seen_keys = set()
        for path in all_files:
            if day_str in os.path.basename(path):
                cached = _FILE_CACHE.get(path)
                rows = cached["rows"] if cached else []
                if not rows:
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            for line in f:
                                if stripped := line.strip():
                                    try:
                                        rows.append(json.loads(stripped))
                                    except ValueError:
                                        continue
                    except OSError:
                        continue
                for r in rows:
                    key = (r.get("ts"), r.get("rid"))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        matched.append(r)

    matched.sort(key=lambda r: r.get("ts", ""))
    return matched

def get_rolling_rpm(rows: list[dict], now: float) -> int:
    cutoff = now - 60.0
    return sum(1 for r in rows if row_timestamp(r) >= cutoff)

def parse_usage_and_cost(usage_obj: Any, target_model: str) -> dict:
    if not usage_obj:
        return {"in": 0, "c_in": 0, "out": 0, "r_out": 0, "cost": 0.0}

    getter = (lambda k: usage_obj.get(k)) if isinstance(usage_obj, dict) else (lambda k: getattr(usage_obj, k, None))

    prompt_tok = getter("prompt_tokens") or 0
    comp_tok = getter("completion_tokens") or 0

    p_det = getter("prompt_tokens_details")
    cached_tok = 0
    if p_det:
        cached_tok = (p_det.get("cached_tokens") if isinstance(p_det, dict) else getattr(p_det, "cached_tokens", 0)) or 0

    c_det = getter("completion_tokens_details")
    reason_tok = 0
    if c_det:
        reason_tok = (c_det.get("reasoning_tokens") if isinstance(c_det, dict) else getattr(c_det, "reasoning_tokens", 0)) or 0

    prices = config.PRICING.get(target_model, (0.0, 0.0, 0.0))
    uncached_in = max(0, prompt_tok - cached_tok)
    cost = (uncached_in * prices[0] + cached_tok * prices[1] + comp_tok * prices[2]) / 1_000_000.0

    return {
        "in": prompt_tok,
        "c_in": cached_tok,
        "out": comp_tok,
        "r_out": reason_tok,
        "cost": round(cost, 5),
    }

def summarize_rows(rows: list[dict]):
    by_model = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "errors": 0})
    by_caller = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "out": 0, "errors": 0, "os": "unknown"})
    by_agent = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "errors": 0})
    tool_counts = defaultdict(int)

    for r in rows:
        m = r.get("m", "unknown")
        caller = r.get("caller") or "agent"
        caller_os = r.get("os", "unknown")

        names = r.get("tool_names") or []
        sig = ",".join(names[:3]) + ("..." if len(names) > 3 else "") if names else f"{r.get('tools_in', 0)} tools"
        agent_key = f"{m} | {sig}"

        is_err = bool(r.get("err")) or int(r.get("status") or 200) >= 400
        cost = float(r.get("cost") or 0.0)
        p_in = int(r.get("in") or 0)
        p_cin = int(r.get("c_in") or 0)
        p_out = int(r.get("out") or 0)
        p_rout = int(r.get("r_out") or 0)

        for b in (by_model[m], by_agent[agent_key]):
            b["reqs"] += 1
            b["usd"] += cost
            b["in"] += p_in
            b["cached"] += p_cin
            b["out"] += p_out
            b["reasoning"] += p_rout
            if is_err:
                b["errors"] += 1

        bc = by_caller[caller]
        bc["reqs"] += 1
        bc["usd"] += cost
        bc["in"] += p_in
        bc["out"] += p_out
        bc["os"] = caller_os
        if is_err:
            bc["errors"] += 1

        for t in r.get("tools_called") or []:
            if t and t != "?":
                tool_counts[t] += 1

    ranked_tools = sorted(tool_counts.items(), key=lambda item: item[1], reverse=True)
    return dict(by_model), dict(by_caller), dict(by_agent), ranked_tools