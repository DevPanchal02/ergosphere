import os
import json
import time
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import aiosqlite

import config

# Global database connection and serialization lock for transactions
_db_connection: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()
_active_db_path: str = config.DB_PATH

# SQL Schemas according to Section 3 of the Master Plan
SCHEMA_REQUESTS = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    rid TEXT NOT NULL UNIQUE,
    session_id TEXT,
    turn_index INTEGER DEFAULT 0,
    caller_type TEXT NOT NULL,
    caller_dir TEXT NOT NULL,
    caller_os TEXT NOT NULL,
    client_host TEXT NOT NULL,
    model_requested TEXT NOT NULL,
    model_dispatched TEXT NOT NULL,
    is_stream INTEGER NOT NULL,
    key_label TEXT NOT NULL,
    key_attempts INTEGER DEFAULT 1,
    fallback_used INTEGER DEFAULT 0,
    key_quarantined INTEGER DEFAULT 0,
    quarantine_reason TEXT,
    duration_total_s REAL NOT NULL,
    gate_wait_s REAL DEFAULT 0.0,
    pacing_wait_s REAL DEFAULT 0.0,
    ttft_s REAL DEFAULT 0.0,
    tokens_per_second REAL DEFAULT 0.0,
    status_code INTEGER NOT NULL,
    finish_reason TEXT,
    client_aborted INTEGER DEFAULT 0,
    error_message TEXT,
    tokens_prompt_total INTEGER DEFAULT 0,
    tokens_prompt_cached INTEGER DEFAULT 0,
    tokens_prompt_uncached INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0,
    tokens_reasoning INTEGER DEFAULT 0,
    tokens_by_role TEXT,
    delta_tokens INTEGER DEFAULT 0,
    context_utilization_pct REAL DEFAULT 0.0,
    cache_hit_pct REAL DEFAULT 0.0,
    tools_in_count INTEGER DEFAULT 0,
    tool_names_provided TEXT,
    tools_called TEXT,
    duplicate_tool_loop_warning INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    cache_savings_usd REAL DEFAULT 0.0,
    spend_accum_day_usd REAL DEFAULT 0.0,
    spend_accum_run_usd REAL DEFAULT 0.0
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_requests_day_ts ON requests(day, ts DESC);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS idx_requests_caller ON requests(caller_type, caller_dir);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model_requested);
CREATE INDEX IF NOT EXISTS idx_requests_key ON requests(key_label);
"""

SCHEMA_FLEET_STATE = """
CREATE TABLE IF NOT EXISTS fleet_state (
    key_label TEXT PRIMARY KEY,
    quarantined INTEGER NOT NULL DEFAULT 0,
    quarantine_reason TEXT DEFAULT '',
    rpd_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    day TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

SCHEMA_SPEND_STATE = """
CREATE TABLE IF NOT EXISTS spend_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    daily_cap_usd REAL NOT NULL,
    run_cap_usd REAL NOT NULL,
    day_usd REAL NOT NULL DEFAULT 0.0,
    run_usd REAL NOT NULL DEFAULT 0.0,
    run_last_ts REAL NOT NULL DEFAULT 0.0,
    run_request_count INTEGER NOT NULL DEFAULT 0,
    current_day TEXT NOT NULL
);
"""

SCHEMA_INCIDENTS = """
CREATE TABLE IF NOT EXISTS incidents (
    rid TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    request_headers TEXT NOT NULL,
    request_body TEXT NOT NULL,
    response_payload TEXT NOT NULL,
    token_usage TEXT NOT NULL,
    cost_breakdown TEXT NOT NULL,
    FOREIGN KEY(rid) REFERENCES requests(rid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_incidents_day ON incidents(day);
"""


async def init_db(db_path: Optional[str] = None) -> aiosqlite.Connection:
    # Initializes database connection, applies WAL pragmas, and migrates table schemas
    global _db_connection, _active_db_path

    async with _db_lock:
        if _db_connection is not None:
            await _db_connection.close()
            _db_connection = None

        _active_db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(_active_db_path)), exist_ok=True)

        # Mark worker thread as daemon BEFORE awaiting so it starts as a daemon thread
        conn_proxy = aiosqlite.connect(_active_db_path)
        if hasattr(conn_proxy, "_thread"):
            conn_proxy._thread.daemon = True
        conn = await conn_proxy
        conn.row_factory = aiosqlite.Row

        # Apply high-throughput WAL and memory cache settings
        await conn.execute("PRAGMA journal_mode = WAL;")
        await conn.execute("PRAGMA synchronous = NORMAL;")
        await conn.execute("PRAGMA busy_timeout = 5000;")
        await conn.execute("PRAGMA cache_size = -64000;")
        await conn.execute("PRAGMA foreign_keys = ON;")

        # Run schema migrations
        await conn.execute(SCHEMA_REQUESTS)
        for idx_sql in SCHEMA_INDEXES.strip().split(";"):
            if idx_sql.strip():
                await conn.execute(idx_sql)
        await conn.execute(SCHEMA_FLEET_STATE)
        await conn.execute(SCHEMA_SPEND_STATE)
        for inc_sql in SCHEMA_INCIDENTS.strip().split(";"):
            if inc_sql.strip():
                await conn.execute(inc_sql)

        await conn.commit()
        _db_connection = conn
        config.log.info("Initialized SQLite database at %s with WAL pragmas", _active_db_path)
        return _db_connection


