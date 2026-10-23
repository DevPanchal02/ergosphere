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

# Discovers all dates by inspecting filenames and timestamps inside all log files
def get_available_log_days() -> List[Dict[str, Any]]:
    days = set()
    current_day = controller.get_today_str(time.time())
    days.add(current_day)
    
    all_files = analytics.get_all_log_files()

    # 1. Regex check on filenames
    for path in all_files:
        base = os.path.basename(path)
        if m := re.search(r"\d{4}-\d{2}-\d{2}", base):
            days.add(m.group(0))

    # 2. Inspect rows inside files so sessions spanning past midnight (like 2026-10-21) are discovered
    for path in all_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if stripped := line.strip():
                        try:
                            d = json.loads(stripped)
                            if day := analytics.row_day(d):
                                days.add(day)
                        except Exception:
                            continue
        except OSError:
            pass

    # Pre-calculate spend for every discovered day
    results = []
    for d in sorted(days, reverse=True):
        rows = analytics.read_day_rows(d)
        if not rows and d != current_day:
            continue
        cost = sum(float(r.get("cost") or 0.0) for r in rows)
        results.append({"day": d, "cost": cost, "count": len(rows)})
    return results

def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)

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

def render_html_dashboard(selected_day: Optional[str] = None) -> str:
    now = time.time()
    today_str = controller.get_today_str(now)
    active_day = selected_day or today_str
    day_entries = get_available_log_days()

    rows = analytics.read_day_rows(active_day)
    by_model, by_caller, ranked_tools = analytics.summarize_rows(rows)
    pool_status = key_manager.get_pool_status(now)

    total_reqs = len(rows)
    total_errs = sum(1 for r in rows if r.get("err") or int(r.get("status") or 200) >= 400)
    total_spend = sum(float(r.get("cost") or 0.0) for r in rows)
    active_rpm = analytics.get_rolling_rpm(rows, now) if active_day == today_str else 0

    key_cards = []
    gemini_day_calls = 0
    for i, (n, _) in enumerate(key_manager.keys):
        label = f"G{n}"
        k_rows = [r for r in rows if r.get("key") == label]
        k_reqs = len(k_rows)
        gemini_day_calls += k_reqs
        k_tokens = sum(int(r.get("in") or 0) + int(r.get("out") or 0) for r in k_rows)
        k_errs = sum(1 for r in k_rows if r.get("err") or int(r.get("status") or 200) >= 400)
        is_quarantined = key_manager.quarantined[i] if i < len(key_manager.quarantined) else False
        live_tpm = key_manager.get_tpm_usage(i, now) if active_day == today_str else 0

        key_cards.append({
            "label": label,
            "reqs": k_reqs,
            "tokens": k_tokens,
            "errs": k_errs,
            "tpm_used": live_tpm,
            "quarantined": is_quarantined
        })

    max_rpd_pool = len(key_manager.keys) * config.GEMINI_RPD_LIMIT
    rpd_pct = (gemini_day_calls / max(1, max_rpd_pool)) * 100
    
    dt = datetime.fromtimestamp(now, config.TZ)
    seconds_in_day = dt.hour * 3600 + dt.minute * 60 + dt.second
    day_pct = (seconds_in_day / 86400) * 100
    pacing_status = "SURPLUS" if rpd_pct <= day_pct else "PACING"
    pacing_color = "#10b981" if pacing_status == "SURPLUS" else "#f59e0b"

    initial_data = {
        "active_day": active_day,
        "today_str": today_str,
        "rows": rows,
        "key_cards": key_cards,
        "spend": {
            "day_usd": round(total_spend, 4),
            "daily_cap": controller.daily_cap,
            "run_usd": controller.ledger["run_usd"],
            "run_cap": controller.run_cap
        }
    }

    # Compact button label for the top bar
    active_button_label = "Today" if active_day == today_str else active_day

    # Dropdown menu items with right-aligned spend
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

    .top-bar {{ display: flex; align-items: center; justify-content: space-between; background: var(--card); border: 1px solid var(--card-border); padding: 6px 12px; margin-bottom: 8px; border-radius: 4px; }}
    .brand {{ display: flex; align-items: center; gap: 8px; font-weight: 700; color: var(--cyan); font-size: 14px; letter-spacing: 0.5px; }}
    .pill {{ padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
    .pill-green {{ background: rgba(16, 185, 129, 0.15); color: var(--green); }}
    .pill-amber {{ background: rgba(245, 158, 11, 0.15); color: var(--amber); }}
    .pill-red {{ background: rgba(239, 68, 68, 0.15); color: var(--red); }}
    .pill-muted {{ background: #1e293b; color: var(--text-muted); }}

    /* Compact floating dropdown */
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
      overflow: hidden;
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

    .grid {{ display: grid; grid-template-columns: 360px 1fr 340px; gap: 8px; margin-bottom: 8px; }}
    .bento-card {{ background: var(--card); border: 1px solid var(--card-border); border-radius: 4px; padding: 8px; display: flex; flex-direction: column; overflow: hidden; }}
    .card-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; padding-bottom: 4px; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
    .card-actions {{ display: flex; align-items: center; gap: 6px; }}

    .btn-action {{ background: #1e293b; border: 1px solid #334155; color: var(--text); padding: 2px 6px; font-size: 10px; border-radius: 3px; cursor: pointer; }}
    .btn-action:hover {{ background: #334155; }}
    .depth-pills span {{ cursor: pointer; padding: 1px 4px; border-radius: 2px; font-size: 10px; color: var(--text-muted); }}
    .depth-pills span.active {{ background: var(--accent); color: #fff; font-weight: 700; }}

    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th {{ text-align: left; color: var(--text-muted); font-size: 10px; padding: 4px; font-weight: 600; text-transform: uppercase; }}
    td {{ padding: 4px; border-top: 1px solid rgba(255,255,255,0.03); white-space: nowrap; }}
    tr:hover td {{ background: rgba(255,255,255,0.02); }}
    .status-ok {{ color: var(--green); font-weight: 700; }}
    .status-err {{ color: var(--red); font-weight: 700; }}
    .mono-num {{ text-align: right; font-variant-numeric: tabular-nums; }}

    .key-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin-top: 4px; }}
    .key-box {{ background: rgba(0,0,0,0.25); border: 1px solid var(--card-border); padding: 5px; border-radius: 3px; font-size: 10px; }}
    .key-box.quarantined {{ border-color: var(--red); opacity: 0.6; }}
    .key-title {{ display: flex; justify-content: space-between; font-weight: 700; }}
    .key-metrics {{ display: flex; justify-content: space-between; color: var(--text-muted); font-size: 9px; margin-top: 2px; }}

    .full-width {{ margin-bottom: 8px; }}
    .scroll-y {{ max-height: 280px; overflow-y: auto; }}
    .os-badge {{ font-size: 9px; padding: 0 3px; border-radius: 2px; background: #1e293b; color: var(--text-muted); text-transform: uppercase; }}
  </style>
</head>
<body>

  <div class="top-bar">
    <div class="brand">
      <span>⚡ HORIZON PROXY</span>
      
      <!-- Compact trigger button: takes minimal room on the top bar -->
      <div class="dropdown">
        <button class="dropdown-trigger" onclick="toggleDropdown(event)">
          <span>{active_button_label}</span>
          <span style="font-size:8px; color:var(--text-muted);">▼</span>
        </button>
        <!-- Floating menu: opens with date on left and cost on right -->
        <div class="dropdown-menu" id="day-dropdown">
          {''.join(dropdown_items_html)}
        </div>
      </div>

      <span class="pill" id="sse-status">INITIALIZING</span>
    </div>

    <div style="display:flex; align-items:center; gap:16px; font-size:11px;">
      <div>POOL TPM: <strong style="color:var(--cyan);">{format_tokens(pool_status["pool_tpm_available"])}</strong> / {format_tokens(pool_status["pool_tpm_max"])}</div>
      <div>RPM: <strong style="color:var(--cyan);">{active_rpm}</strong> / {pool_status["pool_rpm_max"]}</div>
      <div>KEYS: <strong style="color:var(--green);">{pool_status["healthy_keys"]}</strong></div>
      <div>RPD: <strong>{gemini_day_calls:,}</strong> / {max_rpd_pool:,} (<span style="color:{pacing_color}; font-weight:700;">{pacing_status}</span>)</div>
      <div style="background:rgba(56,189,248,0.1); border:1px solid rgba(56,189,248,0.3); padding:3px 8px; border-radius:3px;">
        PAID SPEND: <strong style="color:var(--cyan); font-size:12px;">${total_spend:.4f}</strong> <span style="color:var(--text-muted);">/ ${controller.daily_cap:.2f}</span>
      </div>
    </div>
  </div>

  <div class="grid">
    
    <div class="bento-card">
      <div class="card-head">
        <span>Gemini Fleet (16 Keys)</span>
        <button class="btn-action" onclick="copyCardData('key-grid')">Copy</button>
      </div>
      <div class="key-grid" id="key-grid">
        {''.join(f'''
        <div class="key-box {'quarantined' if k['quarantined'] else ''}">
          <div class="key-title">
            <span style="color:{'var(--red)' if k['quarantined'] else 'var(--green)'};">{k['label']}</span>
            <span style="color:{'var(--red)' if k['errs'] else 'var(--text-muted)'};">{k['reqs']} reqs</span>
          </div>
          <div class="key-metrics">
            <span>{format_tokens(k['tokens'])}</span>
            <span style="color:{'var(--red)' if k['errs'] else 'inherit'};">{k['errs']} errs</span>
          </div>
        </div>
        ''' for k in key_cards)}
      </div>
    </div>

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
        </tbody>
      </table>
    </div>
  </div>

  <script>
    const INITIAL_DATA = {json.dumps(initial_data)};
    let depthConfig = {{ req: 8, err: 8 }};

    function toggleDropdown(e) {{
      e.stopPropagation();
      const menu = document.getElementById("day-dropdown");
      menu.classList.toggle("show");
    }}

    window.addEventListener("click", () => {{
      const menu = document.getElementById("day-dropdown");
      if (menu) menu.classList.remove("show");
    }});

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

    function getCallerLabel(r) {{
      if (r.caller && r.caller !== 'unknown') {{
        return `<span class="os-badge">${{r.os || '?'}}</span> ${{r.caller}}`;
      }}
      if (r.tool_names && r.tool_names.length) {{
        return `<span class="os-badge">agent</span> ${{r.tool_names.slice(0, 2).join(',')}}${{r.tool_names.length > 2 ? '...' : ''}}`;
      }}
      return `<span class="os-badge">prompt</span> ${{r.tools_in || 0}} tools`;
    }}

    function renderTables() {{
      const rows = INITIAL_DATA.rows || [];
      const reqTbody = document.getElementById("req-tbody");
      const errTbody = document.getElementById("err-tbody");

      const recentReqs = rows.slice(-depthConfig.req).reverse();
      reqTbody.innerHTML = recentReqs.length ? recentReqs.map(r => `
        <tr>
          <td style="color:var(--text-muted);">${{(r.ts || '').substring(11, 19)}}Z</td>
          <td class="${{r.status < 400 ? 'status-ok' : 'status-err'}}">${{r.status || 200}}</td>
          <td>${{(r.m || '').split('/').pop()}}</td>
          <td>${{getCallerLabel(r)}}</td>
          <td><strong style="color:var(--green);">${{r.key || '-'}}</strong></td>
          <td class="mono-num">${{(r.in || 0).toLocaleString()}} <span style="color:var(--text-muted); font-size:10px;">(${{(r.c_in || 0).toLocaleString()}})</span></td>
          <td class="mono-num">${{(r.out || 0).toLocaleString()}}</td>
          <td class="mono-num" style="color:var(--cyan);">$${{parseFloat(r.cost || 0).toFixed(4)}}</td>
          <td class="mono-num">${{r.dur || 0}}s</td>
        </tr>
      `).join('') : '<tr><td colspan="9" style="text-align:center; color:var(--text-muted);">No requests</td></tr>';

      const errRows = rows.filter(r => r.err || r.status >= 400);
      const recentErrs = errRows.slice(-depthConfig.err).reverse();
      errTbody.innerHTML = recentErrs.length ? recentErrs.map(r => `
        <tr>
          <td style="color:var(--text-muted);">${{(r.ts || '').substring(11, 19)}}Z</td>
          <td class="status-err">${{r.status || 500}}</td>
          <td>${{(r.m || '').split('/').pop()}}</td>
          <td>${{getCallerLabel(r)}}</td>
          <td><strong style="color:var(--red);">${{r.key || '-'}}</strong></td>
          <td style="color:var(--red);">${{r.err || 'Unknown Error'}}</td>
          <td class="mono-num">${{r.dur || 0}}s</td>
        </tr>
      `).join('') : '<tr><td colspan="7" style="text-align:center; color:var(--text-muted);">No errors recorded</td></tr>';
    }}

    function copyCardData(elementId) {{
      const el = document.getElementById(elementId);
      if (!el) return;
      let text = `[EXPORT:${{elementId.toUpperCase()}}]\n`;
      const rows = el.querySelectorAll("tr");
      if (rows.length) {{
        rows.forEach(r => {{
          const cells = Array.from(r.querySelectorAll("th, td")).map(c => c.innerText.replace(/\\n/g, ' ').trim());
          if (cells.length) text += cells.join(" | ") + "\\n";
        }});
      }} else {{
        el.querySelectorAll(".key-box").forEach(k => {{
          text += k.innerText.replace(/\\n/g, ' | ') + "\\n";
        }});
      }}
      navigator.clipboard.writeText(text).then(() => {{
        alert("Copied to clipboard!");
      }});
    }}

    function setupStatusAndSSE() {{
      const statusEl = document.getElementById("sse-status");
      const isToday = (INITIAL_DATA.active_day === INITIAL_DATA.today_str);

      if (!isToday) {{
        statusEl.textContent = "ARCHIVED";
        statusEl.className = "pill pill-muted";
        return;
      }}

      statusEl.textContent = "○ PENDING";
      statusEl.className = "pill pill-amber";

      const es = new EventSource("/api/live");

      es.onopen = () => {{
        statusEl.textContent = "● LIVE";
        statusEl.className = "pill pill-green";
      }};

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
        if (es.readyState === EventSource.CONNECTING) {{
          statusEl.textContent = "○ PENDING";
          statusEl.className = "pill pill-amber";
        }} else {{
          statusEl.textContent = "✕ OFFLINE";
          statusEl.className = "pill pill-red";
        }}
      }};
    }}

    renderTables();
    setupStatusAndSSE();
  </script>
</body>
</html>
"""