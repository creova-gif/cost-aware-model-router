from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from cost_aware_model_router.schemas import (
    ComplexityFeatures,
    ModelTier,
    ProvenanceTag,
    TaskCategory,
)
from cost_aware_model_router.thermostat import (
    AgentBudgetThermostat,
    BudgetPolicy,
    ThermostatAction,
)


def test_budget_policy_validation() -> None:
    policy = BudgetPolicy(
        org_id="org_acme",
        agent_id="agent_sql",
        hourly_budget_usd=Decimal("5.00"),
        soft_downgrade_ratio=0.80,
        hard_cap_ratio=1.00,
        max_loop_repeats=3,
        min_quality_tier=ModelTier.MID,
    )
    assert policy.hourly_budget_usd == Decimal("5.00000000")

    with pytest.raises(ValidationError):
        BudgetPolicy(
            org_id="org_acme",
            hourly_budget_usd=Decimal("0"),
        )

    with pytest.raises(ValidationError):
        BudgetPolicy(
            org_id="org_acme",
            hourly_budget_usd=Decimal("10.00"),
            soft_downgrade_ratio=0.95,
            hard_cap_ratio=0.90,
        )


def test_allow_and_semantic_cache_hit() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_fin",
                agent_id="agent_triage",
                hourly_budget_usd=Decimal("1.00"),
                max_loop_repeats=3,
                enable_semantic_cache=True,
            )
        ]
    )

    decision1 = thermostat.evaluate_request(
        request_id="req_1",
        org_id="org_fin",
        agent_id="agent_triage",
        prompt="  Classify   customer ticket #42  ",
        requested_tier=ModelTier.MID,
    )
    assert decision1.action == ThermostatAction.ALLOW_REQUESTED_TIER
    assert decision1.allowed is True
    assert decision1.approved_tier == ModelTier.MID
    assert decision1.cached_response is None

    thermostat.record_outcome(
        request_id="req_1",
        org_id="org_fin",
        agent_id="agent_triage",
        category=TaskCategory.CLASSIFICATION,
        tier=ModelTier.MID,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("0.05000000"),
        input_tokens=150,
        output_tokens=20,
        prompt="Classify customer ticket #42",
        response_text='{"label": "billing"}',
    )

    # Second identical (after whitespace normalization) prompt hits semantic cache at $0 cost
    decision2 = thermostat.evaluate_request(
        request_id="req_2",
        org_id="org_fin",
        agent_id="agent_triage",
        prompt="Classify customer ticket #42\n",
        requested_tier=ModelTier.MID,
    )
    assert decision2.action == ThermostatAction.CACHE_HIT
    assert decision2.allowed is True
    assert decision2.cached_response == '{"label": "billing"}'
    assert decision2.remaining_budget_usd == Decimal("0.95000000")


def test_runaway_loop_circuit_breaker() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_loop",
                agent_id="agent_runaway",
                hourly_budget_usd=Decimal("10.00"),
                max_loop_repeats=3,
                enable_semantic_cache=False,
            )
        ]
    )

    for idx in range(1, 3):
        dec = thermostat.evaluate_request(
            request_id=f"req_{idx}",
            org_id="org_loop",
            agent_id="agent_runaway",
            prompt="SELECT * FROM broken_table;",
            requested_tier=ModelTier.FRONTIER,
        )
        assert dec.allowed is True
        assert dec.action == ThermostatAction.ALLOW_REQUESTED_TIER

    # 3rd identical prompt in window trips LOOP_CIRCUIT_BREAKER
    dec3 = thermostat.evaluate_request(
        request_id="req_3",
        org_id="org_loop",
        agent_id="agent_runaway",
        prompt="  SELECT   * FROM broken_table; ",
        requested_tier=ModelTier.FRONTIER,
    )
    assert dec3.allowed is False
    assert dec3.approved_tier is None
    assert dec3.action == ThermostatAction.LOOP_CIRCUIT_BREAKER