async def get_db() -> aiosqlite.Connection:
    # Lazy initializer ensuring an active connection is returned to caller
    global _db_connection
    if _db_connection is None:
        return await init_db(_active_db_path)
    return _db_connection


async def close_db() -> None:
    # Gracefully closes the database connection if open
    global _db_connection
    async with _db_lock:
        if _db_connection is not None:
            await _db_connection.close()
            _db_connection = None


async def insert_request(row: dict) -> None:
    # Inserts telemetry row into requests table, providing backward compatibility for old field names
    conn = await get_db()

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_local = datetime.now(config.TZ).strftime("%Y-%m-%d")

    # Map legacy diagnostics dictionary keys to canonical database schema
    ts = row.get("ts") or now_utc
    day = row.get("day") or today_local
    rid = row.get("rid") or f"gen_{int(time.time()*1000)}"
    session_id = row.get("session_id")
    turn_index = int(row.get("turn_index") or 0)
    caller_type = row.get("caller_type") or row.get("caller") or "User"
    caller_dir = row.get("caller_dir") or row.get("dir") or "adhoc"
    caller_os = row.get("caller_os") or row.get("os") or "unknown"
    client_host = row.get("client_host") or "unknown"

    model_requested = row.get("model_requested") or row.get("m") or "unknown"
    model_dispatched = row.get("model_dispatched") or model_requested
    is_stream = int(row.get("is_stream", 0))
    key_label = row.get("key_label") or row.get("key") or "-"
    key_attempts = int(row.get("key_attempts") or 1)
    fallback_used = int(row.get("fallback_used") or 0)
    key_quarantined = int(row.get("key_quarantined") or 0)
    quarantine_reason = row.get("quarantine_reason")

    duration_total_s = float(row.get("duration_total_s") or row.get("dur") or 0.0)
    gate_wait_s = float(row.get("gate_wait_s") or 0.0)
    pacing_wait_s = float(row.get("pacing_wait_s") or 0.0)
    ttft_s = float(row.get("ttft_s") or 0.0)
    tokens_per_second = float(row.get("tokens_per_second") or 0.0)

    status_code = int(row.get("status_code") or row.get("status") or 200)
    finish_reason = row.get("finish_reason")
    client_aborted = int(row.get("client_aborted") or 0)
    error_message = row.get("error_message") or row.get("err")

    tokens_prompt_total = int(row.get("tokens_prompt_total") or row.get("in") or 0)
    tokens_prompt_cached = int(row.get("tokens_prompt_cached") or row.get("c_in") or 0)
    tokens_prompt_uncached = int(row.get("tokens_prompt_uncached") or max(0, tokens_prompt_total - tokens_prompt_cached))
    tokens_completion = int(row.get("tokens_completion") or row.get("out") or 0)
    tokens_reasoning = int(row.get("tokens_reasoning") or row.get("r_out") or 0)

    # Serialize JSON structure helpers
    tokens_by_role = row.get("tokens_by_role")
    if isinstance(tokens_by_role, (dict, list)):
        tokens_by_role = json.dumps(tokens_by_role)

    delta_tokens = int(row.get("delta_tokens") or 0)
    context_utilization_pct = float(row.get("context_utilization_pct") or 0.0)
    cache_hit_pct = float(row.get("cache_hit_pct") or ((tokens_prompt_cached / tokens_prompt_total * 100) if tokens_prompt_total else 0.0))

    tools_in_count = int(row.get("tools_in_count") or row.get("tools_in") or 0)
    tool_names = row.get("tool_names_provided") or row.get("tool_names")
    if isinstance(tool_names, list):
        tool_names = json.dumps(tool_names)

    tools_called = row.get("tools_called")
    if isinstance(tools_called, (list, set)):
        tools_called = json.dumps(list(tools_called))

    duplicate_tool_loop_warning = int(row.get("duplicate_tool_loop_warning") or 0)
    cost_usd = float(row.get("cost_usd") or row.get("cost") or 0.0)
    cache_savings_usd = float(row.get("cache_savings_usd") or 0.0)
    spend_accum_day_usd = float(row.get("spend_accum_day_usd") or 0.0)
    spend_accum_run_usd = float(row.get("spend_accum_run_usd") or 0.0)

    sql = """
    INSERT INTO requests (
        ts, day, rid, session_id, turn_index, caller_type, caller_dir, caller_os, client_host,
        model_requested, model_dispatched, is_stream, key_label, key_attempts, fallback_used,
        key_quarantined, quarantine_reason, duration_total_s, gate_wait_s, pacing_wait_s,
        ttft_s, tokens_per_second, status_code, finish_reason, client_aborted, error_message,
        tokens_prompt_total, tokens_prompt_cached, tokens_prompt_uncached, tokens_completion,
        tokens_reasoning, tokens_by_role, delta_tokens, context_utilization_pct, cache_hit_pct,
        tools_in_count, tool_names_provided, tools_called, duplicate_tool_loop_warning,
        cost_usd, cache_savings_usd, spend_accum_day_usd, spend_accum_run_usd
    ) VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?, ?
    );
    """

    params = (
        ts, day, rid, session_id, turn_index, caller_type, caller_dir, caller_os, client_host,
        model_requested, model_dispatched, is_stream, key_label, key_attempts, fallback_used,
        key_quarantined, quarantine_reason, duration_total_s, gate_wait_s, pacing_wait_s,
        ttft_s, tokens_per_second, status_code, finish_reason, client_aborted, error_message,
        tokens_prompt_total, tokens_prompt_cached, tokens_prompt_uncached, tokens_completion,
        tokens_reasoning, tokens_by_role, delta_tokens, context_utilization_pct, cache_hit_pct,
        tools_in_count, tool_names, tools_called, duplicate_tool_loop_warning,
        cost_usd, cache_savings_usd, spend_accum_day_usd, spend_accum_run_usd
    )

    async with _db_lock:
        await conn.execute(sql, params)
        await conn.commit()


