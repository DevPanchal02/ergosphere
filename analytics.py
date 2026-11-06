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


def sanitize_path(raw_path: str) -> Tuple[str, str]:
    """
    Dynamically strips OS drive prefixes and user home directories WITHOUT any hardcoded folder names.
    Applies a 2-3 segment sliding window so parent directory hierarchies are never doxxed.
    Returns: (rough_directory, detected_os)
    """
    if not raw_path or raw_path.strip() in ("", "-", "unknown", "none"):
        return "terminal/adhoc", "unknown"

    raw = raw_path.strip().replace("\\", "/")

    # 1. OS Detection from path syntax
    detected_os = "unknown"
    if re.match(r"^[A-Za-z]:", raw_path) or "\\" in raw_path:
        detected_os = "windows"
    elif raw.startswith("/Users/"):
        detected_os = "macos"
    elif raw.startswith(("/home/", "/root/")):
        detected_os = "linux"

    # 2. Dynamic Home/User Directory Stripping (Works on any username/drive)
    # Strips 'C:/Users/<username>/', '/Users/<username>/', '/home/<username>/', '/root/'
    cleaned = re.sub(r"^[A-Za-z]:/(?:Users|home)/[^/]+/", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"^/(?:Users|home)/[^/]+/", "", cleaned)
    cleaned = re.sub(r"^/root/", "", cleaned)
    cleaned = cleaned.lstrip("/")

    # 3. Dynamic Sliding Window: Keep the last 2 to 3 meaningful directory segments
    parts = [p for p in cleaned.split("/") if p and p not in (".", "..")]
    if len(parts) >= 3:
        rough_dir = "/".join(parts[-3:])
    elif len(parts) > 0:
        rough_dir = "/".join(parts)
    else:
        rough_dir = "root"

    return rough_dir, detected_os


