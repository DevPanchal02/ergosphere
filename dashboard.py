import os
import glob
import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

import config
import analytics
from controller import controller
from key_manager import key_manager

# Scans disk for available diagnostic dates to populate the dropdown
def get_available_log_days() -> List[str]:
    files = glob.glob(os.path.join(config.LOGS_DIR, "diagnostics_*.jsonl"))
    days = set()
    for f in files:
        base = os.path.basename(f)
        parts = base.replace(".jsonl", "").split("_")
        if len(parts) == 2 and len(parts[1]) == 10:
            days.add(parts[1])
    current_day = controller.get_today_str(time.time())
    days.add(current_day)
    return sorted(list(days), reverse=True)

# Formats compact token counts (e.g. 41.5M, 197.5k)
def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)

# Generates ANSI plaintext summary for curl or terminal inspection
def render_cli_text(day_str: str) -> str:
    rows = analytics.read_day_rows(day_str)
    by_model, by_caller, ranked_tools = analytics.summarize_rows(rows)
    now = time.time()
    controller._roll(now)
    
    out = [
        f"=== HORIZON PROXY RECEIPT: {day_str} ({config.TZ_NOTE}) ===",
        f"SPEND: ${controller.ledger['day_usd']:.4f} of ${controller.daily_cap:.2f} daily cap | Run: ${controller.ledger['run_usd']:.4f} of ${controller.run_cap:.2f}",
        f"TOTAL REQUESTS: {len(rows):,} | ERRORS: {sum(1 for r in rows if r.get('err')):,}",
        "",
        "--- MODELS ---",
    ]
    for m, b in sorted(by_model.items()):
        out.append(f"{m:<30} reqs={b['reqs']:<5} ${b['usd']:<8.4f} in={format_tokens(b['in']):<8} out={format_tokens(b['out']):<8} err={b['errors']}")
    
    out.append("\n--- CALLERS / ROLES ---")
    for c, b in sorted(by_caller.items(), key=lambda x: x[1]["usd"], reverse=True):
        out.append(f"{c:<40} [{b.get('os','?')}] reqs={b['reqs']:<5} ${b['usd']:<8.4f} in={format_tokens(b['in'])}")

    out.append("\n--- TOP TOOLS ---")
    for t, count in ranked_tools[:8]:
        out.append(f"{t:<25} {count:,} calls")

    out.append("\n--- LAST 8 REQUESTS ---")
    for r in rows[-8:]:
        status = r.get("status", 200)
        out.append(f"{r.get('ts','?')[11:19]}Z {status} {r.get('m','?'):<24} {r.get('caller','?'):<25} ${float(r.get('cost') or 0):.4f}")

    return "\n".join(out) + "\n"