async def save_incident(rid: str, cost_usd: float, prompt_tokens: int, payload: dict,
                        ts: Optional[str] = None, day: Optional[str] = None) -> None:
    # Stores incident payload when request exceeds cost or token safety threshold
    conn = await get_db()
    ts_val = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_val = day or datetime.now(config.TZ).strftime("%Y-%m-%d")

    headers_str = json.dumps(payload.get("headers") or {})
    body_str = json.dumps(payload.get("body") or {})
    response_str = json.dumps(payload.get("response") or {})
    usage_str = json.dumps(payload.get("usage") or {})
    breakdown_str = json.dumps(payload.get("cost_breakdown") or {})

    sql = """
    INSERT INTO incidents (
        rid, ts, day, cost_usd, prompt_tokens, request_headers, request_body,
        response_payload, token_usage, cost_breakdown
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(rid) DO UPDATE SET
        cost_usd = excluded.cost_usd,
        prompt_tokens = excluded.prompt_tokens,
        response_payload = excluded.response_payload,
        token_usage = excluded.token_usage;
    """

    async with _db_lock:
        await conn.execute(sql, (
            rid, ts_val, day_val, cost_usd, prompt_tokens,
            headers_str, body_str, response_str, usage_str, breakdown_str
        ))
        await conn.commit()


