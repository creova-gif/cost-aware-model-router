from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    load_bundled_catalog,
)
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.metrics import (
    escape_prometheus_label_value,
    render_prometheus_metrics,
)
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import (
    ModelTier,
    PolicySpec,
    ProvenanceTag,
    TaskCategory,
)
from cost_aware_model_router.telemetry import InMemorySpanExporter, OTelSpanRecord
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy


def _policy_by_id(policy_id: str) -> PolicySpec:
    for policy in default_policy_suite():
        if policy.policy_id == policy_id:
            return policy
    raise KeyError(policy_id)


def test_escape_prometheus_label_value_prevents_injection() -> None:
    malicious = 'org_evil"}\ncost_router_requests_total{org_id="fake\\path'
    escaped = escape_prometheus_label_value(malicious)
    assert escaped == 'org_evil\\"}\\ncost_router_requests_total{org_id=\\"fake\\\\path'
    assert "\n" not in escaped


def test_render_prometheus_metrics_empty_spans_without_summary() -> None:
    output = render_prometheus_metrics([])
    assert "# HELP cost_router_requests_total" in output
    assert "# TYPE cost_router_requests_total counter" in output
    assert "# HELP cost_router_spend_usd_total" in output
    assert "# TYPE cost_router_spend_usd_total counter" in output
    assert "# HELP cost_router_latency_ms" in output
    assert "# TYPE cost_router_latency_ms summary" in output
    assert "cost_router_cache_hits_total" not in output
    assert "cost_router_goodput_usd" not in output
    assert output.endswith("\n")


def test_render_prometheus_metrics_aggregates_spans_and_escapes_labels() -> None:
    spans = [
        OTelSpanRecord.create_router_span(
            system="openai",
            request_model="gpt-4o-mini",
            input_tokens=100,
            output_tokens=25,
            org_id='org_"acme"\n\\1',
            agent_id="bot-1",
            policy_id="threshold-balanced",
            selected_tier=ModelTier.COMPACT,
            cost_usd=Decimal("0.00150000"),
            cost_provenance=ProvenanceTag.MEASURED,
            shadow_mode=False,
            start_time_unix_nano=1_000_000_000,
            end_time_unix_nano=1_025_000_000,
        ),
        OTelSpanRecord.create_router_span(
            system="openai",
            request_model="gpt-4o-mini",
            input_tokens=200,
            output_tokens=50,
            org_id='org_"acme"\n\\1',
            agent_id="bot-1",
            policy_id="threshold-balanced",
            selected_tier=ModelTier.COMPACT,
            cost_usd=Decimal("0.00250000"),
            cost_provenance=ProvenanceTag.MEASURED,
            shadow_mode=False,
            start_time_unix_nano=2_000_000_000,
            end_time_unix_nano=2_015_000_000,
        ),
        OTelSpanRecord.create_router_span(
            system="anthropic",
            request_model="claude-3-5-sonnet",
            input_tokens=300,
            output_tokens=100,
            org_id="org_beta",
            agent_id="bot-2",
            policy_id="threshold-balanced",
            selected_tier=ModelTier.FRONTIER,
            cost_usd=Decimal("0.01000000"),
            cost_provenance=ProvenanceTag.MODELED,
            shadow_mode=True,
            start_time_unix_nano=3_000_000_000,
            end_time_unix_nano=3_100_000_000,
        ),
    ]

    output = render_prometheus_metrics(spans)

    escaped_org = 'org_\\"acme\\"\\n\\\\1'
    assert (
        f'cost_router_requests_total{{org_id="{escaped_org}",tier="compact",'
        f'provenance="measured",shadow_mode="false"}} 2'
    ) in output
    assert (
        'cost_router_requests_total{org_id="org_beta",tier="frontier",'
        'provenance="modeled",shadow_mode="true"} 1'
    ) in output
    assert (
        f'cost_router_spend_usd_total{{org_id="{escaped_org}",tier="compact",'
        'provenance="measured"} 0.00400000'
    ) in output
    assert (
        'cost_router_spend_usd_total{org_id="org_beta",tier="frontier",'
        'provenance="modeled"} 0.01000000'
    ) in output
    assert f'cost_router_latency_ms_sum{{org_id="{escaped_org}",tier="compact"}} 40.000000' in output
    assert f'cost_router_latency_ms_count{{org_id="{escaped_org}",tier="compact"}} 2' in output
    assert 'cost_router_latency_ms_sum{org_id="org_beta",tier="frontier"} 100.000000' in output
    assert 'cost_router_latency_ms_count{org_id="org_beta",tier="frontier"} 1' in output


