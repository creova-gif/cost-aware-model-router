from __future__ import annotations

import html
import json
from typing import Any

from cost_aware_model_router.report import escape_json_for_script_tag


def render_live_finops_dashboard_html(
    economics: dict[str, Any] | None = None,
    shadow_summary: dict[str, Any] | None = None,
    recent_spans: list[dict[str, Any]] | None = None,
    circuits: dict[str, Any] | None = None,
    *,
    title: str = "Cost-Aware Model Router — Live FinOps Control Plane",
) -> str:
    """Render a zero-CDN, self-contained HTML live FinOps control-plane dashboard."""
    snapshot: dict[str, Any] = {
        "economics": economics,
        "shadow_summary": shadow_summary
        if shadow_summary is not None
        else {
            "total_shadow_requests": 0,
            "downgrade_count": 0,
            "primary_total_cost_usd": "0.00000000",
            "shadow_total_cost_usd": "0.00000000",
            "shadow_cost_delta_usd": "0.00000000",
            "records": [],
        },
        "recent_spans": recent_spans if recent_spans is not None else [],
        "circuits": circuits if circuits is not None else {"enabled": False, "circuits": {}},
    }
    safe_snapshot = escape_json_for_script_tag(json.dumps(snapshot, separators=(",", ":")))
    safe_title = html.escape(title, quote=True)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title}</title>