# Renders the full self-contained industrial dark-mode HTML dashboard
def render_html_dashboard(selected_day: Optional[str] = None) -> str:
    now = time.time()
    today_str = controller.get_today_str(now)
    active_day = selected_day or today_str
    available_days = get_available_log_days()

    rows = analytics.read_day_rows(active_day)
    by_model, by_caller, ranked_tools = analytics.summarize_rows(rows)
    pool_status = key_manager.get_pool_status(now)

    total_reqs = len(rows)
    total_errs = sum(1 for r in rows if r.get("err"))
    total_prompt = sum(int(r.get("in") or 0) for r in rows)
    total_cached = sum(int(r.get("c_in") or 0) for r in rows)
    total_out = sum(int(r.get("out") or 0) for r in rows)
    total_spend = sum(float(r.get("cost") or 0.0) for r in rows)

    # RPD burn-down tracking across the 8,000 call aggregate pool
    total_rpd_used = sum(k["rpd_used"] for k in pool_status["keys"])
    max_rpd_pool = len(pool_status["keys"]) * config.GEMINI_RPD_LIMIT
    rpd_pct = (total_rpd_used / max(1, max_rpd_pool)) * 100
    
    # Calculate day progression percentage based on local time
    dt = datetime.fromtimestamp(now, config.TZ)
    seconds_in_day = dt.hour * 3600 + dt.minute * 60 + dt.second
    day_pct = (seconds_in_day / 86400) * 100
    pacing_status = "SURPLUS" if rpd_pct <= day_pct else "PACING"
    pacing_color = "#10b981" if pacing_status == "SURPLUS" else "#f59e0b"

    initial_data = {
        "active_day": active_day,
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
  <title>Horizon Proxy // Industrial Control</title>
  <style>
    :root {{
      --bg: #090d16;
      --card: #111726;
      --card-border: #1e293b;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --green: #10b981;
      --red: #ef4444;
      --amber: #f59e0b;
      --cyan: #38bdf8;
      --accent: #2563eb;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    body {{ background: var(--bg); color: var(--text); padding: 8px; font-size: 13px; line-height: 1.35; }}

    /* Layout structure */
    .top-bar {{ display: flex; align-items: center; justify-content: space-between; background: var(--card); border: 1px solid var(--card-border); padding: 6px 12px; margin-bottom: 8px; border-radius: 4px; }}
    .brand {{ display: flex; align-items: center; gap: 8px; font-weight: 700; color: var(--cyan); font-size: 14px; letter-spacing: 0.5px; }}
    .pill {{ padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
    .pill-green {{ background: rgba(16, 185, 129, 0.15); color: var(--green); }}
    .pill-amber {{ background: rgba(245, 158, 11, 0.15); color: var(--amber); }}
    .pill-red {{ background: rgba(239, 68, 68, 0.15); color: var(--red); }}
    .pill-cyan {{ background: rgba(56, 189, 248, 0.15); color: var(--cyan); }}

    .grid {{ display: grid; grid-template-columns: 320px 1fr 340px; gap: 8px; margin-bottom: 8px; }}
    .bento-card {{ background: var(--card); border: 1px solid var(--card-border); border-radius: 4px; padding: 8px; display: flex; flex-direction: column; overflow: hidden; }}
    .card-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; padding-bottom: 4px; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
    .card-actions {{ display: flex; align-items: center; gap: 6px; }}

    /* Action buttons */
    .btn-action {{ background: #1e293b; border: 1px solid #334155; color: var(--text); padding: 2px 6px; font-size: 10px; border-radius: 3px; cursor: pointer; }}
    .btn-action:hover {{ background: #334155; }}
    .depth-pills span {{ cursor: pointer; padding: 1px 4px; border-radius: 2px; font-size: 10px; color: var(--text-muted); }}
    .depth-pills span.active {{ background: var(--accent); color: #fff; font-weight: 700; }}

    /* Tables */
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th {{ text-align: left; color: var(--text-muted); font-size: 10px; padding: 4px; font-weight: 600; text-transform: uppercase; }}
    td {{ padding: 4px; border-top: 1px solid rgba(255,255,255,0.03); white-space: nowrap; }}
    tr:hover td {{ background: rgba(255,255,255,0.02); }}
    .status-ok {{ color: var(--green); font-weight: 700; }}
    .status-err {{ color: var(--red); font-weight: 700; }}
    .mono-num {{ text-align: right; font-variant-numeric: tabular-nums; }}

    /* Key matrix */
    .key-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin-top: 4px; }}
    .key-box {{ background: rgba(0,0,0,0.25); border: 1px solid var(--card-border); padding: 4px; border-radius: 3px; font-size: 10px; }}
    .key-box.quarantined {{ border-color: var(--red); opacity: 0.6; }}
    .key-title {{ display: flex; justify-content: space-between; font-weight: 700; }}
    .key-bar {{ height: 3px; background: #1e293b; border-radius: 2px; margin-top: 3px; overflow: hidden; }}
    .key-fill {{ height: 100%; background: var(--green); }}

    /* Bottom full-width tables */
    .full-width {{ margin-bottom: 8px; }}
    .scroll-y {{ max-height: 260px; overflow-y: auto; }}
    .os-badge {{ font-size: 9px; padding: 0 3px; border-radius: 2px; background: #1e293b; color: var(--text-muted); }}
  </style>
</head>
<body>

  <!-- Top control & header bar -->
  <div class="top-bar">
    <div class="brand">
      <span>⚡ HORIZON PROXY</span>
      <select id="day-select" style="background:#1e293b; border:1px solid #334155; color:#fff; padding:2px 6px; border-radius:3px; font-size:11px;" onchange="changeDay(this.value)">
        {''.join(f'<option value="{d}" {"selected" if d == active_day else ""}>{d}</option>' for d in available_days)}
      </select>
      <span class="pill pill-green" id="sse-status">● LIVE</span>
    </div>

    <!-- Fleet gauge metrics -->
    <div style="display:flex; align-items:center; gap:16px; font-size:11px;">
      <div>POOL TPM: <strong style="color:var(--cyan);">{format_tokens(pool_status["pool_tpm_available"])}</strong> / {format_tokens(pool_status["pool_tpm_max"])}</div>
      <div>POOL RPM: <strong style="color:var(--cyan);">{pool_status["pool_rpm_max"]}</strong></div>
      <div>KEYS: <strong style="color:var(--green);">{pool_status["healthy_keys"]}</strong></div>
      <div>RPD: <strong>{total_rpd_used:,}</strong> / {max_rpd_pool:,} (<span style="color:{pacing_color}; font-weight:700;">{pacing_status}</span>)</div>
      <div style="background:rgba(56,189,248,0.1); border:1px solid rgba(56,189,248,0.3); padding:3px 8px; border-radius:3px;">
        PAID SPEND: <strong style="color:var(--cyan); font-size:12px;">${total_spend:.4f}</strong> <span style="color:var(--text-muted);">/ ${controller.daily_cap:.2f}</span>
      </div>
    </div>
  </div>

  <!-- Primary 3-column bento grid -->
  <div class="grid">
    
    <!-- Left: 16-key hardware state -->
    <div class="bento-card">
      <div class="card-head">
        <span>Gemini Fleet (16 Keys)</span>
        <button class="btn-action" onclick="copyCardData('key-table')">Copy</button>
      </div>
      <div class="key-grid" id="key-grid">
        {''.join(f'''
        <div class="key-box {'quarantined' if k['quarantined'] else ''}">
          <div class="key-title">
            <span style="color:{'var(--red)' if k['quarantined'] else 'var(--green)'};">{k['label']}</span>
            <span>{k['rpd_used']}/500</span>
          </div>
          <div style="color:var(--text-muted); font-size:9px;">TPM: {format_tokens(k['tpm_used'])}</div>
          <div class="key-bar">
            <div class="key-fill" style="width:{min(100, int((k['tpm_used'] / max(1, config.GEMINI_TPM_LIMIT)) * 100))}%; background:{'var(--red)' if k['tpm_used'] > 200000 else 'var(--green)'};"></div>
          </div>
        </div>
        ''' for k in pool_status["keys"])}
      </div>
    </div>

    <!-- Center: Model breakdown -->
    <div class="bento-card">
      <div class="card-head">
        <span>Model Breakdown</span>
        <button class="btn-action" onclick="copyCardData('model-table')">Copy</button>
      </div>
      <table id="model-table">
        <thead>
          <tr>
            <th>Model</th>
            <th class="mono-num">Reqs</th>
            <th class="mono-num">Cost</th>
            <th class="mono-num">Prompt</th>
            <th class="mono-num">Comp</th>
            <th class="mono-num">Reason</th>
            <th class="mono-num">Errors</th>
          </tr>
        </thead>
        <tbody>
          {''.join(f'''
          <tr>
            <td style="font-weight:600; color:{'var(--cyan)' if 'openrouter' in m else 'var(--text)'};">{m.split('/')[-1]}</td>
            <td class="mono-num">{b['reqs']:,}</td>
            <td class="mono-num">${b['usd']:.4f}</td>
            <td class="mono-num">{format_tokens(b['in'])} <span style="color:var(--text-muted); font-size:10px;">({format_tokens(b['cached'])})</span></td>
            <td class="mono-num">{format_tokens(b['out'])}</td>
            <td class="mono-num" style="color:var(--amber);">{format_tokens(b['reasoning'])}</td>
            <td class="mono-num" style="color:{'var(--red)' if b['errors'] else 'inherit'};">{b['errors']}</td>
          </tr>
          ''' for m, b in sorted(by_model.items())) if by_model else '<tr><td colspan="7" style="color:var(--text-muted); text-align:center;">No activity today</td></tr>'}
        </tbody>
      </table>
    </div>

    <!-- Right: Ranked tool calls -->
    <div class="bento-card">
      <div class="card-head">
        <span>Ranked Tool Invocations</span>
        <button class="btn-action" onclick="copyCardData('tool-table')">Copy</button>
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
        <tbody>
          {''.join(f'''
          <tr>
            <td style="color:var(--text-muted); font-size:10px;">#{idx+1}</td>
            <td style="font-weight:600; color:var(--text);">{t}</td>
            <td class="mono-num">{count:,}</td>
            <td class="mono-num" style="color:var(--cyan);">{count / max(1, sum(c for _, c in ranked_tools)) * 100:.1f}%</td>
          </tr>
          ''' for idx, (t, count) in enumerate(ranked_tools[:8])) if ranked_tools else '<tr><td colspan="4" style="color:var(--text-muted); text-align:center;">No tool calls logged</td></tr>'}
        </tbody>
      </table>
    </div>

  </div>

  <!-- Bottom: Requests & Errors Tables -->
  <div class="bento-card full-width">
    <div class="card-head">
      <div style="display:flex; align-items:center; gap:12px;">
        <span>Recent Requests</span>
        <div class="depth-pills" id="req-depth">
          DEPTH:
          <span class="active" onclick="setDepth('req', 8)">8</span>
          <span onclick="setDepth('req', 15)">15</span>
          <span onclick="setDepth('req', 30)">30</span>
          <span onclick="setDepth('req', 50)">50</span>
        </div>
      </div>
      <button class="btn-action" onclick="copyCardData('req-table')">Copy</button>
    </div>
    <div class="scroll-y">
      <table id="req-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Status</th>
            <th>Model</th>
            <th>Caller / Agent</th>
            <th>Key</th>
            <th class="mono-num">In (Cached)</th>
            <th class="mono-num">Out</th>
            <th class="mono-num">Cost</th>
            <th class="mono-num">Duration</th>
          </tr>
        </thead>
        <tbody id="req-tbody">
          <!-- Populated by JavaScript -->
        </tbody>
      </table>
    </div>
  </div>

  <div class="bento-card full-width">
    <div class="card-head">
      <div style="display:flex; align-items:center; gap:12px;">
        <span style="color:var(--red);">Recent Errors</span>
        <div class="depth-pills" id="err-depth">
          DEPTH:
          <span class="active" onclick="setDepth('err', 8)">8</span>
          <span onclick="setDepth('err', 15)">15</span>
          <span onclick="setDepth('err', 30)">30</span>
          <span onclick="setDepth('err', 50)">50</span>
        </div>
      </div>
      <button class="btn-action" onclick="copyCardData('err-table')">Copy</button>
    </div>
    <div class="scroll-y">
      <table id="err-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Status</th>
            <th>Model</th>
            <th>Caller / Agent</th>
            <th>Key</th>
            <th>Clean Error Cause</th>
            <th class="mono-num">Duration</th>
          </tr>
        </thead>
        <tbody id="err-tbody">
          <!-- Populated by JavaScript -->
        </tbody>
      </table>
    </div>
  </div>

  <script>
    const INITIAL_DATA = {json.dumps(initial_data)};
    let depthConfig = {{ req: 8, err: 8 }};

    function changeDay(day) {{
      window.location.href = window.location.pathname + "?day=" + day;
    }}

    function setDepth(type, n) {{
      depthConfig[type] = n;
      document.querySelectorAll(`#${{type}}-depth span`).forEach(el => {{
        el.classList.toggle('active', parseInt(el.textContent) === n);
      }});
      renderTables();
    }}

    function renderTables() {{
      const rows = INITIAL_DATA.rows || [];
      const reqTbody = document.getElementById("req-tbody");
      const errTbody = document.getElementById("err-tbody");

      // Render requests
      const recentReqs = rows.slice(-depthConfig.req).reverse();
      reqTbody.innerHTML = recentReqs.length ? recentReqs.map(r => `
        <tr>
          <td style="color:var(--text-muted);">${{(r.ts || '').substring(11, 19)}}Z</td>
          <td class="${{r.status < 400 ? 'status-ok' : 'status-err'}}">${{r.status || 200}}</td>
          <td>${{(r.m || '').split('/').pop()}}</td>
          <td><span class="os-badge">${{r.os || '?'}}</span> ${{r.caller || 'unknown'}}</td>
          <td><strong style="color:var(--green);">${{r.key || '-'}}</strong></td>
          <td class="mono-num">${{(r.in || 0).toLocaleString()}} <span style="color:var(--text-muted); font-size:10px;">(${{(r.c_in || 0).toLocaleString()}})</span></td>
          <td class="mono-num">${{(r.out || 0).toLocaleString()}}</td>
          <td class="mono-num" style="color:var(--cyan);">$${{parseFloat(r.cost || 0).toFixed(4)}}</td>
          <td class="mono-num">${{r.dur || 0}}s</td>
        </tr>
      `).join('') : '<tr><td colspan="9" style="text-align:center; color:var(--text-muted);">No requests</td></tr>';

      // Render errors
      const errRows = rows.filter(r => r.err || r.status >= 400);
      const recentErrs = errRows.slice(-depthConfig.err).reverse();
      errTbody.innerHTML = recentErrs.length ? recentErrs.map(r => `
        <tr>
          <td style="color:var(--text-muted);">${{(r.ts || '').substring(11, 19)}}Z</td>
          <td class="status-err">${{r.status || 500}}</td>
          <td>${{(r.m || '').split('/').pop()}}</td>
          <td><span class="os-badge">${{r.os || '?'}}</span> ${{r.caller || 'unknown'}}</td>
          <td><strong style="color:var(--red);">${{r.key || '-'}}</strong></td>
          <td style="color:var(--red);">${{r.err || 'Unknown Error'}}</td>
          <td class="mono-num">${{r.dur || 0}}s</td>
        </tr>
      `).join('') : '<tr><td colspan="7" style="text-align:center; color:var(--text-muted);">No errors recorded</td></tr>';
    }}

    // Formats any table on screen into compact Markdown and copies it to clipboard
    function copyCardData(tableId) {{
      const table = document.getElementById(tableId);
      if (!table) return;
      let text = `[TABLE:${{tableId.toUpperCase()}}]\n`;
      const rows = table.querySelectorAll("tr");
      rows.forEach(r => {{
        const cells = Array.from(r.querySelectorAll("th, td")).map(c => c.innerText.replace(/\\n/g, ' ').trim());
        if (cells.length) text += cells.join(" | ") + "\\n";
      }});
      navigator.clipboard.writeText(text).then(() => {{
        alert("Copied table as token-efficient markdown!");
      }});
    }}

    // Connect to Server-Sent Events stream for live dynamic updates
    function initSSE() {{
      const statusEl = document.getElementById("sse-status");
      const es = new EventSource("/api/live");

      es.onmessage = (event) => {{
        try {{
          const data = JSON.parse(event.data);
          if (data.type === "heartbeat") return;
          if (data.type === "telemetry" && data.row) {{
            INITIAL_DATA.rows.push(data.row);
            renderTables();
          }}
        }} catch(e) {{}}
      }};

      es.onerror = () => {{
        if (statusEl) {{
          statusEl.textContent = "○ POLLING";
          statusEl.className = "pill pill-amber";
        }}
      }};
    }}

    renderTables();
    initSSE();
  </script>
</body>
</html>
"""