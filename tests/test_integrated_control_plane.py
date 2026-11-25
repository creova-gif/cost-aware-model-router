from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cost_aware_model_router.cli import main
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import ModelTier
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy, ThermostatAction


@pytest.mark.anyio
async def test_integrated_gateway_thermostat_cache_step_down_and_429_blocks() -> None:
    policies = {p.policy_id: p for p in default_policy_suite()}
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_int",
                agent_id="agent_loop",
                hourly_budget_usd=Decimal("0.03000000"),
                soft_downgrade_ratio=0.45,
                hard_cap_ratio=1.00,
                max_loop_repeats=3,
                min_quality_tier=ModelTier.COMPACT,
            ),
            BudgetPolicy(
                org_id="org_int",
                agent_id="agent_floor",
                hourly_budget_usd=Decimal("0.03000000"),
                soft_downgrade_ratio=0.40,
                hard_cap_ratio=1.00,
                min_quality_tier=ModelTier.FRONTIER,
            ),
        ]
    )
    gateway = OpenAIRouterGateway(
        primary_policy=policies["all-frontier"],
        shadow_policy=policies["threshold-aggressive"],
        thermostat=thermostat,
    )

    async with httpx.AsyncClient(
        transport=gateway.as_httpx_transport(), base_url="http://router.local"
    ) as client:
        # 1. Normal completion on frontier
        r1 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "request_id": "int-001",
                "category": "reasoning",
                "messages": [{"role": "user", "content": "Prove consensus invariant #1"}],
                "x_org_id": "org_int",
                "x_agent_id": "agent_loop",
            },
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["router_metadata"]["selected_tier"] == "frontier"
        assert (
            d1["router_metadata"]["thermostat_action"]
            == ThermostatAction.ALLOW_REQUESTED_TIER.value
        )

        # 2. Identical prompt -> semantic cache hit ($0.00000000)
        r2 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "request_id": "int-002",
                "category": "reasoning",
                "messages": [{"role": "user", "content": "Prove consensus invariant #1"}],
                "x_org_id": "org_int",
                "x_agent_id": "agent_loop",
            },
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["usage"]["cost_usd"] == "0.00000000"
        assert d2["router_metadata"]["thermostat_action"] == ThermostatAction.CACHE_HIT.value

        # 3. Different prompt while utilization >= 45% -> soft step-down from frontier to mid
        r3 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "request_id": "int-003",
                "category": "reasoning",
                "messages": [{"role": "user", "content": "Prove consensus invariant #2"}],
                "x_org_id": "org_int",
                "x_agent_id": "agent_loop",
            },
        )
        assert r3.status_code == 200
        d3 = r3.json()
        assert d3["router_metadata"]["selected_tier"] == "mid"
        assert d3["router_metadata"]["thermostat_action"] == ThermostatAction.STEP_DOWN_TIER.value

        # 4. 3rd repeat of prompt #1 -> 429 LOOP_CIRCUIT_BREAKER
        r4 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "request_id": "int-004",
                "category": "reasoning",
                "messages": [{"role": "user", "content": "Prove consensus invariant #1"}],
                "x_org_id": "org_int",
                "x_agent_id": "agent_loop",
            },
        )
        assert r4.status_code == 429
        assert r4.json()["error"]["type"] == ThermostatAction.LOOP_CIRCUIT_BREAKER.value

        # 5. Quality-floor protected agent: first request succeeds, second hits soft cap -> 429 escalation
        r5 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "request_id": "int-005",
                "category": "reasoning",
                "messages": [{"role": "user", "content": "Prove safety lemma #9"}],
                "x_org_id": "org_int",
                "x_agent_id": "agent_floor",
            },
        )
        assert r5.status_code == 200

        r6 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "request_id": "int-006",
                "category": "reasoning",
                "messages": [{"role": "user", "content": "Prove safety lemma #10"}],
                "x_org_id": "org_int",
                "x_agent_id": "agent_floor",
            },
        )
        assert r6.status_code == 429
        assert r6.json()["error"]["type"] == ThermostatAction.QUALITY_FLOOR_ESCALATION.value

        # 6. Live FinOps economics endpoint
        econ_resp = await client.get("/v1/finops/economics", params={"org_id": "org_int"})
        assert econ_resp.status_code == 200
        econ = econ_resp.json()
        assert econ["total_requests"] == 4
        assert econ["cache_hits"] == 1
        assert Decimal(econ["total_cost_usd"]) > Decimal("0")


def test_cli_simulate_thermostat_writes_artifacts(tmp_path: Path) -> None:
    out_dir = tmp_path / "sim"
    rc = main(["simulate-thermostat", "--out", str(out_dir)])
    assert rc == 0

    econ_path = out_dir / "thermostat-economics.json"
    shadow_path = out_dir / "shadow-summary.json"
    otel_path = out_dir / "otel-spans.jsonl"

    assert econ_path.is_file()
    assert shadow_path.is_file()
    assert otel_path.is_file()

    econ = json.loads(econ_path.read_text(encoding="utf-8"))
    assert econ["cache_hits"] >= 1
    assert len(econ["blocked_events"]) >= 1
    spans = [
        json.loads(line) for line in otel_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(spans) >= 3