async def load_fleet_state(day: str) -> Dict[str, dict]:
    # Returns active quarantine status and usage counts for all keys on given day
    conn = await get_db()
    cursor = await conn.execute("SELECT * FROM fleet_state WHERE day = ?;", (day,))
    rows = await cursor.fetchall()
    return {
        r["key_label"]: {
            "key_label": r["key_label"],
            "quarantined": bool(r["quarantined"]),
            "quarantine_reason": r["quarantine_reason"],
            "rpd_count": r["rpd_count"],
            "error_count": r["error_count"],
            "day": r["day"],
            "updated_at": r["updated_at"]
        } for r in rows
    }


async def save_key_quarantine(key_label: str, quarantined: bool, reason: str, day: str) -> None:
    # Persists immediate key quarantine or revival across process restarts
    conn = await get_db()
    now_ts = time.time()
    sql = """
    INSERT INTO fleet_state (key_label, quarantined, quarantine_reason, rpd_count, error_count, day, updated_at)
    VALUES (?, ?, ?, 0, 0, ?, ?)
    ON CONFLICT(key_label) DO UPDATE SET
        quarantined = excluded.quarantined,
        quarantine_reason = excluded.quarantine_reason,
        updated_at = excluded.updated_at;
    """
    async with _db_lock:
        await conn.execute(sql, (key_label, int(quarantined), reason, day, now_ts))
        await conn.commit()


async def update_key_usage(key_label: str, rpd_count: int, error_count: int, day: str) -> None:
    # Updates today's daily request and error counters for a Gemini key
    conn = await get_db()
    now_ts = time.time()
    sql = """
    INSERT INTO fleet_state (key_label, quarantined, quarantine_reason, rpd_count, error_count, day, updated_at)
    VALUES (?, 0, '', ?, ?, ?, ?)
    ON CONFLICT(key_label) DO UPDATE SET
        rpd_count = excluded.rpd_count,
        error_count = excluded.error_count,
        day = excluded.day,
        updated_at = excluded.updated_at;
    """
    async with _db_lock:
        await conn.execute(sql, (key_label, rpd_count, error_count, day, now_ts))
        await conn.commit()


async def load_spend_state() -> Optional[dict]:
    # Loads singleton spend controller state
    conn = await get_db()
    cursor = await conn.execute("SELECT * FROM spend_state WHERE id = 1;")
    row = await cursor.fetchone()
    if row:
        return dict(row)
    return None


async def save_spend_state(daily_cap_usd: float, run_cap_usd: float, current_day: str,
                           day_usd: float = 0.0, run_usd: float = 0.0,
                           run_last_ts: float = 0.0, run_request_count: int = 0) -> None:
    # Saves or updates the singleton spend budget state
    conn = await get_db()
    sql = """
    INSERT INTO spend_state (id, daily_cap_usd, run_cap_usd, day_usd, run_usd, run_last_ts, run_request_count, current_day)
    VALUES (1, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        daily_cap_usd = excluded.daily_cap_usd,
        run_cap_usd = excluded.run_cap_usd,
        day_usd = excluded.day_usd,
        run_usd = excluded.run_usd,
        run_last_ts = excluded.run_last_ts,
        run_request_count = excluded.run_request_count,
        current_day = excluded.current_day;
    """
    async with _db_lock:
        await conn.execute(sql, (
            daily_cap_usd, run_cap_usd, day_usd, run_usd, run_last_ts, run_request_count, current_day
        ))
        await conn.commit()