def detect_caller_and_os(headers: dict, body: dict, client_host: str) -> Tuple[str, str, str]:
    """
    Extracts (caller_type, rough_directory, detected_os) across all scenarios:
      - OpenCode Agents (builder, planner, scout, reviewer)
      - Agent-made tools running in test loops or standalone
      - Manual user projects outside OpenCode
      - Ad-hoc curl / terminal calls
    """
    h = {k.lower(): v for k, v in headers.items()}
    auth = h.get("authorization", "")
    user_param = str(body.get("user") or "").strip()
    user_agent = h.get("user-agent", "").lower()

    caller_type = "User"
    raw_path = ""
    os_hint = "unknown"

    # 1. Check Bearer Token (sk-<type>-<path> or sk-<os>-<project>)
    if "bearer " in auth.lower():
        raw_token = auth[auth.lower().index("bearer ") + 7:].strip()
        if raw_token.lower().startswith("sk-"):
            raw_token = raw_token[3:]

        parts = raw_token.split("-")
        first = parts[0].lower() if parts else ""

        if first in ("agent_tool", "tool"):
            caller_type = "Agent-Tool"
            raw_path = "-".join(parts[1:])
        elif first == "agent":
            if len(parts) > 2 and parts[1].lower() in ("builder", "planner", "scout", "explorer", "reviewer", "coder"):
                caller_type = f"Agent:{parts[1].lower()}"
                raw_path = "-".join(parts[2:])
            else:
                caller_type = "Agent"
                raw_path = "-".join(parts[1:])
        elif first == "user":
            caller_type = "User"
            raw_path = "-".join(parts[1:])
        elif first in ("mac", "darwin"):
            os_hint = "macos"
            raw_path = "-".join(parts[1:])
        elif first in ("pc", "win", "windows"):
            os_hint = "windows"
            raw_path = "-".join(parts[1:])
        elif first in ("linux", "wsl"):
            os_hint = "linux"
            raw_path = "-".join(parts[1:])
        else:
            raw_path = raw_token

    # 2. Check Standard OpenAI "user" field
    if user_param:
        if ":" in user_param:
            prefix, path_val = user_param.split(":", 1)
            p_low = prefix.lower()
            if "tool" in p_low:
                caller_type = "Agent-Tool"
            elif "agent" in p_low:
                caller_type = "Agent"
            else:
                caller_type = "User"
            raw_path = path_val
        else:
            if caller_type == "User":
                raw_path = user_param
            else:
                raw_path = f"{raw_path}/{user_param}" if raw_path else user_param

    # 3. Explicit Headers (if present)
    explicit_path = (
        h.get("x-caller-path") or
        h.get("x-project-dir") or
        h.get("x-file-path") or
        h.get("x-workspace-path") or
        h.get("x-project-path") or
        ""
    ).strip()
    if explicit_path:
        raw_path = explicit_path

    if h.get("x-caller-type"):
        caller_type = h["x-caller-type"]
    elif h.get("x-agent-role") or h.get("x-opencode-agent"):
        caller_type = f"Agent:{h.get('x-agent-role') or h.get('x-opencode-agent')}"

    # 4. OpenCode System Prompt Sniffing (For meta-agent loops)
    for msg in (body.get("messages") or []):
        if isinstance(msg, dict) and msg.get("role") in ("system", "user"):
            content = str(msg.get("content") or "").lower()
            for role in ("builder", "planner", "scout", "explorer", "reviewer", "coder"):
                if f"you are the {role}" in content or f"you are a {role}" in content:
                    caller_type = f"Agent:{role}"
                    break

            # Extract relative project directory mentioned in prompt if raw_path is still empty
            if not raw_path:
                m = re.search(r"(?:projects|workspace|repos)/([a-zA-Z0-9_\-\./]+)", content)
                if m:
                    raw_path = m.group(0).rstrip("/.")

    # 5. OS Detection from Client Environment / Network
    if h.get("x-client-os") or h.get("x-host-os"):
        os_hint = (h.get("x-client-os") or h.get("x-host-os")).lower()
    elif "darwin" in user_agent or "macintosh" in user_agent or "mac os" in user_agent:
        os_hint = "macos"
    elif "windows" in user_agent:
        os_hint = "windows"
    elif "linux" in user_agent:
        os_hint = "linux"

    # Sanitize directory path without hardcoded safe-folders
    rough_dir, path_os = sanitize_path(raw_path)

    final_os = path_os if path_os != "unknown" else os_hint
    if final_os == "unknown":
        if client_host in ("127.0.0.1", "localhost", "host.docker.internal"):
            final_os = "windows"
        else:
            final_os = "macos"

    return caller_type, rough_dir, final_os

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
    by_caller = defaultdict(lambda: {
        "reqs": 0, "usd": 0.0, "in": 0, "out": 0, "errors": 0,
        "caller": "User", "dir": "adhoc", "os": "unknown"
    })
    by_agent = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "errors": 0})
    tool_counts = defaultdict(int)

    for r in rows:
        m = r.get("m", "unknown")
        
        # Backwards-compatible extraction
        caller_type = r.get("caller") or "User"
        caller_dir = r.get("dir")
        caller_os = r.get("os") or "unknown"
        if not caller_dir:
            # If an old log format exists without 'dir', sanitize it now
            s_dir, s_os = sanitize_path(caller_type)
            caller_dir = s_dir
            if caller_os == "unknown":
                caller_os = s_os
            caller_type = "Agent" if "agent" in str(r.get("caller")).lower() else "User"

        caller_key = f"{caller_type}::{caller_dir}"

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

        bc = by_caller[caller_key]
        bc["caller"] = caller_type
        bc["dir"] = caller_dir
        bc["os"] = caller_os
        bc["reqs"] += 1
        bc["usd"] += cost
        bc["in"] += p_in
        bc["out"] += p_out
        if is_err:
            bc["errors"] += 1

        for t in r.get("tools_called") or []:
            if t and t != "?":
                tool_counts[t] += 1

    ranked_tools = sorted(tool_counts.items(), key=lambda item: item[1], reverse=True)
    return dict(by_model), dict(by_caller), dict(by_agent), ranked_tools