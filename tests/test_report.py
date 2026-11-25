from __future__ import annotations

import asyncio
import json
import re

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
)
from cost_aware_model_router.policies import (
    build_evaluation_matrix,
    default_policy_suite,
    evaluate_policy_suite,
)
from cost_aware_model_router.receipt import build_savings_receipt, render_receipt_markdown
from cost_aware_model_router.report import (
    escape_json_for_script_tag,
    render_pareto_explorer_html,
)


def test_escape_json_for_script_tag_prevents_xss_breakout() -> None:
    payload = '{"evil": "</script><img src=x onerror=alert(1)> <!-- -->"}'
    escaped = escape_json_for_script_tag(payload)
    assert "</script>" not in escaped
    assert "<img" not in escaped
    assert "<!--" not in escaped
    assert "\\u003c/script\\u003e" in escaped


def test_render_pareto_explorer_html_self_contained_and_safe() -> None:
    workload = generate_bundled_workload()[:6]
    catalog = load_bundled_catalog()
    traces = asyncio.run(DeterministicFixtureAdapter().execute_workload(workload, catalog))
    eval_matrix, matrix_map, complete_ids, excluded_ids, unknown_burned = build_evaluation_matrix(
        workload, traces
    )
    results, frontier_ids = evaluate_policy_suite(
        workload, default_policy_suite(), matrix_map, complete_ids, excluded_ids, unknown_burned
    )
    receipt = build_savings_receipt(
        workload_name="xss-test-</script><script>alert(1)</script>.jsonl",
        workload=workload,
        catalog=catalog,
        evaluation_matrix=eval_matrix,
        policy_results=results,
        pareto_optimal_ids=frontier_ids,
        complete_case_count=len(complete_ids),
        excluded_unknown_count=len(excluded_ids),
    )
    md = render_receipt_markdown(receipt)
    html_out = render_pareto_explorer_html(receipt, md)

    # Zero unescaped <script> injection; uses createElementNS and textContent
    assert "<script>alert(1)</script>" not in html_out
    assert "document.createElementNS" in html_out
    assert "innerHTML" not in html_out
    # Zero external script/stylesheet links
    assert "https://cdn" not in html_out

    # Both embedded JSON script blocks must parse as valid JSON
    m_pareto = re.search(
        r'<script id="pareto-data" type="application/json">(.*?)</script>', html_out, re.DOTALL
    )
    m_md = re.search(
        r'<script id="receipt-md-data" type="application/json">(.*?)</script>', html_out, re.DOTALL
    )
    assert m_pareto is not None and m_md is not None
    parsed_pareto = json.loads(m_pareto.group(1))
    parsed_md = json.loads(m_md.group(1))
    assert parsed_pareto["complete_case_requests"] == 6
    assert "# Auditable Savings Receipt" in parsed_md
