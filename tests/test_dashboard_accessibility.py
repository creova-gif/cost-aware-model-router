from __future__ import annotations

import re

from cost_aware_model_router.dashboard import render_live_finops_dashboard_html


def test_dashboard_html_has_wcag_landmarks_and_skip_link() -> None:
    html_doc = render_live_finops_dashboard_html()

    assert '<a class="skip-link" href="#main-content">Skip to main content</a>' in html_doc
    assert '<header class="panel header-bar">' in html_doc
    assert "</header>" in html_doc
    assert '<main id="main-content">' in html_doc
    assert "</main>" in html_doc


def test_dashboard_html_has_focus_and_reduced_motion_styles() -> None:
    html_doc = render_live_finops_dashboard_html()

    assert ".skip-link" in html_doc
    assert ".skip-link:focus" in html_doc
    assert ":focus-visible" in html_doc
    assert "outline: 2px solid #2563eb; outline-offset: 2px;" in html_doc
    assert "@media (prefers-reduced-motion: reduce)" in html_doc


def test_dashboard_html_has_live_regions() -> None:
    html_doc = render_live_finops_dashboard_html()

    assert 'id="refresh-status" aria-live="polite"' in html_doc
    assert 'id="status-banner" role="status" aria-live="polite"' in html_doc


def test_dashboard_html_every_th_has_scope_col() -> None:
    html_doc = render_live_finops_dashboard_html()

    th_tags = re.findall(r"<th\b[^>]*>", html_doc)
    assert len(th_tags) == 24
    for tag in th_tags:
        assert 'scope="col"' in tag