def test_soft_downgrade_and_quality_floor_escalation() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_ops",
                agent_id="agent_coder",
                hourly_budget_usd=Decimal("1.00000000"),
                soft_downgrade_ratio=0.80,
                hard_cap_ratio=1.00,
                min_quality_tier=ModelTier.MID,
            )
        ]
    )

    # Spend $0.82 (82% of $1.00 budget -> above 80% soft downgrade threshold)
    thermostat.record_outcome(
        request_id="req_init",
        org_id="org_ops",
        agent_id="agent_coder",
        category=TaskCategory.CODE_SQL,
        tier=ModelTier.FRONTIER,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("0.82000000"),
        input_tokens=2000,
        output_tokens=800,
    )

    # Requesting FRONTIER steps down gracefully to MID (which satisfies min_quality_tier=MID)
    step_down = thermostat.evaluate_request(
        request_id="req_step",
        org_id="org_ops",
        agent_id="agent_coder",
        prompt="Write a SQL query joining orders and line_items.",
        requested_tier=ModelTier.FRONTIER,
    )
    assert step_down.allowed is True
    assert step_down.action == ThermostatAction.STEP_DOWN_TIER
    assert step_down.approved_tier == ModelTier.MID
    assert step_down.budget_utilization_ratio == pytest.approx(0.82)

    # Requesting MID cannot step down to COMPACT because min_quality_tier is MID
    floor_escalation = thermostat.evaluate_request(
        request_id="req_floor",
        org_id="org_ops",
        agent_id="agent_coder",
        prompt="Refactor complex payment state machine.",
        requested_tier=ModelTier.MID,
    )
    assert floor_escalation.allowed is False
    assert floor_escalation.approved_tier is None
    assert floor_escalation.action == ThermostatAction.QUALITY_FLOOR_ESCALATION

    # Complexity-driven quality floor also prevents stepping down below MID on high-complexity tasks
    thermostat_compact_floor = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_ops2",
                agent_id="agent_reasoner",
                hourly_budget_usd=Decimal("1.00000000"),
                soft_downgrade_ratio=0.80,
                hard_cap_ratio=1.00,
                min_quality_tier=ModelTier.COMPACT,
            )
        ]
    )
    thermostat_compact_floor.record_outcome(
        request_id="req_burn",
        org_id="org_ops2",
        agent_id="agent_reasoner",
        category=TaskCategory.REASONING,
        tier=ModelTier.FRONTIER,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("0.85000000"),
        input_tokens=1000,
        output_tokens=500,
    )
    high_complexity = ComplexityFeatures(
        prompt_tokens_est=1200,
        has_code_or_sql=True,
        schema_depth=4,
        multi_step_reasoning=True,
        domain_difficulty=0.9,
    )
    comp_escalation = thermostat_compact_floor.evaluate_request(
        request_id="req_comp_floor",
        org_id="org_ops2",
        agent_id="agent_reasoner",
        prompt="Prove deadlock freedom across 3 distributed locks.",
        requested_tier=ModelTier.MID,
        features=high_complexity,
    )
    assert comp_escalation.allowed is False
    assert comp_escalation.action == ThermostatAction.QUALITY_FLOOR_ESCALATION


def test_hard_cap_budget_circuit_breaker_and_economics_summary() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_ent",
                hourly_budget_usd=Decimal("0.50000000"),
                soft_downgrade_ratio=0.80,
                hard_cap_ratio=1.00,
            )
        ]
    )

    thermostat.record_outcome(
        request_id="r1",
        org_id="org_ent",
        agent_id="agent_a",
        category=TaskCategory.JSON_EXTRACTION,
        tier=ModelTier.COMPACT,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("0.20000000"),
        input_tokens=500,
        output_tokens=100,
    )
    thermostat.record_outcome(
        request_id="r2",
        org_id="org_ent",
        agent_id="agent_b",
        category=TaskCategory.REASONING,
        tier=ModelTier.FRONTIER,
        provenance=ProvenanceTag.MODELED,
        passed=False,
        cost_usd=Decimal("0.30000000"),
        input_tokens=1500,
        output_tokens=600,
    )

    # Budget is now 100% utilized ($0.50 / $0.50)
    blocked = thermostat.evaluate_request(
        request_id="r3",
        org_id="org_ent",
        agent_id="agent_a",
        prompt="Extract invoice fields",
        requested_tier=ModelTier.COMPACT,
    )
    assert blocked.allowed is False
    assert blocked.approved_tier is None
    assert blocked.action == ThermostatAction.BUDGET_CIRCUIT_BREAKER
    assert blocked.remaining_budget_usd == Decimal("0.00000000")

    summary = thermostat.summarize_economics(org_id="org_ent")
    assert summary.total_requests == 2
    assert summary.passed_requests == 1
    assert summary.failed_requests == 1
    assert summary.total_cost_usd == Decimal("0.50000000")
    assert summary.measured_cost_usd == Decimal("0.20000000")
    assert summary.modeled_cost_usd == Decimal("0.30000000")
    assert summary.cost_per_request_usd == Decimal("0.25000000")
    # Goodput cost per successful outcome = $0.50 / 1 pass = $0.50
    assert summary.cost_per_successful_outcome_usd == Decimal("0.50000000")
    assert summary.spend_by_org["org_ent"] == Decimal("0.50000000")
    assert summary.spend_by_agent["org_ent/agent_a"] == Decimal("0.20000000")
    assert summary.spend_by_agent["org_ent/agent_b"] == Decimal("0.30000000")
    assert summary.spend_by_category[TaskCategory.JSON_EXTRACTION] == Decimal("0.20000000")
    assert summary.spend_by_category[TaskCategory.REASONING] == Decimal("0.30000000")
    assert summary.spend_by_tier[ModelTier.COMPACT] == Decimal("0.20000000")
    assert summary.spend_by_tier[ModelTier.FRONTIER] == Decimal("0.30000000")
