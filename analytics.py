import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional, Tuple, Dict, Any, List

import config

# In-memory file cache maintained for backwards compatibility with legacy file readers
_FILE_CACHE: Dict[str, Dict[str, Any]] = {}


def sanitize_path(raw_path: str) -> Tuple[str, str]:
    """
    Dynamically strips drive letters, WSL mount roots, and arbitrary user home directories.
    Applies a 3-directory sliding window and trims trailing file extensions so personal
    filesystem hierarchies are never logged.
    Returns: (sanitized_directory, detected_os)
    """
    if not raw_path or raw_path.strip() in ("", "-", "unknown", "none"):
        return "terminal/adhoc", "unknown"

    raw = raw_path.strip().replace("\\", "/")

    # Detect operating system from path syntax signatures
    detected_os = "unknown"
    if re.match(r"^[A-Za-z]:", raw_path) or "\\" in raw_path:
        detected_os = "windows"
    elif raw.startswith(("/mnt/", "mnt/")):
        detected_os = "wsl"
    elif raw.startswith(("/Users/", "Users/")):
        detected_os = "macos"
    elif raw.startswith(("/home/", "/root/", "home/", "root/")):
        detected_os = "linux"
    elif raw.startswith(("/workspace/", "workspace/", "/app/", "app/")):
        detected_os = "docker"

    # Strip Windows drive letters (e.g., 'C:/')
    cleaned = re.sub(r"^[A-Za-z]:/", "", raw, flags=re.IGNORECASE)

    # Strip WSL mount points (e.g., '/mnt/c/' or 'mnt/c/')
    cleaned = re.sub(r"^/?mnt/[A-Za-z]/", "", cleaned, flags=re.IGNORECASE)

    # Strip dynamic user homes without hardcoding usernames (e.g., '/Users/john/', '/home/ubuntu/')
    cleaned = re.sub(r"^/?(?:Users|home)/[^/]+/", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^/?root/", "", cleaned)
    cleaned = cleaned.lstrip("/")

    # Tokenize path into meaningful folder segments
    segments = [p for p in cleaned.split("/") if p and p not in (".", "..")]

    # Trim file name if the final segment has a file extension (e.g., 'server.py' -> popped)
    if segments and re.search(r"^[^.].*\.[a-zA-Z0-9]{1,8}$", segments[-1]):
        segments.pop()

    # Apply 3-directory sliding window: keep at most the last 3 directories
    if len(segments) >= 3:
        rough_dir = "/".join(segments[-3:])
    elif len(segments) > 0:
        rough_dir = "/".join(segments)
    else:
        rough_dir = "root" if detected_os != "unknown" else "terminal/adhoc"

    return rough_dir, detected_os


# Alias for backward compatibility with older test suites
sanitize_caller = sanitize_path


def parse_bearer_token(auth_header: str, master_key: Optional[str] = None) -> Tuple[bool, str, str, str]:
    """
    Extracts (is_valid, caller_role, os_hint, raw_project_path) from Authorization Bearer token.
    Format: Bearer <MASTER_KEY>-<caller/role>-<os>-<project> or Bearer sk-<role>-<path>
    """
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return False, "", "", ""

    raw_token = auth_header[7:].strip()

    # Verify Master Key secret prefix if configured in environment
    if master_key:
        if not raw_token.startswith(master_key):
            return False, "", "", ""
        metadata_payload = raw_token[len(master_key):].lstrip("-_")
    else:
        metadata_payload = raw_token.removeprefix("Bearer ").strip()
        if metadata_payload.lower().startswith("sk-"):
            metadata_payload = metadata_payload[3:]

    if not metadata_payload:
        return True, "User", "", ""

    # Split positional metadata: up to 3 parts (caller, os, project_path)
    parts = metadata_payload.split("-", 2)
    caller_role = parts[0] if len(parts) > 0 else ""
    os_hint = parts[1] if len(parts) > 1 else ""
    raw_project_path = parts[2] if len(parts) > 2 else ""

    # If only 2 parts were provided, the second part is treated as the project path
    if len(parts) == 2:
        raw_project_path = parts[1]
        os_hint = ""

    return True, caller_role, os_hint, raw_project_path


def verify_master_key(auth_header: str) -> bool:
    """Verifies that the incoming Authorization header satisfies MASTER_KEY requirements."""
    if not config.MASTER_KEY:
        return True
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return False
    token = auth_header[7:].strip()
    return token.startswith(config.MASTER_KEY)


def detect_caller_and_os(headers: dict, body: dict, client_host: str) -> Tuple[str, str, str]:
    """
    Dynamically identifies (caller_type, caller_dir, caller_os) across all callers:
      1. Bearer token metadata
      2. OpenAI standard 'user' parameter
      3. Explicit X- headers
      4. Dynamic OpenCode system prompt regex sniffer
      5. Network origin (Docker sandbox vs Host vs LAN)
    """
    h = {k.lower(): v for k, v in headers.items()}
    auth = h.get("authorization", "")
    user_param = str(body.get("user") or "").strip()
    user_agent = h.get("user-agent", "").lower()

    caller_type = "User"
    raw_path = ""
    os_hint = "unknown"

    # 1. Parse Bearer Token metadata
    _, b_caller, b_os, b_path = parse_bearer_token(auth, config.MASTER_KEY)
    if b_caller:
        c_low = b_caller.lower()
        if c_low in ("agent_tool", "agent-tool", "tool"):
            caller_type = "Agent-Tool"
        elif c_low.startswith(("agent:", "agent_", "agent-")):
            role_name = re.sub(r"^agent[:_\-]", "", c_low)
            caller_type = f"Agent:{role_name}"
        elif c_low == "agent":
            caller_type = "Agent"
        elif c_low == "user":
            caller_type = "User"
        elif c_low in ("win", "windows", "pc"):
            os_hint = "windows"
        elif c_low in ("mac", "macos", "darwin"):
            os_hint = "macos"
        elif c_low in ("linux", "wsl"):
            os_hint = "linux"
        elif c_low == "docker":
            os_hint = "docker"

    if b_os:
        os_hint = b_os.lower()
    if b_path:
        raw_path = b_path

    # 2. Check OpenAI standard 'user' parameter
    if user_param:
        if ":" in user_param:
            prefix, path_val = user_param.split(":", 1)
            p_low = prefix.lower()
            if "tool" in p_low:
                caller_type = "Agent-Tool"
            elif "agent" in p_low:
                caller_type = "Agent"
            raw_path = path_val
        else:
            if caller_type == "User":
                raw_path = user_param
            elif not raw_path:
                raw_path = user_param

    # 3. Explicit headers
    explicit_path = (
        h.get("x-caller-path") or
        h.get("x-project-dir") or
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

    # 4. OpenCode dynamic system prompt sniffer: extracts roles like 'Agent:builder', 'Agent:qa-tester'
    for msg in (body.get("messages") or []):
        if isinstance(msg, dict) and msg.get("role") in ("system", "user"):
            content = str(msg.get("content") or "")
            # Sniff role name, allowing optional qualifying adjectives like 'experienced', 'senior', 'lead'
            m_role = re.search(
                r"you are (?:a|an|the)\s+(?:(?:experienced|senior|lead|expert|principal)\s+)?([a-zA-Z0-9_\-]+)",
                content,
                re.IGNORECASE,
            )
            if m_role and caller_type in ("User", "Agent"):
                extracted = m_role.group(1).lower()
                caller_type = f"Agent:{extracted}"

            # Extract relative project workspace if not yet specified
            if not raw_path:
                m_proj = re.search(r"(?:projects|workspace|repos)/([a-zA-Z0-9_\-\./]+)", content)
                if m_proj:
                    raw_path = m_proj.group(0).rstrip("/.")

    # 5. OS detection fallback ladder: User-Agent and network origin
    if h.get("x-client-os") or h.get("x-host-os"):
        os_hint = (h.get("x-client-os") or h.get("x-host-os")).lower()
    elif "darwin" in user_agent or "macintosh" in user_agent:
        os_hint = "macos"
    elif "windows" in user_agent:
        os_hint = "windows"
    elif "linux" in user_agent:
        os_hint = "linux"

    # Sanitize path hierarchy
    sanitized_dir, path_os = sanitize_path(raw_path)

    # Determine final operating system
    final_os = path_os if path_os != "unknown" else os_hint
    if final_os == "unknown":
        if client_host in ("127.0.0.1", "localhost", "::1"):
            final_os = "windows"
        elif client_host.startswith("172."):
            final_os = "docker"
        elif client_host.startswith(("192.168.", "10.")):
            final_os = "macos"

    return caller_type, sanitized_dir, final_os


def derive_request_context(headers: dict, body: dict) -> Tuple[str, int, Dict[str, int]]:
    """
    Derives deterministic session ID, turn index counter, and tokens partitioned by message role.
    """
    h = {k.lower(): v for k, v in headers.items()}
    messages = body.get("messages") or []

    # 1. Derive Session ID from headers or root prompt hash
    session_id = h.get("x-session-id") or h.get("x-conversation-id")
    if not session_id:
        root_content = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") in ("system", "user"):
                root_content += str(m.get("content") or "")
                if len(root_content) > 300:
                    break
        if root_content:
            session_id = hashlib.sha256(root_content.encode("utf-8")).hexdigest()[:16]
        else:
            session_id = "adhoc-session"

    # 2. Derive Turn Index: count completed user questions and tool turns
    turn_index = sum(1 for m in messages if isinstance(m, dict) and m.get("role") in ("user", "tool"))

    # 3. Partition Prompt Tokens by Role (system, user, assistant, tool)
    tokens_by_role = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    try:
        import litellm
        for m in messages:
            if isinstance(m, dict):
                role = m.get("role", "user")
                target_role = role if role in tokens_by_role else "user"
                tok_count = litellm.token_counter(model=config.PRIMARY_GEMINI_MODEL, messages=[m])
                tokens_by_role[target_role] += tok_count
    except Exception:
        # Fallback character approximation (approx 4 chars per token)
        for m in messages:
            if isinstance(m, dict):
                role = m.get("role", "user")
                target_role = role if role in tokens_by_role else "user"
                content_len = len(str(m.get("content") or ""))
                tokens_by_role[target_role] += max(1, content_len // 4)

    return session_id, turn_index, tokens_by_role


def clean_error_message(msg: str) -> str:
    """Cleans raw upstream provider errors into concise diagnostic status messages."""
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


def parse_usage_and_cost(usage_obj: Any, target_model: str) -> dict:
    """Parses prompt, cached, completion, and reasoning tokens, computing cost via PRICING ledger."""
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
    """Aggregates request telemetry across models, callers, and tool invocations."""
    by_model = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "errors": 0})
    by_caller = defaultdict(lambda: {
        "reqs": 0, "usd": 0.0, "in": 0, "out": 0, "errors": 0,
        "caller": "User", "dir": "adhoc", "os": "unknown"
    })
    by_agent = defaultdict(lambda: {"reqs": 0, "usd": 0.0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "errors": 0})
    tool_counts = defaultdict(int)

    for r in rows:
        m = r.get("m") or r.get("model_requested") or "unknown"
        caller_type = r.get("caller") or r.get("caller_type") or "User"
        caller_dir = r.get("dir") or r.get("caller_dir") or "adhoc"
        caller_os = r.get("os") or r.get("caller_os") or "unknown"
        caller_key = f"{caller_type}::{caller_dir}"

        names = r.get("tool_names") or []
        sig = ",".join(names[:3]) + ("..." if len(names) > 3 else "") if names else f"{r.get('tools_in', 0)} tools"
        agent_key = f"{m} | {sig}"

        is_err = bool(r.get("err") or r.get("error_message")) or int(r.get("status") or r.get("status_code") or 200) >= 400
        cost = float(r.get("cost") or r.get("cost_usd") or 0.0)
        p_in = int(r.get("in") or r.get("tokens_prompt_total") or 0)
        p_cin = int(r.get("c_in") or r.get("tokens_prompt_cached") or 0)
        p_out = int(r.get("out") or r.get("tokens_completion") or 0)
        p_rout = int(r.get("r_out") or r.get("tokens_reasoning") or 0)

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

        tools_called = r.get("tools_called") or []
        if isinstance(tools_called, str):
            try:
                tools_called = json.loads(tools_called)
            except Exception:
                tools_called = []
        for t in tools_called:
            if t and t != "?":
                tool_counts[t] += 1

    ranked_tools = sorted(tool_counts.items(), key=lambda item: item[1], reverse=True)
    return dict(by_model), dict(by_caller), dict(by_agent), ranked_tools


# --- Legacy File Diagnostics Compatibility Helpers (used until downstream chunks) ---

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


def get_all_log_files() -> List[str]:
    found = []
    for root, _, files in os.walk(config.LOGS_DIR):
        if "incidents" in root:
            continue
        for f in files:
            if f.endswith(".jsonl") or (f.startswith("diagnostics") and not f.endswith(".tmp") and not f.endswith(".json")):
                found.append(os.path.join(root, f))
    return sorted(found)


def read_day_rows(day_str: str) -> list[dict]:
    # Reads legacy jsonl rows or returns empty list
    all_files = get_all_log_files()
    rows = []
    for path in all_files:
        if day_str in os.path.basename(path):
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
    return rows


def get_rolling_rpm(rows: list[dict], now: float) -> int:
    cutoff = now - 60.0
    return sum(1 for r in rows if row_timestamp(r) >= cutoff)