<style>
  :root {{
    --bg: #fafafa;
    --panel: #ffffff;
    --border: #d4d4d8;
    --border-strong: #18181b;
    --text: #18181b;
    --muted: #52525b;
    --accent: #0f172a;
    --pass-bg: #ecfdf5;
    --pass-fg: #065f46;
    --fail-bg: #fef2f2;
    --fail-fg: #991b1b;
    --warn-bg: #fffbeb;
    --warn-fg: #92400e;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 24px;
    line-height: 1.45;
  }}
  .container {{ max-width: 1280px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }}
  .panel {{ background: var(--panel); border: 1px solid var(--border); padding: 16px 18px; border-radius: 4px; }}
  .header-bar {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }}
  h1 {{ font-size: 19px; font-weight: 700; letter-spacing: -0.01em; }}
  h2 {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  h3 {{ font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin-bottom: 8px; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
  .sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .controls {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .badge {{ border: 1px solid var(--border-strong); padding: 3px 8px; font-size: 11px; font-family: ui-monospace, monospace; background: #f4f4f5; border-radius: 2px; }}
  .badge-pass {{ background: var(--pass-bg); color: var(--pass-fg); border-color: #34d399; }}
  .badge-warn {{ background: var(--warn-bg); color: var(--warn-fg); border-color: #fbbf24; }}
  .badge-fail {{ background: var(--fail-bg); color: var(--fail-fg); border-color: #f87171; }}
  .btn {{ border: 1px solid var(--border-strong); background: var(--accent); color: #ffffff; padding: 6px 12px; font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 3px; }}
  .btn:hover {{ background: #1e293b; }}
  .btn:disabled {{ opacity: 0.6; cursor: wait; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }}
  .kpi-card {{ background: var(--panel); border: 1px solid var(--border); padding: 14px; border-radius: 4px; }}
  .kpi-label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--muted); }}
  .kpi-value {{ font-size: 20px; font-weight: 700; margin: 6px 0 4px; font-family: ui-monospace, monospace; }}
  .kpi-detail {{ font-size: 12px; color: var(--muted); }}
  .breakdown-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
  .breakdown-box {{ border: 1px solid var(--border); padding: 12px; border-radius: 4px; background: #fcfcfd; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ border: 1px solid var(--border); padding: 7px 9px; text-align: left; vertical-align: top; }}
  th {{ background: #f4f4f5; font-weight: 600; }}
  .empty-state {{ display: none; padding: 14px; text-align: center; font-size: 12px; color: var(--muted); border: 1px dashed var(--border); margin-top: 8px; }}
  .status-banner {{ display: none; padding: 8px 12px; font-size: 12px; border-radius: 3px; border: 1px solid #fbbf24; background: var(--warn-bg); color: var(--warn-fg); }}
  .skip-link {{ position: absolute; left: -9999px; top: 0; background: #18181b; color: #ffffff; padding: 8px 12px; font-size: 12px; font-weight: 600; text-decoration: none; border-radius: 3px; z-index: 1000; }}
  .skip-link:focus {{ left: 12px; top: 12px; }}
  a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, [tabindex]:focus-visible {{ outline: 2px solid #2563eb; outline-offset: 2px; }}
  main {{ display: flex; flex-direction: column; gap: 16px; }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
  @media (max-width: 960px) {{
    .kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .breakdown-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to main content</a>
<div class="container">
  <header class="panel header-bar">
    <div>
      <h1>{safe_title}</h1>
      <div class="sub mono" id="refresh-status" aria-live="polite">Initial server snapshot loaded</div>
    </div>
    <div class="controls">
      <span class="badge" id="thermostat-badge">Thermostat: Unknown</span>
      <button class="btn" id="btn-refresh" type="button">Refresh Live Metrics</button>
    </div>
  </header>

  <main id="main-content">
  <div class="status-banner" id="status-banner" role="status" aria-live="polite"></div>

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">Total Spend</div>
      <div class="kpi-value" id="kpi-total-spend">$0.0000</div>
      <div class="kpi-detail" id="kpi-total-spend-sub">0 requests</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Cost / Request</div>
      <div class="kpi-value" id="kpi-cost-per-req">$0.00000</div>
      <div class="kpi-detail" id="kpi-cost-per-req-sub">—</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Goodput (Cost / Success)</div>
      <div class="kpi-value" id="kpi-goodput">—</div>
      <div class="kpi-detail" id="kpi-goodput-sub">—</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Cache Hits</div>
      <div class="kpi-value" id="kpi-cache-hits">0</div>
      <div class="kpi-detail" id="kpi-cache-hits-sub">0.0% hit rate</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Shadow Savings Delta</div>
      <div class="kpi-value" id="kpi-shadow-delta">$0.0000</div>
      <div class="kpi-detail" id="kpi-shadow-delta-sub">0 shadow requests</div>
    </div>
  </div>

  <div class="panel">
    <h2>1. Spend Breakdown</h2>
    <div class="breakdown-grid">
      <div class="breakdown-box">
        <h3>By Org (org_id)</h3>
        <table>
          <thead><tr><th scope="col">org_id</th><th scope="col">Spend (USD)</th></tr></thead>
          <tbody id="tbody-org"></tbody>
        </table>
        <div class="empty-state" id="empty-org">No org spend recorded.</div>
      </div>
      <div class="breakdown-box">
        <h3>By Agent (agent_id)</h3>
        <table>
          <thead><tr><th scope="col">agent_id</th><th scope="col">Spend (USD)</th></tr></thead>
          <tbody id="tbody-agent"></tbody>
        </table>
        <div class="empty-state" id="empty-agent">No agent spend recorded.</div>
      </div>
      <div class="breakdown-box">
        <h3>By Category</h3>
        <table>
          <thead><tr><th scope="col">category</th><th scope="col">Spend (USD)</th></tr></thead>
          <tbody id="tbody-category"></tbody>
        </table>
        <div class="empty-state" id="empty-category">No category spend recorded.</div>
      </div>
      <div class="breakdown-box">
        <h3>By Tier</h3>
        <table>
          <thead><tr><th scope="col">tier</th><th scope="col">Spend (USD)</th></tr></thead>
          <tbody id="tbody-tier"></tbody>
        </table>
        <div class="empty-state" id="empty-tier">No tier spend recorded.</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>2. Shadow Routing Comparison</h2>
    <table>
      <thead>
        <tr>
          <th scope="col">Request ID</th>
          <th scope="col">Org / Agent</th>
          <th scope="col">Primary Policy</th>
          <th scope="col">Shadow Policy</th>
          <th scope="col">Delta (USD)</th>
          <th scope="col">Would Downgrade</th>
          <th scope="col">Explanation</th>
        </tr>
      </thead>
      <tbody id="tbody-shadow"></tbody>
    </table>
    <div class="empty-state" id="empty-shadow">No shadow routing comparisons recorded yet.</div>
  </div>

  <div class="panel">
    <h2>3. Recent OpenTelemetry gen_ai.* Spans</h2>
    <table>
      <thead>
        <tr>
          <th scope="col">Trace / Span ID</th>
          <th scope="col">gen_ai.system / model</th>
          <th scope="col">Tokens (In / Out)</th>
          <th scope="col">Org / Agent</th>
          <th scope="col">Policy / Tier</th>
          <th scope="col">Cost (USD)</th>
          <th scope="col">Provenance / Shadow</th>
        </tr>
      </thead>
      <tbody id="tbody-spans"></tbody>
    </table>
    <div class="empty-state" id="empty-spans">No OpenTelemetry spans recorded yet.</div>
  </div>

  <div class="panel">
    <h2>4. Circuit Breaker Health</h2>
    <table>
      <thead>
        <tr>
          <th scope="col">Model Tier</th>
          <th scope="col">Circuit State</th>
        </tr>
      </thead>
      <tbody id="tbody-circuits"></tbody>
    </table>
    <div class="empty-state" id="empty-circuits">Circuit breaker disabled</div>
  </div>
  </main>
</div>

<script id="finops-snapshot" type="application/json">{safe_snapshot}</script>
<script>
(function() {{
  "use strict";
  const state = JSON.parse(document.getElementById("finops-snapshot").textContent);

  function clearNode(node) {{
    while (node.firstChild) {{
      node.removeChild(node.firstChild);
    }}
  }}

  function fmtUsd(raw, digits) {{
    if (raw === null || raw === undefined || raw === "") return "—";
    const num = Number(raw);
    if (!Number.isFinite(num)) return String(raw);
    const sign = num < 0 ? "-" : "";
    return sign + "$" + Math.abs(num).toFixed(digits);
  }}

  function addCell(tr, text, className) {{
    const td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = text;
    tr.appendChild(td);
  }}

  function renderBreakdownTable(tbodyId, emptyId, mapping) {{
    const tbody = document.getElementById(tbodyId);
    const emptyEl = document.getElementById(emptyId);
    clearNode(tbody);
    const entries = mapping ? Object.entries(mapping) : [];
    if (entries.length === 0) {{
      emptyEl.style.display = "block";
      return;
    }}
    emptyEl.style.display = "none";
    entries.forEach(function(pair) {{
      const tr = document.createElement("tr");
      addCell(tr, pair[0], "mono");
      addCell(tr, fmtUsd(pair[1], 6), "mono");
      tbody.appendChild(tr);
    }});
  }}

  function renderDashboard() {{
    const econ = state.economics;
    const shadow = state.shadow_summary || {{}};
    const spans = state.recent_spans || [];

    const badge = document.getElementById("thermostat-badge");
    const banner = document.getElementById("status-banner");

    if (econ) {{
      badge.textContent = "Thermostat: Active";
      badge.className = "badge badge-pass";
      banner.style.display = "none";

      const totalReq = econ.total_requests || 0;
      const passedReq = econ.passed_requests || 0;
      const failedReq = econ.failed_requests || 0;
      const cacheHits = econ.cache_hits || 0;
      const passRate = totalReq > 0 ? ((passedReq / totalReq) * 100).toFixed(1) : "0.0";
      const hitRate = totalReq > 0 ? ((cacheHits / totalReq) * 100).toFixed(1) : "0.0";

      document.getElementById("kpi-total-spend").textContent = fmtUsd(econ.total_cost_usd, 4);
      document.getElementById("kpi-total-spend-sub").textContent =
        totalReq + " reqs (measured: " + fmtUsd(econ.measured_cost_usd, 4) +
        ", modeled: " + fmtUsd(econ.modeled_cost_usd, 4) + ")";

      document.getElementById("kpi-cost-per-req").textContent = fmtUsd(econ.cost_per_request_usd, 5);
      document.getElementById("kpi-cost-per-req-sub").textContent =
        "Passed: " + passedReq + " | Failed: " + failedReq;

      document.getElementById("kpi-goodput").textContent =
        econ.cost_per_successful_outcome_usd !== null && econ.cost_per_successful_outcome_usd !== undefined
          ? fmtUsd(econ.cost_per_successful_outcome_usd, 5)
          : "—";
      document.getElementById("kpi-goodput-sub").textContent =
        "Pass rate: " + passRate + "% (" + passedReq + "/" + totalReq + ")";

      document.getElementById("kpi-cache-hits").textContent = String(cacheHits);
      document.getElementById("kpi-cache-hits-sub").textContent = hitRate + "% cache hit rate";

      renderBreakdownTable("tbody-org", "empty-org", econ.spend_by_org);
      renderBreakdownTable("tbody-agent", "empty-agent", econ.spend_by_agent);
      renderBreakdownTable("tbody-category", "empty-category", econ.spend_by_category);
      renderBreakdownTable("tbody-tier", "empty-tier", econ.spend_by_tier);
    }} else {{
      badge.textContent = "Thermostat: Not Configured";
      badge.className = "badge badge-warn";
      banner.style.display = "block";
      banner.textContent = "Thermostat is not configured on this gateway; unit-economics ledger metrics are unavailable.";

      document.getElementById("kpi-total-spend").textContent = "—";
      document.getElementById("kpi-total-spend-sub").textContent = "Thermostat disabled";
      document.getElementById("kpi-cost-per-req").textContent = "—";
      document.getElementById("kpi-cost-per-req-sub").textContent = "—";
      document.getElementById("kpi-goodput").textContent = "—";
      document.getElementById("kpi-goodput-sub").textContent = "—";
      document.getElementById("kpi-cache-hits").textContent = "—";
      document.getElementById("kpi-cache-hits-sub").textContent = "—";

      renderBreakdownTable("tbody-org", "empty-org", null);
      renderBreakdownTable("tbody-agent", "empty-agent", null);
      renderBreakdownTable("tbody-category", "empty-category", null);
      renderBreakdownTable("tbody-tier", "empty-tier", null);
    }}

    const totalShadow = shadow.total_shadow_requests || 0;
    const downgrades = shadow.downgrade_count || 0;
    document.getElementById("kpi-shadow-delta").textContent = fmtUsd(shadow.shadow_cost_delta_usd || "0", 4);
    document.getElementById("kpi-shadow-delta-sub").textContent =
      totalShadow + " shadow reqs (" + downgrades + " downgrades)";

    const shadowTbody = document.getElementById("tbody-shadow");
    const emptyShadow = document.getElementById("empty-shadow");
    clearNode(shadowTbody);
    const records = shadow.records || [];
    if (records.length === 0) {{
      emptyShadow.style.display = "block";
    }} else {{
      emptyShadow.style.display = "none";
      records.slice().reverse().forEach(function(rec) {{
        const tr = document.createElement("tr");
        addCell(tr, rec.request_id || "", "mono");
        addCell(tr, (rec.org_id || "") + " / " + (rec.agent_id || ""), "mono");
        addCell(tr, (rec.primary_policy_id || "") + " (" + (rec.primary_tier || "") + ", " + fmtUsd(rec.primary_cost_usd, 6) + ")", "mono");
        addCell(tr, (rec.shadow_policy_id || "") + " (" + (rec.shadow_tier || "") + ", " + fmtUsd(rec.shadow_cost_usd, 6) + ")", "mono");
        addCell(tr, fmtUsd(rec.shadow_cost_delta_usd, 6), "mono");
        addCell(tr, rec.would_downgrade ? "YES" : "NO", rec.would_downgrade ? "badge-pass mono" : "mono");
        addCell(tr, rec.explanation || "", "");
        shadowTbody.appendChild(tr);
      }});
    }}

    const spansTbody = document.getElementById("tbody-spans");
    const emptySpans = document.getElementById("empty-spans");
    clearNode(spansTbody);
    if (spans.length === 0) {{
      emptySpans.style.display = "block";
    }} else {{
      emptySpans.style.display = "none";
      spans.slice().reverse().forEach(function(span) {{
        const attrs = span.attributes || {{}};
        const tr = document.createElement("tr");
        const traceShort = (span.trace_id || "").slice(0, 12);
        const spanShort = (span.span_id || "").slice(0, 8);
        addCell(tr, traceShort + "... / " + spanShort, "mono");
        addCell(tr, (attrs["gen_ai.system"] || "") + " / " + (attrs["gen_ai.request.model"] || ""), "mono");
        addCell(tr, String(attrs["gen_ai.usage.input_tokens"] ?? 0) + " / " + String(attrs["gen_ai.usage.output_tokens"] ?? 0), "mono");
        addCell(tr, (attrs["cost_router.org_id"] || "") + " / " + (attrs["cost_router.agent_id"] || ""), "mono");
        addCell(tr, (attrs["cost_router.policy_id"] || "") + " (" + (attrs["cost_router.selected_tier"] || "") + ")", "mono");
        addCell(tr, fmtUsd(attrs["cost_router.cost_usd"], 6), "mono");
        addCell(tr, String(attrs["cost_router.cost_provenance"] || "") + " | shadow=" + String(Boolean(attrs["cost_router.shadow_mode"])), "mono");
        spansTbody.appendChild(tr);
      }});
    }}

    const circuitsPayload = state.circuits || {{ enabled: false, circuits: {{}} }};
    const circuitsTbody = document.getElementById("tbody-circuits");
    const emptyCircuits = document.getElementById("empty-circuits");
    clearNode(circuitsTbody);
    if (!circuitsPayload.enabled) {{
      emptyCircuits.textContent = "Circuit breaker disabled";
      emptyCircuits.style.display = "block";
    }} else {{
      const circuitEntries = Object.entries(circuitsPayload.circuits || {{}});
      if (circuitEntries.length === 0) {{
        emptyCircuits.textContent = "No circuit breaker tiers recorded.";
        emptyCircuits.style.display = "block";
      }} else {{
        emptyCircuits.style.display = "none";
        circuitEntries.forEach(function(pair) {{
          const tr = document.createElement("tr");
          addCell(tr, pair[0], "mono");
          const stateVal = String(pair[1] || "");
          let stateClass = "badge mono";
          if (stateVal === "closed") {{
            stateClass = "badge badge-pass mono";
          }} else if (stateVal === "half_open") {{
            stateClass = "badge badge-warn mono";
          }} else if (stateVal === "open") {{
            stateClass = "badge badge-fail mono";
          }}
          const td = document.createElement("td");
          const span = document.createElement("span");
          span.className = stateClass;
          span.textContent = stateVal;
          td.appendChild(span);
          tr.appendChild(td);
          circuitsTbody.appendChild(tr);
        }});
      }}
    }}
  }}

  async function refreshLiveMetrics() {{
    const btn = document.getElementById("btn-refresh");
    const statusEl = document.getElementById("refresh-status");
    btn.disabled = true;
    statusEl.textContent = "Refreshing live FinOps metrics...";
    try {{
      const [econResp, shadowResp, circuitsResp] = await Promise.all([
        fetch("/v1/finops/economics", {{ cache: "no-store" }}),
        fetch("/v1/finops/shadow-summary", {{ cache: "no-store" }}),
        fetch("/v1/finops/circuits", {{ cache: "no-store" }})
      ]);
      if (econResp.ok) {{
        state.economics = await econResp.json();
      }} else if (econResp.status === 400) {{
        state.economics = null;
      }}
      if (shadowResp.ok) {{
        state.shadow_summary = await shadowResp.json();
      }}
      if (circuitsResp.ok) {{
        state.circuits = await circuitsResp.json();
      }} else if (
        circuitsResp.status === 401 ||
        circuitsResp.status === 403 ||
        circuitsResp.status === 404
      ) {{
        // Gracefully tolerate unauthenticated or unavailable circuits endpoint.
      }}
      renderDashboard();
      statusEl.textContent = "Last refreshed at " + new Date().toLocaleTimeString();
    }} catch (err) {{
      statusEl.textContent = "Refresh failed: " + String(err);
    }} finally {{
      btn.disabled = false;
    }}
  }}

  document.getElementById("btn-refresh").addEventListener("click", refreshLiveMetrics);
  renderDashboard();
}})();
</script>
</body>
</html>
"""
