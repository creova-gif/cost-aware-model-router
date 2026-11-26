from __future__ import annotations

import html
import json

from cost_aware_model_router.schemas import SavingsReceipt


def escape_json_for_script_tag(json_str: str) -> str:
    """
    Escape `<`, `>`, `&`, and line separators so JSON inside
    `<script type="application/json">` can never break out or trigger HTML5 script states.
    """
    return (
        json_str.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_pareto_explorer_html(receipt: SavingsReceipt, markdown_receipt: str) -> str:
    safe_json = escape_json_for_script_tag(receipt.model_dump_json())
    safe_md = escape_json_for_script_tag(json.dumps(markdown_receipt))
    workload_title = html.escape(receipt.workload_name, quote=True)
    digest_short = html.escape(receipt.integrity.digest[:16], quote=True)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cost-Aware Model Router — Pareto Explorer ({workload_title})</title>
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
  .container {{ max-width: 1240px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }}
  main {{ display: flex; flex-direction: column; gap: 16px; }}
  .skip-link {{
    position: absolute;
    top: -40px;
    left: 0;
    background: var(--accent);
    color: #ffffff;
    padding: 8px 14px;
    z-index: 1000;
    font-size: 13px;
    font-weight: 600;
    text-decoration: none;
    border-radius: 0 0 4px 0;
  }}
  .skip-link:focus {{
    top: 0;
  }}
  :focus-visible {{
    outline: 2px solid #2563eb;
    outline-offset: 2px;
  }}
  @media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.01ms !important;
      scroll-behavior: auto !important;
    }}
  }}
  .panel {{ background: var(--panel); border: 1px solid var(--border); padding: 16px 18px; border-radius: 4px; }}
  .header-bar {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }}
  h1 {{ font-size: 19px; font-weight: 700; letter-spacing: -0.01em; }}
  h2 {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
  .sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .badges {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .badge {{ border: 1px solid var(--border-strong); padding: 2px 8px; font-size: 11px; font-family: ui-monospace, monospace; background: #f4f4f5; border-radius: 2px; }}
  .badge-pass {{ background: var(--pass-bg); color: var(--pass-fg); border-color: #34d399; }}
  .badge-fail {{ background: var(--fail-bg); color: var(--fail-fg); border-color: #f87171; }}
  .badge-warn {{ background: var(--warn-bg); color: var(--warn-fg); border-color: #fbbf24; }}
  .btn {{ border: 1px solid var(--border-strong); background: var(--panel); color: var(--text); padding: 5px 10px; font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 3px; }}
  .btn:hover {{ background: #f4f4f5; }}
  .btn-active {{ background: var(--accent); color: #fff; }}
  .btn-active:hover {{ background: #1e293b; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  .kpi-card {{ background: var(--panel); border: 1px solid var(--border); padding: 14px; border-radius: 4px; }}
  .kpi-label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--muted); }}
  .kpi-value {{ font-size: 22px; font-weight: 700; margin: 6px 0 4px; font-family: ui-monospace, monospace; }}
  .kpi-detail {{ font-size: 12px; color: var(--muted); }}
  .grid-2 {{ display: grid; grid-template-columns: 1.55fr 1fr; gap: 16px; }}
  .slider-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; background: #f4f4f5; padding: 10px 12px; border-radius: 4px; }}
  .slider-row input[type=range] {{ flex: 1; cursor: pointer; }}
  .banner-warn {{ display: none; background: var(--warn-bg); color: var(--warn-fg); border: 1px solid #fbbf24; padding: 8px 12px; font-size: 12px; margin-bottom: 10px; border-radius: 3px; }}
  .chart-wrap {{ border: 1px solid var(--border); background: #fcfcfd; height: 280px; position: relative; }}
  .summary-box {{ border: 1px dashed var(--border); padding: 12px; background: #fafafa; margin-bottom: 12px; font-size: 12px; }}
  .summary-box div {{ margin-bottom: 4px; }}
  .filter-bar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; gap: 8px; flex-wrap: wrap; }}
  .filter-tabs {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ border: 1px solid var(--border); padding: 7px 9px; text-align: left; vertical-align: top; }}
  th {{ background: #f4f4f5; font-weight: 600; }}
  .empty-state {{ display: none; padding: 18px; text-align: center; font-size: 13px; color: var(--muted); border: 1px dashed var(--border); margin-top: 8px; }}
</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to main content</a>
<div class="container">
  <header class="panel header-bar">
    <div>
      <h1>Cost-Aware Model Router — Pareto Explorer &amp; Auditable Savings Receipt</h1>
      <div class="sub mono" id="header-meta">Workload: {workload_title} | Integrity SHA-256: {digest_short}...</div>
    </div>
    <div class="badges">
      <span class="badge" id="badge-measured">MEASURED: 0</span>
      <span class="badge" id="badge-modeled">MODELED: 0</span>
      <span class="badge badge-warn" id="badge-unknown">UNKNOWN EXCLUDED: 0</span>
      <button class="btn" id="btn-dl-json" type="button">Download JSON Receipt</button>
      <button class="btn" id="btn-dl-md" type="button">Download MD Receipt</button>
    </div>
  </header>

  <main id="main-content">
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">Baseline (All-Frontier)</div>
      <div class="kpi-value" id="kpi-base-cost">$0.0000</div>
      <div class="kpi-detail" id="kpi-base-sub">—</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label" id="kpi-sel-label">Selected Policy</div>
      <div class="kpi-value" id="kpi-sel-cost">$0.0000</div>
      <div class="kpi-detail" id="kpi-sel-sub">—</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Savings &amp; Goodput (Cost / Pass)</div>
      <div class="kpi-value" id="kpi-delta-cost">0.0%</div>
      <div class="kpi-detail" id="kpi-delta-sub">—</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">3-Tier Allocation (C / M / F)</div>
      <div class="kpi-value" id="kpi-tier-mix">0 / 0 / 0</div>
      <div class="kpi-detail" id="kpi-tier-sub">—</div>
    </div>
  </div>

  <div class="grid-2">
    <div class="panel">
      <h2>1. Interactive 2D Cost vs Quality Pareto Frontier</h2>
      <div class="slider-row">
        <label for="quality-slider" class="mono"><strong>Quality Floor (q_min):</strong></label>
        <input id="quality-slider" type="range" min="50" max="100" step="1" value="92" aria-label="Minimum quality floor">
        <span class="mono" id="quality-slider-val"><strong>92%</strong></span>
      </div>
      <div class="banner-warn" id="infeasible-banner" aria-live="polite"></div>
      <div class="chart-wrap">
        <svg id="pareto-svg" role="img" aria-label="2D Pareto Frontier Chart" width="100%" height="100%" viewBox="0 0 680 270" preserveAspectRatio="xMidYMid meet"></svg>
      </div>
    </div>

    <div class="panel">
      <h2>2. Auditable Savings Receipt &amp; Provenance</h2>
      <div class="summary-box" id="provenance-box"></div>
      <div class="summary-box" id="policy-rationale-box"></div>
      <div class="summary-box" id="oracle-comparison-box"></div>
    </div>
  </div>

  <div class="panel">
    <div class="filter-bar">
      <h2 style="border:none; margin:0; padding:0;">3. Per-Request Routing Decisions &amp; Explanations</h2>
      <div class="filter-tabs">
        <button class="btn btn-active" data-filter="all" type="button" aria-pressed="true">All</button>
        <button class="btn" data-filter="frontier" type="button" aria-pressed="false">Frontier Required</button>
        <button class="btn" data-filter="regressions" type="button" aria-pressed="false">Regressions vs Baseline</button>
        <button class="btn" data-filter="unknown" type="button" aria-pressed="false">Excluded Unknown</button>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th scope="col">Req ID</th>
          <th scope="col">Category</th>
          <th scope="col">Selected Tier</th>
          <th scope="col">Baseline -&gt; Selected Cost</th>
          <th scope="col">Latency</th>
          <th scope="col">Eval Verdict</th>
          <th scope="col">Provenance</th>
          <th scope="col">Routing Explanation</th>
        </tr>
      </thead>
      <tbody id="decisions-tbody"></tbody>
    </table>
    <div class="empty-state" id="table-empty-state"></div>
  </div>
  </main>
</div>

<script id="pareto-data" type="application/json">{safe_json}</script>
<script id="receipt-md-data" type="application/json">{safe_md}</script>
<script>
(function() {{
  "use strict";
  const receipt = JSON.parse(document.getElementById("pareto-data").textContent);
  const rawMd = JSON.parse(document.getElementById("receipt-md-data").textContent);
  const policies = receipt.policy_results || [];
  const deployable = policies.filter(function(p) {{ return p.deployable; }});
  const frontier = deployable.filter(function(p) {{ return p.is_pareto_optimal; }});
  const oracle = policies.find(function(p) {{ return !p.deployable; }}) || null;
  const baseline = policies.find(function(p) {{ return p.policy_id === receipt.baseline_policy_id; }}) || deployable[deployable.length - 1];

  let currentFilter = "all";
  let currentPolicy = baseline;

  function selectPolicyForFloor(qMin) {{
    const pool = frontier.length > 0 ? frontier : deployable;
    const eligible = pool.filter(function(p) {{ return (p.pass_rate + 1e-9) >= qMin; }});
    if (eligible.length > 0) {{
      eligible.sort(function(a, b) {{
        const cDiff = parseFloat(a.total_cost_usd) - parseFloat(b.total_cost_usd);
        if (Math.abs(cDiff) > 1e-12) return cDiff;
        if (Math.abs(a.p95_latency_ms - b.p95_latency_ms) > 1e-9) return a.p95_latency_ms - b.p95_latency_ms;
        if (Math.abs(b.pass_rate - a.pass_rate) > 1e-9) return b.pass_rate - a.pass_rate;
        return a.policy_id.localeCompare(b.policy_id);
      }});
      return {{ feasible: true, policy: eligible[0] }};
    }}
    const fallbackPool = pool.slice().sort(function(a, b) {{
      if (Math.abs(b.pass_rate - a.pass_rate) > 1e-9) return b.pass_rate - a.pass_rate;
      const cDiff = parseFloat(a.total_cost_usd) - parseFloat(b.total_cost_usd);
      if (Math.abs(cDiff) > 1e-12) return cDiff;
      return a.p95_latency_ms - b.p95_latency_ms;
    }});
    return {{ feasible: false, policy: fallbackPool[0] }};
  }}

  function setLines(container, lines) {{
    while (container.firstChild) container.removeChild(container.firstChild);
    lines.forEach(function(text, idx) {{
      const div = document.createElement("div");
      if (idx === 0) div.style.fontWeight = "700";
      div.textContent = text;
      container.appendChild(div);
    }});
  }}

  function renderSvgChart(selectedPol, qMin) {{
    const svg = document.getElementById("pareto-svg");
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const ns = "http://www.w3.org/2000/svg";
    const padL = 52, padR = 175, padT = 24, padB = 34;
    const w = 680 - padL - padR;
    const h = 270 - padT - padB;

    const maxCost = Math.max.apply(null, policies.map(function(p) {{ return parseFloat(p.total_cost_usd); }}).concat([0.001]));
    function xOf(cost) {{ return padL + (cost / maxCost) * w; }}
    function yOf(rate) {{ return padT + (1.0 - rate) * h; }}

    // Axes
    const axis = document.createElementNS(ns, "polyline");
    axis.setAttribute("points", padL + "," + padT + " " + padL + "," + (padT + h) + " " + (padL + w) + "," + (padT + h));
    axis.setAttribute("fill", "none");
    axis.setAttribute("stroke", "#a1a1aa");
    axis.setAttribute("stroke-width", "1");
    svg.appendChild(axis);

    // Quality floor horizontal line
    const floorY = yOf(qMin);
    const floorLine = document.createElementNS(ns, "line");
    floorLine.setAttribute("x1", String(padL));
    floorLine.setAttribute("y1", String(floorY));
    floorLine.setAttribute("x2", String(padL + w));
    floorLine.setAttribute("y2", String(floorY));
    floorLine.setAttribute("stroke", "#52525b");
    floorLine.setAttribute("stroke-dasharray", "3,3");
    svg.appendChild(floorLine);

    const floorTxt = document.createElementNS(ns, "text");
    floorTxt.setAttribute("x", String(padL + 6));
    floorTxt.setAttribute("y", String(Math.max(14, floorY - 5)));
    floorTxt.setAttribute("font-size", "10");
    floorTxt.setAttribute("fill", "#52525b");
    floorTxt.textContent = "Quality Floor (" + Math.round(qMin * 100) + "%)";
    svg.appendChild(floorTxt);

    // Frontier polyline
    if (frontier.length > 1) {{
      const pts = frontier.map(function(p) {{
        return xOf(parseFloat(p.total_cost_usd)).toFixed(1) + "," + yOf(p.pass_rate).toFixed(1);
      }}).join(" ");
      const poly = document.createElementNS(ns, "polyline");
      poly.setAttribute("points", pts);
      poly.setAttribute("fill", "none");
      poly.setAttribute("stroke", "#18181b");
      poly.setAttribute("stroke-width", "2");
      svg.appendChild(poly);
    }}

    // Plot each policy with collision-free vertical label staggering
    const sortedForPlot = policies.slice().sort(function(a, b) {{
      if (Math.abs(b.pass_rate - a.pass_rate) > 1e-6) return b.pass_rate - a.pass_rate;
      return parseFloat(a.total_cost_usd) - parseFloat(b.total_cost_usd);
    }});
    const usedLabelYs = [];
    sortedForPlot.forEach(function(p) {{
      const cx = xOf(parseFloat(p.total_cost_usd));
      const cy = yOf(p.pass_rate);
      const isSel = p.policy_id === selectedPol.policy_id;
      const circle = document.createElementNS(ns, "circle");
      circle.setAttribute("cx", cx.toFixed(1));
      circle.setAttribute("cy", cy.toFixed(1));
      circle.setAttribute("r", isSel ? "6.5" : "4.5");
      circle.setAttribute("fill", isSel ? "#18181b" : (p.deployable ? "#ffffff" : "#e4e4e7"));
      circle.setAttribute("stroke", "#18181b");
      circle.setAttribute("stroke-width", isSel ? "2.5" : "1.5");
      if (!p.deployable) circle.setAttribute("stroke-dasharray", "2,2");
      svg.appendChild(circle);

      let labelY = Math.max(14, cy + 3);
      while (usedLabelYs.some(function(prevY) {{ return Math.abs(prevY - labelY) < 13; }})) {{
        labelY += 13;
      }}
      usedLabelYs.push(labelY);

      if (Math.abs(labelY - (cy + 3)) > 6) {{
        const leader = document.createElementNS(ns, "line");
        leader.setAttribute("x1", (cx + 5).toFixed(1));
        leader.setAttribute("y1", cy.toFixed(1));
        leader.setAttribute("x2", (cx + 10).toFixed(1));
        leader.setAttribute("y2", (labelY - 3).toFixed(1));
        leader.setAttribute("stroke", "#a1a1aa");
        leader.setAttribute("stroke-width", "0.8");
        svg.appendChild(leader);
      }}

      const label = document.createElementNS(ns, "text");
      label.setAttribute("x", (cx + 12).toFixed(1));
      label.setAttribute("y", labelY.toFixed(1));
      label.setAttribute("font-size", "10");
      label.setAttribute("font-family", "ui-monospace, monospace");
      label.setAttribute("font-weight", isSel ? "700" : "400");
      label.setAttribute("fill", isSel ? "#18181b" : "#3f3f46");
      const tag = !p.deployable ? " [ORACLE]" : (p.in_sample_fit ? " [grid]" : "");
      label.textContent = p.policy_id + " ($" + parseFloat(p.total_cost_usd).toFixed(4) + ", " + (p.pass_rate * 100).toFixed(1) + "%)" + tag;
      svg.appendChild(label);
    }});
  }}

  function renderTable(pol) {{
    const tbody = document.getElementById("decisions-tbody");
    const emptyEl = document.getElementById("table-empty-state");
    while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

    if (currentFilter === "unknown") {{
      emptyEl.style.display = "block";
      emptyEl.textContent = receipt.excluded_unknown_requests > 0
        ? receipt.excluded_unknown_requests + " request(s) had UNKNOWN provenance and were excluded from cross-policy savings math."
        : "0 requests have UNKNOWN provenance. All " + receipt.complete_case_requests + " workload requests have complete 3-tier evaluations.";
      return;
    }}

    const rows = (pol.decisions || []).filter(function(d) {{
      if (currentFilter === "frontier") return d.frontier_required;
      if (currentFilter === "regressions") return d.regressed_vs_baseline;
      return true;
    }});

    if (rows.length === 0) {{
      emptyEl.style.display = "block";
      emptyEl.textContent = "No requests match filter '" + currentFilter + "' under policy '" + pol.policy_id + "'.";
      return;
    }}
    emptyEl.style.display = "none";

    rows.forEach(function(d) {{
      const tr = document.createElement("tr");
      function addCell(text, cls) {{
        const td = document.createElement("td");
        if (cls) td.className = cls;
        td.textContent = text;
        tr.appendChild(td);
      }}
      addCell(d.request_id, "mono");
      addCell(d.category, "");
      addCell(d.selected_tier.toUpperCase() + (d.attempted_tiers.length > 1 ? " (" + d.attempted_tiers.join("->") + ")" : ""), "mono");
      addCell("$" + parseFloat(d.baseline_cost_usd).toFixed(5) + " -> $" + parseFloat(d.cost_usd).toFixed(5), "mono");
      addCell(d.latency_ms.toFixed(0) + " ms", "mono");
      addCell(d.passed ? "PASS (" + d.score.toFixed(2) + ")" : "FAIL (" + d.score.toFixed(2) + ")", d.passed ? "badge-pass mono" : "badge-fail mono");
      const provSummary = (d.provenance.tokens_provenance === "measured" && d.provenance.verdict_provenance === "measured") ? "MEASURED" : "MODELED";
      addCell(provSummary + " (cost:" + d.provenance.cost_provenance + ")", "mono");
      addCell(d.explanation, "");
      tbody.appendChild(tr);
    }});
  }}

  function updateView() {{
    const sliderEl = document.getElementById("quality-slider");
    const qMin = parseInt(sliderEl.value, 10) / 100.0;
    document.getElementById("quality-slider-val").textContent = Math.round(qMin * 100) + "%";

    const sel = selectPolicyForFloor(qMin);
    const pol = sel.policy;
    currentPolicy = pol;

    const banner = document.getElementById("infeasible-banner");
    if (!sel.feasible) {{
      banner.style.display = "block";
      banner.textContent = "No deployable policy reaches the " + Math.round(qMin * 100) + "% quality floor. Showing closest feasible policy: " + pol.display_name + " (" + (pol.pass_rate * 100).toFixed(1) + "%).";
    }} else {{
      banner.style.display = "none";
    }}

    const pb = pol.provenance_breakdown;
    document.getElementById("badge-measured").textContent = "MEASURED: " + pb.measured_requests;
    document.getElementById("badge-modeled").textContent = "MODELED: " + pb.modeled_requests;
    document.getElementById("badge-unknown").textContent = "UNKNOWN EXCLUDED: " + pb.unknown_excluded_requests;

    document.getElementById("kpi-base-cost").textContent = "$" + parseFloat(baseline.total_cost_usd).toFixed(4);
    document.getElementById("kpi-base-sub").textContent = "Pass: " + (baseline.pass_rate * 100).toFixed(1) + "% (" + baseline.passed_count + "/" + baseline.evaluated_requests + ") | P95: " + baseline.p95_latency_ms.toFixed(0) + " ms";

    document.getElementById("kpi-sel-label").textContent = "Selected: " + pol.policy_id + (pol.in_sample_fit ? " [in-sample]" : "");
    document.getElementById("kpi-sel-cost").textContent = "$" + parseFloat(pol.total_cost_usd).toFixed(4);
    document.getElementById("kpi-sel-sub").textContent = "Pass: " + (pol.pass_rate * 100).toFixed(1) + "% (" + pol.passed_count + "/" + pol.evaluated_requests + ") | P95: " + pol.p95_latency_ms.toFixed(0) + " ms";

    const goodput = pol.cost_per_successful_outcome_usd !== null ? "$" + parseFloat(pol.cost_per_successful_outcome_usd).toFixed(5) : "∞";
    const baseGoodput = baseline.cost_per_successful_outcome_usd !== null ? "$" + parseFloat(baseline.cost_per_successful_outcome_usd).toFixed(5) : "∞";
    document.getElementById("kpi-delta-cost").textContent = (pol.cost_savings_vs_baseline_pct >= 0 ? "-" : "+") + Math.abs(pol.cost_savings_vs_baseline_pct).toFixed(1) + "% cost";
    document.getElementById("kpi-delta-sub").textContent = "Cost/pass: " + goodput + " (vs " + baseGoodput + ")";

    const tc = pol.tier_counts || {{}};
    const cCnt = tc.compact || 0, mCnt = tc.mid || 0, fCnt = tc.frontier || 0;
    document.getElementById("kpi-tier-mix").textContent = cCnt + " / " + mCnt + " / " + fCnt;
    document.getElementById("kpi-tier-sub").textContent = "Compact: " + cCnt + " | Mid: " + mCnt + " | Frontier: " + fCnt;

    setLines(document.getElementById("provenance-box"), [
      "Provenance Breakdown (" + pol.policy_id + ")",
      "- Measured (live API + eval): " + pb.measured_requests + " reqs ($" + parseFloat(pb.measured_cost_usd).toFixed(4) + ")",
      "- Modeled (fixture / catalog price): " + pb.modeled_requests + " reqs ($" + parseFloat(pb.modeled_cost_usd).toFixed(4) + ")",
      "- Unknown (excluded complete-case): " + pb.unknown_excluded_requests + " reqs (burned: $" + parseFloat(pb.unknown_burned_cost_usd).toFixed(4) + ")"
    ]);

    setLines(document.getElementById("policy-rationale-box"), [
      "Why This Policy Won at q_min=" + Math.round(qMin * 100) + "%",
      "- Pass rate: " + (pol.pass_rate * 100).toFixed(1) + "% | Goodput: " + goodput + "/success",
      "- Savings vs All-Frontier: $" + parseFloat(pol.cost_savings_vs_baseline_usd).toFixed(4) + " (" + pol.cost_savings_vs_baseline_pct.toFixed(1) + "%)",
      pol.in_sample_fit ? "- Note: threshold parameters fit in-sample on this workload." : "- Zero in-sample parameter fitting (deployable rule)."
    ]);

    if (oracle) {{
      setLines(document.getElementById("oracle-comparison-box"), [
        "Offline Hindsight Oracle Benchmark (non-deployable)",
        "- Policy: " + oracle.policy_id + " | Cost: $" + parseFloat(oracle.total_cost_usd).toFixed(4) + " | Pass: " + (oracle.pass_rate * 100).toFixed(1) + "%",
        "- Theoretical remaining gap vs selected policy: $" + Math.max(0, parseFloat(pol.total_cost_usd) - parseFloat(oracle.total_cost_usd)).toFixed(4)
      ]);
    }}

    renderSvgChart(pol, qMin);
    renderTable(pol);
  }}

  document.getElementById("quality-slider").addEventListener("input", updateView);

  Array.prototype.forEach.call(document.querySelectorAll("[data-filter]"), function(btn) {{
    btn.addEventListener("click", function() {{
      Array.prototype.forEach.call(document.querySelectorAll("[data-filter]"), function(b) {{
        b.classList.remove("btn-active");
        b.setAttribute("aria-pressed", "false");
      }});
      btn.classList.add("btn-active");
      btn.setAttribute("aria-pressed", "true");
      currentFilter = btn.getAttribute("data-filter") || "all";
      renderTable(currentPolicy);
    }});
  }});

  function triggerDownload(filename, content, mime) {{
    const blob = new Blob([content], {{ type: mime }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }}

  document.getElementById("btn-dl-json").addEventListener("click", function() {{
    triggerDownload("savings-receipt.json", JSON.stringify(receipt, null, 2), "application/json");
  }});
  document.getElementById("btn-dl-md").addEventListener("click", function() {{
    triggerDownload("savings-receipt.md", rawMd, "text/markdown");
  }});

  updateView();
}})();
</script>
</body>
</html>
"""
