import os
import re
import json
import time
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional, Tuple, Dict, Any, List

import config

def sanitize_caller(raw: Optional[str]) -> Tuple[str, str]:
    if not raw or not raw.strip():
        return "unknown", "unknown"
    
    raw_str = raw.strip()
    detected_os = "unknown"
    
    if re.match(r"^[A-Za-z]:[\\/]", raw_str) or "\\" in raw_str:
        detected_os = "windows"
    elif raw_str.startswith("/Users/"):
        detected_os = "macos"
    elif raw_str.startswith(("/home/", "/root/")):
        detected_os = "linux"
        
    cleaned = raw_str.replace("\\", "/")
    cleaned = re.sub(r"^[A-Za-z]:/(?:Users|home)/[^/]+/", "", cleaned)
    cleaned = re.sub(r"^/(?:Users|home)/[^/]+/", "", cleaned)
    
    for anchor in ("event-horizon/", "projects/", ".opencode/"):
        if anchor in cleaned:
            return cleaned[cleaned.index(anchor):], detected_os
            
    parts = [p for p in cleaned.split("/") if p]
    if len(parts) > 2:
        return "/".join(parts[-3:]), detected_os
        
    return cleaned, detected_os

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
    
    m = re.search(r'"message":\s*"([^"]+)"', clean)
    if m:
        return m.group(1)[:120]
    return clean.split("\n")[0][:120]

def row_day(entry: dict) -> Optional[str]:
    ts_str = entry.get("ts")
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
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

# Finds all diagnostic log files while ignoring the incidents directory
def get_all_log_files() -> List[str]:
    found = []
    for root, dirs, files in os.walk(config.LOGS_DIR):
        if "incidents" in root:
            continue
        for f in files:
            if f.endswith(".jsonl") or (f.startswith("diagnostics") and not f.endswith(".tmp")):
                found.append(os.path.join(root, f))
    return sorted(found)

def get_today_log_path(day_str: str) -> str:
    return os.path.join(config.LOGS_DIR, f"diagnostics_{day_str}.jsonl")

def log_diagnostic(entry: dict):
    day_str = entry.get("ts", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_path = get_today_log_path(day_str)
    try:
        line = json.dumps(entry, separators=(",", ":"))
        with open(target_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        config.log.warning("Could not write diagnostic row: %s", e)

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

# Reads all log rows matching a day, with automatic fallback to filename matching
def read_day_rows(day_str: str) -> list[dict]:
    rows = []
    seen_keys = set()
    all_files = get_all_log_files()

    # Pass 1: Match rows strictly by their recorded timestamp date
    for path in all_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if stripped := line.strip():
                        try:
                            entry = json.loads(stripped)
                            if row_day(entry) == day_str:
                                dedup_key = (entry.get("ts"), entry.get("rid"))
                                if dedup_key not in seen_keys:
                                    seen_keys.add(dedup_key)
                                    rows.append(entry)
                        except ValueError:
                            continue
        except OSError:
            continue

    # Pass 2 Fallback: If 0 rows matched by timestamp, but a file literally has that date in its name
    # (like diagnostics_2026-10-17), load the rows from that file directly so it's never empty
    if not rows:
        for path in all_files:
            if day_str in os.path.basename(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            if stripped := line.strip():
                                try:
                                    entry = json.loads(stripped)
                                    dedup_key = (entry.get("ts"), entry.get("rid"))
                                    if dedup_key not in seen_keys:
                                        seen_keys.add(dedup_key)
                                        rows.append(entry)
                                except ValueError:
                                    continue
                except OSError:
                    continue

    rows.sort(key=lambda r: r.get("ts", ""))
    return rows

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

def summarize_rows(rows: list[dict]) -> Tuple[Dict[str, Any], Dict[str, Any], list[Tuple[str, int]]]:
    by_model = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "errors": 0})
    by_caller = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "out": 0, "errors": 0, "os": "unknown"})
    tool_counts = defaultdict(int)

    for r in rows:
        m = r.get("m", "unknown")
        raw_caller = r.get("caller")
        if raw_caller and raw_caller != "unknown":
            caller = raw_caller
        elif r.get("tool_names"):
            caller = ",".join(r["tool_names"][:2]) + ("..." if len(r["tool_names"]) > 2 else "")
        else:
            caller = f"{r.get('tools_in', 0)} tools"

        caller_os = r.get("os", "agent" if not raw_caller or raw_caller == "unknown" else "?")
        is_err = bool(r.get("err")) or int(r.get("status") or 200) >= 400
        cost = float(r.get("cost") or 0.0)
        p_in = int(r.get("in") or 0)
        p_cin = int(r.get("c_in") or 0)
        p_out = int(r.get("out") or 0)
        p_rout = int(r.get("r_out") or 0)

        bm = by_model[m]
        bm["reqs"] += 1
        bm["usd"] += cost
        bm["in"] += p_in
        bm["cached"] += p_cin
        bm["out"] += p_out
        bm["reasoning"] += p_rout
        if is_err:
            bm["errors"] += 1

        bc = by_caller[caller]
        bc["reqs"] += 1
        bc["usd"] += cost
        bc["in"] += p_in
        bc["out"] += p_out
        bc["os"] = caller_os
        if is_err:
            bc["errors"] += 1

        for t in r.get("tools_called") or []:
            tool_counts[t] += 1

    ranked_tools = sorted(tool_counts.items(), key=lambda item: item[1], reverse=True)
    return dict(by_model), dict(by_caller), ranked_tools