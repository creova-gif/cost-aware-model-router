from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
)
from cost_aware_model_router.cli import run_pipeline
from cost_aware_model_router.dashboard import render_live_finops_dashboard_html
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    OpenAIRouterGateway,
)
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.receipt import render_receipt_markdown
from cost_aware_model_router.report import render_pareto_explorer_html
from cost_aware_model_router.schemas import ModelTier, ProvenanceTag, TaskCategory
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy


def test_thermostat_routing_overhead_under_budget() -> None:
    orgs = [f"org_{idx}" for idx in range(5)]
    policies = [
        BudgetPolicy(
            org_id=org_id,
            agent_id="agent_perf",
            hourly_budget_usd=Decimal("100.00000000"),
            soft_downgrade_ratio=0.80,
            hard_cap_ratio=1.00,
            max_loop_repeats=5,
            min_quality_tier=ModelTier.COMPACT,
        )
        for org_id in orgs
    ]
    thermostat = AgentBudgetThermostat(policies=policies)

    start = time.perf_counter()
    for i in range(500):
        org_id = orgs[i % len(orgs)]
        request_id = f"req-perf-{i:04d}"
        prompt = f"Evaluate deterministic routing request #{i} for {org_id}"
        decision = thermostat.evaluate_request(
            request_id=request_id,
            org_id=org_id,
            agent_id="agent_perf",
            prompt=prompt,
            requested_tier=ModelTier.MID,
            estimated_cost_usd=Decimal("0.00100000"),
        )
        assert decision.allowed is True
        thermostat.record_outcome(
            request_id=request_id,
            org_id=org_id,
            agent_id="agent_perf",
            category=TaskCategory.JSON_EXTRACTION,
            tier=decision.approved_tier or ModelTier.MID,
            provenance=ProvenanceTag.MEASURED,
            passed=True,
            cost_usd=Decimal("0.00100000"),
            input_tokens=120,
            output_tokens=45,
            prompt=prompt,
            response_text=f'{{"id": {i}}}',
        )
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, f"Thermostat 500-call loop took {elapsed:.3f}s (budget < 2.0s)"
    assert thermostat.summarize_economics().total_requests == 500


def test_gateway_sqlite_end_to_end_throughput(tmp_path: Path) -> None:
    db_path = tmp_path / "perf-gateway.db"
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_perf",
                agent_id="agent_gateway",
                hourly_budget_usd=Decimal("100.00000000"),
                soft_downgrade_ratio=0.80,
                hard_cap_ratio=1.00,
                max_loop_repeats=5,
                min_quality_tier=ModelTier.COMPACT,
            )
        ]
    )
    primary_policy = next(p for p in default_policy_suite() if p.policy_id == "threshold-balanced")

    async def _run_batch(gateway: OpenAIRouterGateway, count: int) -> None:
        for i in range(count):
            req = ChatCompletionRequest(
                request_id=f"req-{i:04d}",
                category=TaskCategory.CODE_SQL,
                messages=[
                    ChatMessage(
                        role="user",
                        content=f"SELECT id, amount FROM invoices WHERE batch_id = {i};",
                    )
                ],
                x_org_id="org_perf",
                x_agent_id="agent_gateway",
            )
            resp = await gateway.handle_chat_completion(req)
            assert resp.choices[0].message.content

    with SQLiteEconomicsStore(db_path) as store:
        gateway = OpenAIRouterGateway(
            adapter=DeterministicFixtureAdapter(),
            catalog=load_bundled_catalog(),
            primary_policy=primary_policy,
            thermostat=thermostat,
            store=store,
        )

        start = time.perf_counter()
        asyncio.run(_run_batch(gateway, 100))
        elapsed = time.perf_counter() - start

        assert elapsed < 5.0, f"Gateway+SQLite 100-request batch took {elapsed:.3f}s (budget < 5.0s)"
        summary = store.summarize_economics()
        assert summary.total_requests == 100
        assert gateway.summarize_economics()["total_requests"] == 100


def test_html_report_and_dashboard_render_latency(tmp_path: Path) -> None:
    spans = [
        {
            "trace_id": f"{i:032x}",
            "span_id": f"{i:016x}",
            "name": "llm.router.completion",
            "start_time_unix_nano": 1_700_000_000_000_000_000 + i * 1_000_000,
            "end_time_unix_nano": 1_700_000_000_000_000_000 + i * 1_000_000 + 500_000,
            "attributes": {
                "router.org_id": f"org_{i % 5}",
                "router.agent_id": "agent_perf",
                "router.selected_tier": "compact",
                "router.cost_usd": "0.00050000",
                "router.passed": "true",
            },
        }
        for i in range(50)
    ]
    economics = {
        "total_requests": 50,
        "passed_requests": 50,
        "failed_requests": 0,
        "cache_hits": 5,
        "total_cost_usd": "0.02500000",
        "measured_cost_usd": "0.02500000",
        "modeled_cost_usd": "0.00000000",
        "unknown_cost_usd": "0.00000000",
        "cost_per_request_usd": "0.00050000",
        "cost_per_successful_outcome_usd": "0.00050000",
        "spend_by_org": {f"org_{i}": "0.00500000" for i in range(5)},
        "spend_by_agent": {"org_0/agent_perf": "0.02500000"},
        "spend_by_category": {"json_extraction": "0.02500000"},
        "spend_by_tier": {"compact": "0.02500000", "mid": "0.00000000", "frontier": "0.00000000"},
    }

    dash_start = time.perf_counter()
    dashboard_html = render_live_finops_dashboard_html(
        economics=economics,
        recent_spans=spans,
    )
    dash_elapsed = time.perf_counter() - dash_start
    assert dash_elapsed < 1.0, f"Dashboard render took {dash_elapsed:.3f}s (budget < 1.0s)"
    assert "<!DOCTYPE html>" in dashboard_html

    receipt = asyncio.run(
        run_pipeline(
            workload_name="bundled-perf",
            workload=generate_bundled_workload(),
            catalog=load_bundled_catalog(),
            adapter=DeterministicFixtureAdapter(),
            out_dir=tmp_path / "pipeline-out",
        )
    )
    md_text = render_receipt_markdown(receipt)

    report_start = time.perf_counter()
    report_html = render_pareto_explorer_html(receipt, md_text)
    report_elapsed = time.perf_counter() - report_start
    assert report_elapsed < 1.0, f"Pareto report render took {report_elapsed:.3f}s (budget < 1.0s)"
    assert "<!DOCTYPE html>" in report_html
