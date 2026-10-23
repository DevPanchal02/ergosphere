import os
import re
import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

import config
import analytics
from controller import controller
from key_manager import key_manager

def get_available_log_days() -> List[Dict[str, Any]]:
    days = set()
    current_day = controller.get_today_str(time.time())
    days.add(current_day)

    all_files = analytics.get_all_log_files()

    # 1. Filename discovery (ensures diagnostics_2026-10-17 is added)
    for path in all_files:
        base = os.path.basename(path)
        if m := re.search(r"\d{4}-\d{2}-\d{2}", base):
            days.add(m.group(0))

    # 2. Row timestamp discovery (ensures midnight rollover dates like 2026-10-21 are added)
    for r in analytics.get_all_rows_cached():
        if d := analytics.row_day(r):
            days.add(d)

    results = []
    for d in sorted(days, reverse=True):
        day_rows = analytics.read_day_rows(d)
        if not day_rows and d != current_day:
            continue
        cost = sum(float(r.get("cost") or 0.0) for r in day_rows)
        results.append({"day": d, "cost": cost, "count": len(day_rows)})
    return results

def format_tokens(n: float) -> str:
    try:
        val = float(n or 0)
    except (ValueError, TypeError):
        return "0"
    if abs(val) >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if abs(val) >= 1_000:
        return f"{val / 1_000:.1f}k"
    return f"{int(val):,}" if val.is_integer() else f"{val:.1f}"

def render_cli_text(day_str: str) -> str:
    rows = analytics.read_day_rows(day_str)
    by_model, by_caller, by_agent, ranked_tools = analytics.summarize_rows(rows)
    now = time.time()
    controller._roll(now)
    pool = key_manager.get_pool_status(now)

    total_cost = sum(b["usd"] for b in by_model.values())
    total_reqs = len(rows)
    total_errs = sum(b["errors"] for b in by_model.values())

    out = [
        "==================================================================================================",
        f"HORIZON PROXY // {day_str} ({config.TZ_NOTE})",
        "==================================================================================================",
        f"SPEND: ${total_cost:.4f} / ${controller.daily_cap:.2f} cap | {total_reqs} reqs, {total_errs} errors",
        f"POOL: {pool['healthy_keys']} healthy keys | Available TPM: {format_tokens(pool['pool_tpm_available'])} / {format_tokens(pool['pool_tpm_max'])}",
        "",
        "--- GEMINI FLEET STATUS ---",
    ]

    for k in pool["keys"]:
        fb_str = "[3.1]" if k["fallback"] else "[3.5]"
        out.append(f"  {k['label']:<4} {fb_str} [{k['status_text']:<8}] RPM={k['rpm_used']}/15 TPM={format_tokens(k['tpm_used'])}/{format_tokens(k['tpm_limit'])} RPD={k['rpd_used']}/500 Errs={k['errs']}")

    out.append("\n--- CALLERS & AGENT ROLES ---")
    for c, b in sorted(by_caller.items(), key=lambda x: x[1]["usd"], reverse=True):
        out.append(f"  {c:<32} [{b.get('os','?')}] reqs={b['reqs']:<5} ${b['usd']:<7.4f} in={format_tokens(b['in'])}")

    out.append("\n--- MODELS BREAKDOWN ---")
    for m, b in sorted(by_model.items(), key=lambda x: x[1]["reqs"], reverse=True):
        out.append(f"  {m:<28} reqs={b['reqs']:<5} ${b['usd']:<7.4f} in={format_tokens(b['in'])} out={format_tokens(b['out'])} err={b['errors']}")

    out.append("\n--- RECENT REQUESTS ---")
    for r in reversed(rows[-8:]):
        out.append(f"  {r.get('ts','?')[11:19]}Z status={r.get('status',200)} {r.get('m','?'):<20} {r.get('caller','?'):<22} ${float(r.get('cost') or 0):.4f}")

    return "\n".join(out) + "\n"

