from __future__ import annotations

import asyncio
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
from cost_aware_model_router.report import render_pareto_explorer_html


def _build_fixture_report_html() -> str:
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
        workload_name="a11y-test-workload",
        workload=workload,
        catalog=catalog,
        evaluation_matrix=eval_matrix,
        policy_results=results,
        pareto_optimal_ids=frontier_ids,
        complete_case_count=len(complete_ids),
        excluded_unknown_count=len(excluded_ids),
    )
    md = render_receipt_markdown(receipt)
    return render_pareto_explorer_html(receipt, md)


def test_pareto_report_css_selector_fix_and_a11y_styles() -> None:
    html_out = _build_fixture_report_html()

    assert ".btn-active:hover" in html_out
    assert ". btn-active" not in html_out
    assert ".skip-link" in html_out
    assert ":focus-visible" in html_out
    assert "outline: 2px solid #2563eb;" in html_out
    assert "outline-offset: 2px;" in html_out
    assert "@media (prefers-reduced-motion: reduce)" in html_out


def test_pareto_report_wcag_landmarks_and_aria_attributes() -> None:
    html_out = _build_fixture_report_html()

    assert '<a class="skip-link" href="#main-content">Skip to main content</a>' in html_out
    assert '<header class="panel header-bar">' in html_out
    assert "</header>" in html_out
    assert '<main id="main-content">' in html_out
    assert "</main>" in html_out

    assert 'aria-label="Minimum quality floor"' in html_out
    assert 'aria-live="polite"' in html_out

    svg_match = re.search(r"<svg\b[^>]*>", html_out)
    assert svg_match is not None
    assert 'role="img"' in svg_match.group(0)
    assert 'aria-label="2D Pareto Frontier Chart"' in svg_match.group(0)

    th_tags = re.findall(r"<th\b[^>]*>", html_out)
    assert len(th_tags) == 8
    assert all('scope="col"' in tag for tag in th_tags)

    filter_buttons = re.findall(r'<button\b[^>]*data-filter="[^"]*"[^>]*>', html_out)
    assert len(filter_buttons) == 4
    assert all("aria-pressed=" in btn for btn in filter_buttons)
    assert 'setAttribute("aria-pressed"' in html_out