async def get_day_total_cost(day: str) -> float:
    # Preloads accumulated spend for a specific day using indexed SQL aggregation
    conn = await get_db()
    cursor = await conn.execute("SELECT COALESCE(SUM(cost_usd), 0.0) FROM requests WHERE day = ?;", (day,))
    row = await cursor.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


async def get_day_requests(day: str, limit: Optional[int] = None) -> List[dict]:
    # Returns all requests for a given day ordered chronologically with legacy compatibility aliases
    conn = await get_db()
    query = "SELECT * FROM requests WHERE day = ? ORDER BY ts ASC"
    params = [day]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    cursor = await conn.execute(query, tuple(params))
    rows = await cursor.fetchall()
    results = []

    for r in rows:
        item = dict(r)
        # Parse serialized JSON tool lists
        if item.get("tools_called"):
            try:
                item["tools_called"] = json.loads(item["tools_called"])
            except Exception:
                pass
        if item.get("tool_names_provided"):
            try:
                item["tool_names"] = json.loads(item["tool_names_provided"])
            except Exception:
                pass

        # Populate legacy aliases so downstream modules work seamlessly
        item["m"] = item["model_requested"]
        item["in"] = item["tokens_prompt_total"]
        item["c_in"] = item["tokens_prompt_cached"]
        item["out"] = item["tokens_completion"]
        item["r_out"] = item["tokens_reasoning"]
        item["cost"] = item["cost_usd"]
        item["dur"] = item["duration_total_s"]
        item["caller"] = item["caller_type"]
        item["dir"] = item["caller_dir"]
        item["os"] = item["caller_os"]
        item["status"] = item["status_code"]
        item["key"] = item["key_label"]
        item["err"] = item["error_message"]
        results.append(item)

    return results


async def get_day_summary(day: str) -> dict:
    # Performs single-pass aggregate query to compute daily usage totals
    conn = await get_db()
    sql = """
    SELECT
        COUNT(*) as total_reqs,
        COALESCE(SUM(cost_usd), 0.0) as total_cost,
        COALESCE(SUM(tokens_prompt_total), 0) as total_prompt,
        COALESCE(SUM(tokens_prompt_cached), 0) as total_cached,
        COALESCE(SUM(tokens_completion), 0) as total_completion,
        COALESCE(SUM(tokens_reasoning), 0) as total_reasoning,
        SUM(CASE WHEN status_code >= 400 OR error_message IS NOT NULL THEN 1 ELSE 0 END) as total_errors
    FROM requests WHERE day = ?;
    """
    cursor = await conn.execute(sql, (day,))
    row = await cursor.fetchone()
    if not row:
        return {
            "total_reqs": 0, "total_cost": 0.0, "total_prompt": 0,
            "total_cached": 0, "total_completion": 0, "total_reasoning": 0, "total_errors": 0
        }
    return dict(row)


async def get_available_days() -> List[dict]:
    # Returns distinct dates and aggregate spend for dashboard dropdown
    conn = await get_db()
    sql = """
    SELECT day, COALESCE(SUM(cost_usd), 0.0) as cost, COUNT(*) as count
    FROM requests
    GROUP BY day
    ORDER BY day DESC;
    """
    cursor = await conn.execute(sql)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_rolling_rpm(day: str, cutoff_iso: str) -> int:
    # Counts active requests since the cutoff timestamp within current day
    conn = await get_db()
    sql = "SELECT COUNT(*) FROM requests WHERE day = ? AND ts >= ?;"
    cursor = await conn.execute(sql, (day, cutoff_iso))
    row = await cursor.fetchone()
    return int(row[0]) if row else 0