def render_html_dashboard(selected_day: Optional[str] = None) -> str:
    now = time.time()
    today_str = controller.get_today_str(now)
    active_day = selected_day or today_str
    day_entries = get_available_log_days()

    rows = analytics.read_day_rows(active_day)
    by_model, by_caller, by_agent, ranked_tools = analytics.summarize_rows(rows)
    pool_status = key_manager.get_pool_status(now)

    total_reqs = len(rows)
    total_spend = sum(float(r.get("cost") or 0.0) for r in rows)
    active_rpm = analytics.get_rolling_rpm(rows, now) if active_day == today_str else 0

    max_rpd_pool = len(key_manager.keys) * key_manager.rpd_limit
    gemini_day_calls = sum(1 for r in rows if r.get("key", "").startswith("G"))
    rpd_pct = (gemini_day_calls / max(1, max_rpd_pool)) * 100

    dt = datetime.fromtimestamp(now, config.TZ)
    seconds_in_day = dt.hour * 3600 + dt.minute * 60 + dt.second
    day_pct = (seconds_in_day / 86400) * 100
    pacing_status = "SURPLUS" if rpd_pct <= day_pct else "PACING"
    pacing_color = "#10b981" if pacing_status == "SURPLUS" else "#f59e0b"

    active_button_label = "Today" if active_day == today_str else active_day

    dropdown_items_html = []
    for item in day_entries:
        d = item["day"]
        cost_str = f"${item['cost']:.4f}"
        date_label = "Today" if d == today_str else d
        is_active = (d == active_day)
        dropdown_items_html.append(f'''
          <div class="dropdown-item {'active' if is_active else ''}" onclick="changeDay('{d}')">
            <span class="d-date">{date_label}</span>
            <span class="d-cost">{cost_str}</span>
          </div>
        ''')

    initial_payload = {
        "active_day": active_day,
        "today_str": today_str,
        "rows": rows,
        "pool": pool_status,
        "spend": {
            "day_usd": round(total_spend, 4),
            "daily_cap": controller.daily_cap,
            "run_usd": controller.ledger["run_usd"],
            "run_cap": controller.run_cap
        }
    }

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Horizon Proxy // Cockpit</title>
  <style>
    :root {{
      --bg: #090d16;
      --card: #111726;
      --card-border: #1e293b;
      --card-hover: #162035;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --green: #10b981;
      --red: #ef4444;
      --amber: #f59e0b;
      --cyan: #38bdf8;
      --accent: #2563eb;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    body {{ background: var(--bg); color: var(--text); padding: 8px; font-size: 13px; line-height: 1.35; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }}

    .top-bar {{ display: flex; align-items: center; justify-content: space-between; background: var(--card); border: 1px solid var(--card-border); padding: 6px 12px; margin-bottom: 8px; border-radius: 4px; }}
    .brand {{ display: flex; align-items: center; gap: 8px; font-weight: 700; color: var(--cyan); font-size: 14px; letter-spacing: 0.5px; }}
    .pill {{ padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
    .pill-green {{ background: rgba(16, 185, 129, 0.15); color: var(--green); }}
    .pill-amber {{ background: rgba(245, 158, 11, 0.15); color: var(--amber); }}
    .pill-red {{ background: rgba(239, 68, 68, 0.15); color: var(--red); }}

    .dropdown {{ position: relative; display: inline-block; }}
    .dropdown-trigger {{
      background: #1e293b;
      border: 1px solid #334155;
      color: #fff;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      font-weight: 600;
    }}
    .dropdown-trigger:hover {{ background: #334155; }}
    .dropdown-menu {{
      display: none;
      position: absolute;
      top: 100%;
      left: 0;
      margin-top: 4px;
      background: #111726;
      border: 1px solid #1e293b;
      border-radius: 4px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.6);
      z-index: 1000;
      min-width: 200px;
      max-height: 300px;
      overflow-y: auto;
    }}
    .dropdown-menu.show {{ display: block; }}
    .dropdown-item {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 10px;
      font-size: 11px;
      cursor: pointer;
      border-bottom: 1px solid rgba(255,255,255,0.03);
    }}
    .dropdown-item:last-child {{ border-bottom: none; }}
    .dropdown-item:hover {{ background: rgba(37,99,235,0.25); }}
    .dropdown-item.active {{ background: rgba(37,99,235,0.45); font-weight: 700; }}
    .d-date {{ color: var(--text); }}
    .d-cost {{ color: var(--cyan); font-variant-numeric: tabular-nums; }}

    .cockpit-layout {{
      display: grid;
      grid-template-columns: 380px 1fr;
      gap: 8px;
      flex: 1;
      min-height: 0;
    }}
    @media (max-width: 1150px) {{
      .cockpit-layout {{ grid-template-columns: 1fr; }}
      body {{ overflow-y: auto; height: auto; }}
    }}

    .bento-card {{ background: var(--card); border: 1px solid var(--card-border); border-radius: 4px; padding: 7px; display: flex; flex-direction: column; }}
    .card-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 5px; padding-bottom: 3px; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
    .btn-action {{ background: #1e293b; border: 1px solid #334155; color: var(--text); padding: 2px 6px; font-size: 10px; border-radius: 3px; cursor: pointer; }}
    .btn-action:hover {{ background: #334155; }}

    .balancer-card {{
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid #1e293b;
      border-radius: 4px;
      padding: 5px 8px;
      margin-bottom: 6px;
      font-size: 11px;
    }}
    .bal-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 2px; }}
    .bal-title {{ color: var(--text-muted); font-size: 9.5px; font-weight: 700; text-transform: uppercase; }}
    .bal-val {{ font-weight: 700; color: #fff; font-size: 11px; }}

    .fleet-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 5px;
      flex: 1;
      overflow-y: auto;
    }}
    .key-card {{
      background: rgba(0,0,0,0.25);
      border: 1px solid var(--card-border);
      border-radius: 4px;
      padding: 5px 7px;
      font-size: 10.5px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .key-card.dead {{ border-color: var(--red); background: rgba(239, 68, 68, 0.08); }}
    .key-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 2px; }}
    .key-name {{ font-weight: 800; font-size: 12px; color: #fff; }}
    .key-toggle-btn {{
      padding: 1px 4px;
      border-radius: 2px;
      font-size: 9px;
      font-weight: 700;
      cursor: pointer;
      border: 1px solid #334155;
      background: #1e293b;
      color: var(--text-muted);
    }}
    .key-toggle-btn.active {{
      background: rgba(245, 158, 11, 0.25);
      color: var(--amber);
      border-color: rgba(245, 158, 11, 0.5);
    }}
    .key-stats-row {{ display: flex; justify-content: space-between; color: var(--text-muted); font-size: 9.5px; margin-top: 1px; }}
    .tpm-track {{ background: #162035; height: 3px; border-radius: 2px; overflow: hidden; margin-top: 3px; }}
    .tpm-bar {{ height: 100%; }}

    .main-workspace {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      overflow-y: auto;
      min-height: 0;
    }}
    .grid-2col {{ display: grid; grid-template-columns: 1.25fr 1fr; gap: 8px; }}

    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th {{ text-align: left; color: var(--text-muted); font-size: 10px; padding: 3px 5px; font-weight: 600; text-transform: uppercase; }}
    td {{ padding: 3px 5px; border-top: 1px solid rgba(255,255,255,0.03); white-space: nowrap; }}
    tr:hover td {{ background: var(--card-hover); }}
    .mono-num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .status-ok {{ color: var(--green); font-weight: 700; }}
    .status-err {{ color: var(--red); font-weight: 700; }}

    .depth-sync-input {{
      width: 48px;
      background: #141d2f;
      border: 1px solid #334155;
      color: var(--cyan);
      font-size: 11px;
      font-weight: 700;
      padding: 1px 4px;
      border-radius: 3px;
      text-align: center;
      outline: none;
    }}
    .scroll-y {{ max-height: 180px; overflow-y: auto; }}
    .os-badge {{ font-size: 9px; padding: 0 4px; border-radius: 2px; background: #1e293b; color: var(--cyan); text-transform: uppercase; font-weight: 700; }}
  </style>
</head>
<body>

  <!-- Exact Modular Top Bar -->
  <div class="top-bar">
    <div class="brand">
      <span>⚡ HORIZON PROXY</span>
      
      <div class="dropdown">
        <button class="dropdown-trigger" onclick="toggleDropdown(event)">
          <span>{active_button_label}</span>
          <span style="font-size:8px; color:var(--text-muted);">▼</span>
        </button>
        <div class="dropdown-menu" id="day-dropdown">
          {''.join(dropdown_items_html)}
        </div>
      </div>

      <span class="pill" id="sse-status">INITIALIZING</span>
    </div>

    <div style="display:flex; align-items:center; gap:16px; font-size:11px;">
      <div>POOL TPM: <strong id="val-pool-tpm" style="color:var(--cyan);">{format_tokens(pool_status["pool_tpm_available"])}</strong> / {format_tokens(pool_status["pool_tpm_max"])}</div>
      <div>RPM: <strong id="val-active-rpm" style="color:var(--cyan);">{active_rpm}</strong> / {pool_status["pool_rpm_max"]}</div>
      <div>KEYS: <strong id="val-healthy-keys" style="color:var(--green);">{pool_status["healthy_keys"]}</strong></div>
      <div>RPD: <strong id="val-rpd-calls">{gemini_day_calls:,}</strong> / {max_rpd_pool:,} (<span id="val-pacing" style="color:{pacing_color}; font-weight:700;">{pacing_status}</span>)</div>
      <div style="background:rgba(56,189,248,0.1); border:1px solid rgba(56,189,248,0.3); padding:3px 8px; border-radius:3px;">
        PAID SPEND: <strong id="val-paid-spend" style="color:var(--cyan); font-size:12px;">${total_spend:.4f}</strong> <span style="color:var(--text-muted);">/ ${controller.daily_cap:.2f}</span>
      </div>
    </div>
  </div>

  <div class="cockpit-layout">
    
    <!-- LEFT SIDEBAR: EXPANDED 2-COLUMN FLEET (FITS ON ONE SCREEN) -->
    <aside class="bento-card" style="height: 100%;">
      <div class="card-head">
        <span>Gemini Fleet ({len(key_manager.keys)} Keys)</span>
        <button class="btn-action" onclick="copyCardData('fleet-box', this)">Copy</button>
      </div>

      <!-- Transparent Scheduler Logic Box -->
      <div class="balancer-card">
        <div class="bal-row">
          <span class="bal-title">EQUATION</span>
          <span class="bal-val" style="color:var(--cyan); font-size:9.5px;">max(RPM, TPM) → Least RPD</span>
        </div>
        <div class="bal-row">
          <span class="bal-title">NEXT CANDIDATE</span>
          <span class="bal-val" id="bal-next-key" style="color:var(--green);">{pool_status['balancer']['next_key']} (Wait: {pool_status['balancer']['next_wait']})</span>
        </div>
      </div>

      <div class="fleet-grid" id="fleet-box">
        <!-- Dynamically rendered 2-column cards -->
      </div>
    </aside>

    <!-- RIGHT MAIN: MODELS, CALLERS, REQUESTS & ERRORS SIDE-BY-SIDE -->
    <main class="main-workspace">
      
      <!-- Row 1: Models & Ranked Tools -->
      <div class="grid-2col">
        <div class="bento-card">
          <div class="card-head">
            <span>Models Breakdown</span>
            <button class="btn-action" onclick="copyCardData('model-table', this)">Copy</button>
          </div>
          <table id="model-table">
            <thead>
              <tr>
                <th>Model</th>
                <th class="mono-num">Reqs</th>
                <th class="mono-num">Spend</th>
                <th class="mono-num">Prompt (Cached)</th>
                <th class="mono-num">Comp</th>
                <th class="mono-num">Reason</th>
                <th class="mono-num">Errors</th>
              </tr>
            </thead>
            <tbody id="model-tbody"></tbody>
          </table>
        </div>

        <div class="bento-card">
          <div class="card-head">
            <span>Ranked Tool Invocations</span>
            <button class="btn-action" onclick="copyCardData('tool-table', this)">Copy</button>
          </div>
          <table id="tool-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Tool Name</th>
                <th class="mono-num">Calls</th>
                <th class="mono-num">Share</th>
              </tr>
            </thead>
            <tbody id="tool-tbody"></tbody>
          </table>
        </div>
      </div>

      <!-- Row 2: Callers & Specific Agent Roles -->
      <div class="bento-card">
        <div class="card-head">
          <span>Callers & Agent Roles (Builder / Scout / Planner / Path)</span>
          <button class="btn-action" onclick="copyCardData('caller-table', this)">Copy</button>
        </div>
        <div class="scroll-y" style="max-height: 120px;">
          <table id="caller-table">
            <thead>
              <tr>
                <th>Agent ID / Path</th>
                <th>Platform</th>
                <th class="mono-num">Requests</th>
                <th class="mono-num">Spend</th>
                <th class="mono-num">Tokens In</th>
                <th class="mono-num">Tokens Out</th>
                <th class="mono-num">Errors</th>
              </tr>
            </thead>
            <tbody id="caller-tbody"></tbody>
          </table>
        </div>
      </div>

      <!-- Row 3: Requests & Errors Side-by-Side with Synchronized Custom Depth -->
      <div class="grid-2col" style="flex: 1; min-height: 0;">
        
        <!-- Recent Requests -->
        <div class="bento-card" style="flex: 1;">
          <div class="card-head">
            <div style="display:flex; align-items:center; gap:8px;">
              <span>Recent Requests</span>
              <span style="font-size:10px; color:var(--text-muted);">DEPTH:</span>
              <input type="number" class="depth-sync-input" id="global-depth-input" value="15" min="1" max="500" onchange="syncDepth(this.value)">
            </div>
            <button class="btn-action" onclick="copyCardData('req-table', this)">Copy</button>
          </div>
          <div class="scroll-y" style="max-height: 220px;">
            <table id="req-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Status</th>
                  <th>Model</th>
                  <th>Caller / Agent</th>
                  <th>Key</th>
                  <th class="mono-num">Cost</th>
                  <th class="mono-num">Duration</th>
                </tr>
              </thead>
              <tbody id="req-tbody"></tbody>
            </table>
          </div>
        </div>

        <!-- Recent Errors -->
        <div class="bento-card" style="flex: 1;">
          <div class="card-head">
            <span style="color:var(--red);">Recent Errors</span>
            <button class="btn-action" onclick="copyCardData('err-table', this)">Copy</button>
          </div>
          <div class="scroll-y" style="max-height: 220px;">
            <table id="err-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Status</th>
                  <th>Model</th>
                  <th>Caller / Agent</th>
                  <th>Key</th>
                  <th>Failure Cause</th>
                  <th class="mono-num">Duration</th>
                </tr>
              </thead>
              <tbody id="err-tbody"></tbody>
            </table>
          </div>
        </div>

      </div>

    </main>
  </div>

  <script>
    const INITIAL_DATA = {json.dumps(initial_payload)};
    let currentDepth = 15;

    function toggleDropdown(e) {{
      e.stopPropagation();
      document.getElementById("day-dropdown").classList.toggle("show");
    }}
    window.addEventListener("click", () => {{
      const menu = document.getElementById("day-dropdown");
      if (menu) menu.classList.remove("show");
    }});

    function changeDay(day) {{
      window.location.href = window.location.pathname + "?day=" + day;
    }}

    function formatTok(n) {{
      const num = parseFloat(n || 0);
      if (Math.abs(num) >= 1000000) return (num / 1000000).toFixed(2) + 'M';
      if (Math.abs(num) >= 1000) return (num / 1000).toFixed(1) + 'k';
      return num.toLocaleString();
    }}

    function syncDepth(val) {{
      const parsed = parseInt(val);
      if (parsed > 0) {{
        currentDepth = parsed;
        document.getElementById("global-depth-input").value = parsed;
        renderRequestsAndErrors();
      }}
    }}

    async function toggleKeyFallback(keyLabel, currentFallback) {{
      const nextState = !currentFallback;
      await fetch('/api/key/fallback', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ key: keyLabel, fallback: nextState }})
      }});
    }}

    async function unquarantineKey(keyLabel) {{
      await fetch('/api/key/unquarantine', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ key: keyLabel }})
      }});
    }}

    function renderFleet(pool) {{
      const fleetBox = document.getElementById("fleet-box");
      const balNextKey = document.getElementById("bal-next-key");
      if (pool.balancer) {{
        balNextKey.textContent = `${{pool.balancer.next_key}} (Wait: ${{pool.balancer.next_wait}})`;
      }}

      fleetBox.innerHTML = (pool.keys || []).map(k => `
        <div class="key-card ${{k.quarantined ? 'dead' : ''}}">
          <div class="key-head">
            <div style="display:flex; align-items:center; gap:4px;">
              <span class="key-name">${{k.label}}</span>
              <span class="pill ${{k.status_type === 'err' ? 'pill-red' : (k.status_type === 'warn' ? 'pill-amber' : 'pill-green')}}">${{k.status_text}}</span>
            </div>
            <div style="display:flex; align-items:center; gap:3px;">
              ${{k.quarantined ? `<button class="btn-action" style="color:var(--red);" onclick="unquarantineKey('${{k.label}}')">Revive</button>` : ''}}
              <button class="key-toggle-btn ${{k.fallback ? 'active' : ''}}" onclick="toggleKeyFallback('${{k.label}}', ${{k.fallback}})">
                ${{k.fallback ? '3.1' : '3.5'}}
              </button>
            </div>
          </div>
          <div class="key-stats-row">
            <span>RPM: <b>${{k.rpm_used}}/15</b></span>
            <span>RPD: <b>${{k.rpd_used}}</b></span>
            <span style="color:${{k.errs ? 'var(--red)' : 'var(--text-muted)'}};">Err: <b>${{k.errs}}</b></span>
          </div>
          <div class="key-stats-row" style="font-size:9px;">
            <span>TPM: ${{formatTok(k.tpm_used)}}</span>
            <span>${{k.tpm_pct}}%</span>
          </div>
          <div class="tpm-track">
            <div class="tpm-bar" style="width:${{k.tpm_pct}}%; background:${{k.tpm_pct > 85 ? 'var(--red)' : (k.tpm_pct > 60 ? 'var(--amber)' : 'var(--green)')}};"></div>
          </div>
        </div>
      `).join('');
    }}

    function renderSummaryTables() {{
      const rows = INITIAL_DATA.rows || [];
      const byModel = {{}};
      const byCaller = {{}};
      const toolCounts = {{}};

      rows.forEach(r => {{
        const m = r.m || 'unknown';
        const c = r.caller || 'agent';
        const c_os = r.os || 'unknown';
        const cost = parseFloat(r.cost || 0);
        const p_in = parseInt(r.in || 0);
        const p_cin = parseInt(r.c_in || 0);
        const p_out = parseInt(r.out || 0);
        const p_rout = parseInt(r.r_out || 0);
        const isErr = r.err || parseInt(r.status || 200) >= 400;

        if (!byModel[m]) byModel[m] = {{ reqs: 0, usd: 0, in: 0, cin: 0, out: 0, rout: 0, errs: 0 }};
        byModel[m].reqs++;
        byModel[m].usd += cost;
        byModel[m].in += p_in;
        byModel[m].cin += p_cin;
        byModel[m].out += p_out;
        byModel[m].rout += p_rout;
        if (isErr) byModel[m].errs++;

        if (!byCaller[c]) byCaller[c] = {{ reqs: 0, usd: 0, in: 0, out: 0, errs: 0, os: c_os }};
        byCaller[c].reqs++;
        byCaller[c].usd += cost;
        byCaller[c].in += p_in;
        byCaller[c].out += p_out;
        if (isErr) byCaller[c].errs++;

        (r.tools_called || []).forEach(t => {{
          if (t && t !== '?') toolCounts[t] = (toolCounts[t] || 0) + 1;
        }});
      }});

      // Models Table
      const modelTbody = document.getElementById("model-tbody");
      const modelKeys = Object.keys(byModel).sort((a,b) => byModel[b].reqs - byModel[a].reqs);
      modelTbody.innerHTML = modelKeys.length ? modelKeys.map(m => `
        <tr>
          <td style="font-weight:700; color:${{m.includes('openrouter') ? 'var(--cyan)' : 'var(--text)'}};">${{m.split('/').pop()}}</td>
          <td class="mono-num">${{byModel[m].reqs.toLocaleString()}}</td>
          <td class="mono-num">$${{byModel[m].usd.toFixed(4)}}</td>
          <td class="mono-num">${{formatTok(byModel[m].in)}} <small style="color:var(--text-muted);">(${{formatTok(byModel[m].cin)}})</small></td>
          <td class="mono-num">${{formatTok(byModel[m].out)}}</td>
          <td class="mono-num" style="color:var(--amber);">${{formatTok(byModel[m].rout)}}</td>
          <td class="mono-num" style="color:${{byModel[m].errs ? 'var(--red)' : 'inherit'}};">${{byModel[m].errs}}</td>
        </tr>
      `).join('') : '<tr><td colspan="7" style="text-align:center; color:var(--text-muted);">No activity</td></tr>';

      // Callers Table
      const callerTbody = document.getElementById("caller-tbody");
      const callerKeys = Object.keys(byCaller).sort((a,b) => byCaller[b].reqs - byCaller[a].reqs);
      callerTbody.innerHTML = callerKeys.length ? callerKeys.map(c => `
        <tr>
          <td style="font-weight:700; color:var(--text);">${{c}}</td>
          <td><span class="os-badge">${{byCaller[c].os}}</span></td>
          <td class="mono-num">${{byCaller[c].reqs.toLocaleString()}}</td>
          <td class="mono-num">$${{byCaller[c].usd.toFixed(4)}}</td>
          <td class="mono-num">${{formatTok(byCaller[c].in)}}</td>
          <td class="mono-num">${{formatTok(byCaller[c].out)}}</td>
          <td class="mono-num" style="color:${{byCaller[c].errs ? 'var(--red)' : 'inherit'}};">${{byCaller[c].errs}}</td>
        </tr>
      `).join('') : '<tr><td colspan="7" style="text-align:center; color:var(--text-muted);">No active callers</td></tr>';

      // Tools Table
      const toolTbody = document.getElementById("tool-tbody");
      const rankedTools = Object.entries(toolCounts).sort((a,b) => b[1] - a[1]);
      const totalToolCalls = rankedTools.reduce((acc, x) => acc + x[1], 0);
      toolTbody.innerHTML = rankedTools.length ? rankedTools.slice(0, 8).map(([t, cnt], idx) => `
        <tr>
          <td style="color:var(--text-muted); font-size:10px;">#${{idx + 1}}</td>
          <td style="font-weight:700;">${{t}}</td>
          <td class="mono-num">${{cnt.toLocaleString()}}</td>
          <td class="mono-num" style="color:var(--cyan);">${{(cnt / Math.max(1, totalToolCalls) * 100).toFixed(1)}}%</td>
        </tr>
      `).join('') : '<tr><td colspan="4" style="text-align:center; color:var(--text-muted);">No tool calls</td></tr>';
    }}

    function renderRequestsAndErrors() {{
      const rows = INITIAL_DATA.rows || [];
      const reqTbody = document.getElementById("req-tbody");
      const errTbody = document.getElementById("err-tbody");

      const recentReqs = rows.slice(-currentDepth).reverse();
      reqTbody.innerHTML = recentReqs.length ? recentReqs.map(r => `
        <tr>
          <td style="color:var(--text-muted);">${{(r.ts || '').substring(11, 19)}}Z</td>
          <td class="${{r.status < 400 ? 'status-ok' : 'status-err'}}">${{r.status || 200}}</td>
          <td>${{(r.m || '').split('/').pop()}}</td>
          <td><span class="os-badge">${{r.os || '?'}}</span> ${{r.caller || 'agent'}}</td>
          <td><strong style="color:var(--green);">${{r.key || '-'}}</strong></td>
          <td class="mono-num" style="color:var(--cyan);">$${{parseFloat(r.cost || 0).toFixed(4)}}</td>
          <td class="mono-num">${{r.dur || 0}}s</td>
        </tr>
      `).join('') : '<tr><td colspan="7" style="text-align:center; color:var(--text-muted);">No requests recorded</td></tr>';

      const errRows = rows.filter(r => r.err || parseInt(r.status || 200) >= 400);
      const recentErrs = errRows.slice(-currentDepth).reverse();
      errTbody.innerHTML = recentErrs.length ? recentErrs.map(r => `
        <tr>
          <td style="color:var(--text-muted);">${{(r.ts || '').substring(11, 19)}}Z</td>
          <td class="status-err">${{r.status || 500}}</td>
          <td>${{(r.m || '').split('/').pop()}}</td>
          <td><span class="os-badge">${{r.os || '?'}}</span> ${{r.caller || 'agent'}}</td>
          <td><strong style="color:var(--red);">${{r.key || '-'}}</strong></td>
          <td style="color:var(--red); max-width:200px; overflow:hidden; text-overflow:ellipsis;">${{r.err || 'Error'}}</td>
          <td class="mono-num">${{r.dur || 0}}s</td>
        </tr>
      `).join('') : '<tr><td colspan="7" style="text-align:center; color:var(--text-muted);">No errors recorded</td></tr>';
    }}

    function copyCardData(elementId, btn) {{
      const el = document.getElementById(elementId);
      if (!el) return;
      let text = `[EXPORT: ${{elementId.toUpperCase()}}]\n`;
      const rows = el.querySelectorAll("tr");
      if (rows.length) {{
        rows.forEach(r => {{
          const cells = Array.from(r.querySelectorAll("th, td")).map(c => c.innerText.replace(/\\n/g, ' ').trim());
          if (cells.length) text += cells.join(" | ") + "\\n";
        }});
      }} else {{
        el.querySelectorAll(".key-card").forEach(k => {{
          text += k.innerText.replace(/\\n/g, ' | ') + "\\n";
        }});
      }}
      navigator.clipboard.writeText(text).then(() => {{
        if (btn) {{
          const orig = btn.textContent;
          btn.textContent = "COPIED!";
          setTimeout(() => {{ btn.textContent = orig; }}, 1400);
        }}
      }});
    }}

    function setupLiveSSE() {{
      const statusEl = document.getElementById("sse-status");
      const isToday = (INITIAL_DATA.active_day === INITIAL_DATA.today_str);

      if (!isToday) {{
        statusEl.textContent = "ARCHIVED";
        statusEl.className = "pill pill-muted";
        return;
      }}

      const es = new EventSource("/api/live");
      es.onopen = () => {{
        statusEl.textContent = "● LIVE";
        statusEl.className = "pill pill-green";
      }};

      es.onmessage = (event) => {{
        try {{
          const data = JSON.parse(event.data);
          if (data.pool) {{
            renderFleet(data.pool);
            document.getElementById("val-pool-tpm").textContent = formatTok(data.pool.pool_tpm_available);
            document.getElementById("val-healthy-keys").textContent = data.pool.healthy_keys;
          }}
          if (data.active_rpm !== undefined) {{
            document.getElementById("val-active-rpm").textContent = data.active_rpm;
          }}
          if (data.spend) {{
            document.getElementById("val-paid-spend").textContent = '$' + data.spend.day_usd.toFixed(4);
          }}

          if (data.type === "telemetry" && data.row) {{
            INITIAL_DATA.rows.push(data.row);
            renderSummaryTables();
            renderRequestsAndErrors();
          }}
        }} catch(e) {{}}
      }};

      es.onerror = () => {{
        statusEl.textContent = "○ RECONNECTING";
        statusEl.className = "pill pill-amber";
      }};
    }}

    renderFleet(INITIAL_DATA.pool);
    renderSummaryTables();
    renderRequestsAndErrors();
    setupLiveSSE();
  </script>
</body>
</html>
"""