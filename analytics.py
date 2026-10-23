import os
import re
import json
import time
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional, Tuple, Dict, Any

import config

# Strips local user directories and identifies caller OS from raw path structure
def sanitize_caller(raw: Optional[str]) -> Tuple[str, str]:
    if not raw or not raw.strip():
        return "unknown", "unknown"
    
    raw_str = raw.strip()
    detected_os = "unknown"
    
    # Identify host operating system signatures before normalizing slashes
    if re.match(r"^[A-Za-z]:[\\/]", raw_str) or "\\" in raw_str:
        detected_os = "windows"
    elif raw_str.startswith("/Users/"):
        detected_os = "macos"
    elif raw_str.startswith(("/home/", "/root/")):
        detected_os = "linux"
        
    cleaned = raw_str.replace("\\", "/")
    
    # Strip OS-specific home user directory prefixes
    cleaned = re.sub(r"^[A-Za-z]:/(?:Users|home)/[^/]+/", "", cleaned)
    cleaned = re.sub(r"^/(?:Users|home)/[^/]+/", "", cleaned)
    
    # Anchor to known workspace paths if present
    for anchor in ("event-horizon/", "projects/", ".opencode/"):
        if anchor in cleaned:
            return cleaned[cleaned.index(anchor):], detected_os
            
    # Fall back to the last 2-3 path segments
    parts = [p for p in cleaned.split("/") if p]
    if len(parts) > 2:
        return "/".join(parts[-3:]), detected_os
        
    return cleaned, detected_os

# Condenses verbose nested SDK exceptions down to clean root causes
def clean_error_message(msg: str) -> str:
    clean = config.scrub(str(msg))
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
    
    # Extract inner message text if wrapped in JSON
    m = re.search(r'"message":\s*"([^"]+)"', clean)
    if m:
        return m.group(1)[:120]
    return clean.split("\n")[0][:120]

def get_today_log_path(day_str: str) -> str:
    return os.path.join(config.LOGS_DIR, f"diagnostics_{day_str}.jsonl")

# Writes compact metadata telemetry to today's log partition
def log_diagnostic(entry: dict):
    day_str = entry.get("ts", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_path = get_today_log_path(day_str)
    try:
        line = json.dumps(entry, separators=(",", ":"))
        with open(target_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        config.log.warning("Could not write diagnostic row: %s", e)

# Dumps the entire request/response payload only when spend or tokens breach incident thresholds
def capture_incident_payload(rid: str, cost: float, prompt_tokens: int, payload: dict):
    if cost < config.HIGH_COST_THRESHOLD_USD and prompt_tokens < config.HIGH_TOKEN_THRESHOLD:
        return

    day_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

# Reads only the requested day's file to avoid slow disk scans across weeks of history
def read_day_rows(day_str: str) -> list[dict]:
    target_path = get_today_log_path(day_str)
    if not os.path.isfile(target_path):
        return []
    
    rows = []
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            for line in f:
                if stripped := line.strip():
                    try:
                        rows.append(json.loads(stripped))
                    except ValueError:
                        continue
    except OSError:
        return []
    return rows

# Extracts token breakdowns and calculates exact USD cost based on model pricing tables
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

# Aggregates log entries into summary tables for models, callers, and tool rankings
def summarize_rows(rows: list[dict]) -> Tuple[Dict[str, Any], Dict[str, Any], list[Tuple[str, int]]]:
    by_model = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "errors": 0})
    by_caller = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "out": 0, "errors": 0, "os": "unknown"})
    tool_counts = defaultdict(int)

    for r in rows:
        m = r.get("m", "unknown")
        caller = r.get("caller", "unknown")
        caller_os = r.get("os", "unknown")
        is_err = bool(r.get("err"))
        cost = float(r.get("cost") or 0.0)
        p_in = int(r.get("in") or 0)
        p_cin = int(r.get("c_in") or 0)
        p_out = int(r.get("out") or 0)
        p_rout = int(r.get("r_out") or 0)

        # Model aggregation
        bm = by_model[m]
        bm["reqs"] += 1
        bm["usd"] += cost
        bm["in"] += p_in
        bm["cached"] += p_cin
        bm["out"] += p_out
        bm["reasoning"] += p_rout
        if is_err:
            bm["errors"] += 1

        # Caller attribution
        bc = by_caller[caller]
        bc["reqs"] += 1
        bc["usd"] += cost
        bc["in"] += p_in
        bc["out"] += p_out
        bc["os"] = caller_os
        if is_err:
            bc["errors"] += 1

        # Tool calls ranking
        for t in r.get("tools_called") or []:
            tool_counts[t] += 1

    ranked_tools = sorted(tool_counts.items(), key=lambda item: item[1], reverse=True)
    return dict(by_model), dict(by_caller), ranked_tools