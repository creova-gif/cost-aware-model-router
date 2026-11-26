from __future__ import annotations

import json
import re
from typing import Any

from cost_aware_model_router.dashboard import render_live_finops_dashboard_html
from cost_aware_model_router.report import escape_json_for_script_tag


def _extract_snapshot(html_output: str) -> dict[str, Any]:
    match = re.search(
        r'<script id="finops-snapshot" type="application/json">(.*?)</script>',
        html_output,
        re.DOTALL,
    )
    assert match is not None
    parsed = json.loads(match.group(1))
    assert isinstance(parsed, dict)
    return parsed


def test_dashboard_embeds_default_disabled_circuits_snapshot() -> None:
    html_output = render_live_finops_dashboard_html()
    snapshot = _extract_snapshot(html_output)

    assert snapshot["circuits"] == {"enabled": False, "circuits": {}}
    assert "Circuit Breaker Health" in html_output
    assert "Circuit breaker disabled" in html_output
    assert "/v1/finops/circuits" in html_output


def test_dashboard_embeds_enabled_circuits_states() -> None:
    circuits_payload: dict[str, Any] = {
        "enabled": True,
        "circuits": {
            "compact": "closed",
            "mid": "half_open",
            "frontier": "open",
        },
    }
    html_output = render_live_finops_dashboard_html(circuits=circuits_payload)
    snapshot = _extract_snapshot(html_output)

    assert snapshot["circuits"] == circuits_payload
    assert snapshot["circuits"]["enabled"] is True
    assert snapshot["circuits"]["circuits"]["compact"] == "closed"
    assert snapshot["circuits"]["circuits"]["mid"] == "half_open"
    assert snapshot["circuits"]["circuits"]["frontier"] == "open"


def test_escape_json_for_script_tag_neutralizes_breakout_in_circuits() -> None:
    breakout_payload = "</script><script>alert('xss')</script>"
    circuits_payload: dict[str, Any] = {
        "enabled": True,
        "circuits": {
            breakout_payload: "open",
            "compact": "closed</script>",
        },
    }

    escaped = escape_json_for_script_tag(json.dumps(circuits_payload, separators=(",", ":")))
    assert "</script>" not in escaped
    assert "<script>" not in escaped

    html_output = render_live_finops_dashboard_html(circuits=circuits_payload)
    assert breakout_payload not in html_output
    assert "closed</script>" not in html_output

    snapshot = _extract_snapshot(html_output)
    assert snapshot["circuits"]["circuits"][breakout_payload] == "open"
    assert snapshot["circuits"]["circuits"]["compact"] == "closed</script>"
