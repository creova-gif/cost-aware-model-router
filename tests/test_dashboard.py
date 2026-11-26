from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from http.server import ThreadingHTTPServer

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.dashboard import render_live_finops_dashboard_html
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import PolicySpec
from cost_aware_model_router.server import create_router_http_server
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy


def _policy_by_id(policy_id: str) -> PolicySpec:
    for policy in default_policy_suite():
        if policy.policy_id == policy_id:
            return policy
    raise KeyError(policy_id)


@contextmanager
def _running_server(gateway: OpenAIRouterGateway) -> Iterator[tuple[ThreadingHTTPServer, str]]:
    server = create_router_http_server(gateway, host="127.0.0.1", port=0)
    raw_host, port = server.server_address[:2]
    host = raw_host.decode("utf-8") if isinstance(raw_host, bytes) else raw_host
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_dashboard_html_is_self_contained_zero_cdn_and_xss_safe() -> None:
    malicious_org = "</script><script>alert('xss')</script>"
    economics = {
        "total_requests": 2,
        "passed_requests": 2,
        "failed_requests": 0,
        "cache_hits": 1,
        "total_cost_usd": "0.00120000",
        "measured_cost_usd": "0.00120000",
        "modeled_cost_usd": "0.00000000",
        "unknown_cost_usd": "0.00000000",
        "cost_per_request_usd": "0.00060000",
        "cost_per_successful_outcome_usd": "0.00060000",
        "spend_by_org": {malicious_org: "0.00120000"},
        "spend_by_agent": {f"{malicious_org}/bot": "0.00120000"},
        "spend_by_category": {"json_extraction": "0.00120000"},
        "spend_by_tier": {"compact": "0.00120000", "mid": "0.00000000", "frontier": "0.00000000"},
    }
    shadow_summary = {
        "total_shadow_requests": 1,
        "downgrade_count": 1,
        "primary_total_cost_usd": "0.00120000",
        "shadow_total_cost_usd": "0.00020000",
        "shadow_cost_delta_usd": "-0.00100000",
        "records": [
            {
                "request_id": "req-xss",
                "org_id": malicious_org,
                "agent_id": "bot",
                "primary_policy_id": "all-frontier",
                "primary_tier": "frontier",
                "primary_cost_usd": "0.00120000",
                "primary_latency_ms": 120.0,
                "shadow_policy_id": "threshold-balanced",
                "shadow_tier": "compact",
                "shadow_cost_usd": "0.00020000",
                "shadow_cost_delta_usd": "-0.00100000",
                "would_downgrade": True,
                "explanation": "Shadow </script> test",
            }
        ],
    }
    recent_spans = [
        {
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "name": "gen_ai.router.completion",
            "start_time_unix_nano": 1,
            "end_time_unix_nano": 2,
            "attributes": {
                "gen_ai.system": "openai",
                "gen_ai.request.model": "gpt-4o-mini",
                "gen_ai.usage.input_tokens": 20,
                "gen_ai.usage.output_tokens": 10,
                "cost_router.org_id": malicious_org,
                "cost_router.agent_id": "bot",
                "cost_router.policy_id": "threshold-balanced",
                "cost_router.selected_tier": "compact",
                "cost_router.cost_usd": "0.00120000",
                "cost_router.cost_provenance": "modeled",
                "cost_router.shadow_mode": True,
            },
        }
    ]

    html_output = render_live_finops_dashboard_html(
        economics=economics,
        shadow_summary=shadow_summary,
        recent_spans=recent_spans,
        title="FinOps <script>alert(1)</script>",
    )

    assert "<!DOCTYPE html>" in html_output
    assert "innerHTML" not in html_output
    assert "outerHTML" not in html_output
    assert "insertAdjacentHTML" not in html_output
    assert "textContent" in html_output
    assert "http://" not in html_output
    assert "https://" not in html_output
    assert "/v1/finops/economics" in html_output
    assert "/v1/finops/shadow-summary" in html_output
    assert malicious_org not in html_output
    assert "FinOps <script>alert(1)</script>" not in html_output

    match = re.search(
        r'<script id="finops-snapshot" type="application/json">(.*?)</script>',
        html_output,
        re.DOTALL,
    )
    assert match is not None
    parsed = json.loads(match.group(1))
    assert parsed["economics"]["total_requests"] == 2
    assert malicious_org in parsed["economics"]["spend_by_org"]
    assert parsed["shadow_summary"]["downgrade_count"] == 1
    assert parsed["recent_spans"][0]["attributes"]["gen_ai.system"] == "openai"


def test_server_get_dashboard_returns_live_html_with_and_without_thermostat() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_acme",
                agent_id="invoice_bot",
                hourly_budget_usd=Decimal("5.00000000"),
            )
        ]
    )
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("all-frontier"),
        thermostat=thermostat,
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(base_url=base_url, timeout=5.0) as client:
        chat_resp = client.post(
            "/v1/chat/completions",
            headers={
                "x-org-id": "org_acme",
                "x-agent-id": "invoice_bot",
                "x-policy-id": "threshold-balanced",
                "x-shadow-candidate-policy-id": "threshold-aggressive",
            },
            json={
                "model": "auto",
                "request_id": "req-001",
                "category": "json_extraction",
                "messages": [{"role": "user", "content": "Extract customer_id and invoice_total as JSON."}],
            },
        )
        assert chat_resp.status_code == 200

        dash_resp = client.get("/dashboard")
        assert dash_resp.status_code == 200
        assert dash_resp.headers["content-type"] == "text/html; charset=utf-8"
        assert "<!DOCTYPE html>" in dash_resp.text
        assert "innerHTML" not in dash_resp.text
        assert "http://" not in dash_resp.text
        assert "https://" not in dash_resp.text

        match = re.search(
            r'<script id="finops-snapshot" type="application/json">(.*?)</script>',
            dash_resp.text,
            re.DOTALL,
        )
        assert match is not None
        snapshot = json.loads(match.group(1))
        assert snapshot["economics"]["total_requests"] == 1
        assert "org_acme" in snapshot["economics"]["spend_by_org"]
        assert snapshot["shadow_summary"]["total_shadow_requests"] == 1
        assert len(snapshot["recent_spans"]) == 1
        assert snapshot["recent_spans"][0]["name"] == "gen_ai.router.completion"

    no_thermostat_gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )
    with (
        _running_server(no_thermostat_gateway) as (_, base_url),
        httpx.Client(base_url=base_url, timeout=5.0) as client,
    ):
        dash_resp = client.get("/dashboard")
        assert dash_resp.status_code == 200
        assert dash_resp.headers["content-type"] == "text/html; charset=utf-8"
        match = re.search(
            r'<script id="finops-snapshot" type="application/json">(.*?)</script>',
            dash_resp.text,
            re.DOTALL,
        )
        assert match is not None
        snapshot = json.loads(match.group(1))
        assert snapshot["economics"] is None
        assert snapshot["shadow_summary"]["total_shadow_requests"] == 0