def test_render_prometheus_metrics_with_thermostat_summary() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[BudgetPolicy(org_id="org_acme", hourly_budget_usd=Decimal("5.00"))]
    )
    thermostat.record_outcome(
        request_id="req-1",
        org_id="org_acme",
        agent_id="bot",
        category=TaskCategory.CLASSIFICATION,
        tier=ModelTier.COMPACT,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("0.00200000"),
        input_tokens=50,
        output_tokens=10,
        cached=False,
    )
    thermostat.record_outcome(
        request_id="req-2",
        org_id="org_acme",
        agent_id="bot",
        category=TaskCategory.CLASSIFICATION,
        tier=ModelTier.COMPACT,
        provenance=ProvenanceTag.MODELED,
        passed=True,
        cost_usd=Decimal("0.00000000"),
        input_tokens=0,
        output_tokens=0,
        cached=True,
    )
    summary = thermostat.summarize_economics()
    output = render_prometheus_metrics([], summary=summary)

    assert "# TYPE cost_router_cache_hits_total counter" in output
    assert "cost_router_cache_hits_total 1" in output
    assert "# TYPE cost_router_goodput_usd gauge" in output
    assert "cost_router_goodput_usd 0.00100000" in output


def test_render_prometheus_metrics_omits_goodput_when_none() -> None:
    thermostat = AgentBudgetThermostat()
    summary = thermostat.summarize_economics()
    assert summary.cost_per_successful_outcome_usd is None

    output = render_prometheus_metrics([], summary=summary)
    assert "cost_router_cache_hits_total 0" in output
    assert "cost_router_goodput_usd" not in output


def test_gateway_http_metrics_endpoint_without_thermostat() -> None:
    exporter = InMemorySpanExporter()
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        span_exporter=exporter,
    )

    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=gateway.as_httpx_transport(),
            base_url="http://router.local",
        ) as client:
            post_resp = await client.post(
                "/v1/chat/completions",
                headers={"X-Org-Id": "org_metrics", "X-Agent-Id": "agent_1"},
                json={
                    "model": "auto",
                    "category": "classification",
                    "messages": [{"role": "user", "content": "Classify this ticket."}],
                },
            )
            assert post_resp.status_code == 200
            return await client.get("/metrics")

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    text = resp.text
    assert 'cost_router_requests_total{org_id="org_metrics",tier="compact"' in text
    assert "cost_router_cache_hits_total" not in text
    assert "cost_router_goodput_usd" not in text


def test_gateway_http_metrics_endpoint_with_thermostat_and_cache_hit() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[BudgetPolicy(org_id="org_cached", hourly_budget_usd=Decimal("10.00"))]
    )
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        thermostat=thermostat,
    )

    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=gateway.as_httpx_transport(),
            base_url="http://router.local",
        ) as client:
            payload = {
                "model": "auto",
                "category": "classification",
                "messages": [{"role": "user", "content": "Classify priority: urgent outage"}],
            }
            r1 = await client.post(
                "/v1/chat/completions",
                headers={"X-Org-Id": "org_cached", "X-Agent-Id": "agent_1"},
                json=payload,
            )
            assert r1.status_code == 200
            r2 = await client.post(
                "/v1/chat/completions",
                headers={"X-Org-Id": "org_cached", "X-Agent-Id": "agent_2"},
                json=payload,
            )
            assert r2.status_code == 200
            return await client.get("/metrics")

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    text = resp.text
    assert 'cost_router_requests_total{org_id="org_cached",tier="compact"' in text
    assert "cost_router_cache_hits_total 1" in text
    assert "cost_router_goodput_usd " in text
