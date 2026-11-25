from __future__ import annotations

import asyncio
from decimal import Decimal

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
)
from cost_aware_model_router.policies import (
    build_evaluation_matrix,
    compute_2d_pareto_frontier,
    default_policy_suite,
    evaluate_policy_suite,
    select_policy_for_floor,
)
from cost_aware_model_router.schemas import (
    ComplexityFeatures,
    EvalRule,
    EvalType,
    ExecutionTrace,
    ModelTier,
    PolicyKind,
    PolicyResult,
    PolicySpec,
    ProvenanceBreakdown,
    ProvenanceTag,
    TaskCategory,
    WorkloadItem,
)


def test_complete_case_unknown_exclusion_and_cascade_sla_short_circuit() -> None:
    item_ok = WorkloadItem(
        request_id="req-ok",
        category=TaskCategory.CLASSIFICATION,
        prompt="Classify item",
        features=ComplexityFeatures(prompt_tokens_est=100),
        evaluators=[
            EvalRule(eval_type=EvalType.EXACT_MATCH, expected_text="BILLING"),
            EvalRule(eval_type=EvalType.LATENCY_SLA, max_latency_ms=1000.0),
        ],
    )
    item_unk = WorkloadItem(
        request_id="req-unk",
        category=TaskCategory.CLASSIFICATION,
        prompt="Classify item 2",
        features=ComplexityFeatures(prompt_tokens_est=100),
        evaluators=[EvalRule(eval_type=EvalType.EXACT_MATCH, expected_text="BILLING")],
    )
    workload = [item_ok, item_unk]

    # For req-ok: compact already violates 1000ms SLA (1200ms) -> cascade MUST short-circuit at compact!
    traces: dict[tuple[str, ModelTier], ExecutionTrace] = {
        ("req-ok", ModelTier.COMPACT): ExecutionTrace(
            request_id="req-ok",
            tier=ModelTier.COMPACT,
            model_id="c",
            response_text="WRONG",
            prompt_sha256="a" * 64,
            response_sha256="b" * 64,
            input_tokens=100,
            output_tokens=10,
            latency_ms=1200.0,
            cost_usd=Decimal("0.00100000"),
            tokens_provenance=ProvenanceTag.MEASURED,
            latency_provenance=ProvenanceTag.MEASURED,
            cost_provenance=ProvenanceTag.MODELED,
        ),
        ("req-ok", ModelTier.MID): ExecutionTrace(
            request_id="req-ok",
            tier=ModelTier.MID,
            model_id="m",
            response_text="BILLING",
            prompt_sha256="a" * 64,
            response_sha256="b" * 64,
            input_tokens=100,
            output_tokens=10,
            latency_ms=400.0,
            cost_usd=Decimal("0.00500000"),
            tokens_provenance=ProvenanceTag.MEASURED,
            latency_provenance=ProvenanceTag.MEASURED,
            cost_provenance=ProvenanceTag.MODELED,
        ),
        ("req-ok", ModelTier.FRONTIER): ExecutionTrace(
            request_id="req-ok",
            tier=ModelTier.FRONTIER,
            model_id="f",
            response_text="BILLING",
            prompt_sha256="a" * 64,
            response_sha256="b" * 64,
            input_tokens=100,
            output_tokens=10,
            latency_ms=800.0,
            cost_usd=Decimal("0.02000000"),
            tokens_provenance=ProvenanceTag.MEASURED,
            latency_provenance=ProvenanceTag.MEASURED,
            cost_provenance=ProvenanceTag.MODELED,
        ),
        # req-unk: compact succeeded (MEASURED, $0.001 burned), mid is UNKNOWN -> excluded from complete-case!
        ("req-unk", ModelTier.COMPACT): ExecutionTrace(
            request_id="req-unk",
            tier=ModelTier.COMPACT,
            model_id="c",
            response_text="BILLING",
            prompt_sha256="a" * 64,
            response_sha256="b" * 64,
            input_tokens=100,
            output_tokens=10,
            latency_ms=200.0,
            cost_usd=Decimal("0.00100000"),
            tokens_provenance=ProvenanceTag.MEASURED,
            latency_provenance=ProvenanceTag.MEASURED,
            cost_provenance=ProvenanceTag.MODELED,
        ),
        ("req-unk", ModelTier.MID): ExecutionTrace(
            request_id="req-unk",
            tier=ModelTier.MID,
            model_id="m",
            response_text="",
            prompt_sha256="a" * 64,
            response_sha256="b" * 64,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0.0,
            cost_usd=Decimal("0"),
            tokens_provenance=ProvenanceTag.UNKNOWN,
            latency_provenance=ProvenanceTag.UNKNOWN,
            cost_provenance=ProvenanceTag.UNKNOWN,
            error_code="http_500",
        ),
        ("req-unk", ModelTier.FRONTIER): ExecutionTrace(
            request_id="req-unk",
            tier=ModelTier.FRONTIER,
            model_id="f",
            response_text="BILLING",
            prompt_sha256="a" * 64,
            response_sha256="b" * 64,
            input_tokens=100,
            output_tokens=10,
            latency_ms=600.0,
            cost_usd=Decimal("0.02000000"),
            tokens_provenance=ProvenanceTag.MEASURED,
            latency_provenance=ProvenanceTag.MEASURED,
            cost_provenance=ProvenanceTag.MODELED,
        ),
    }

    _, matrix_map, complete_ids, excluded_ids, unknown_burned = build_evaluation_matrix(
        workload, traces
    )
    assert complete_ids == {"req-ok"}
    assert excluded_ids == {"req-unk"}
    assert unknown_burned["req-unk"] == Decimal("0.02100000")

    cascade_spec = PolicySpec(
        policy_id="cascade-test",
        display_name="Cascade",
        kind=PolicyKind.CASCADE_ESCALATION,
        deployable=True,
    )
    results, _ = evaluate_policy_suite(
        workload, [cascade_spec], matrix_map, complete_ids, excluded_ids, unknown_burned
    )
    res = results[0]
    assert res.evaluated_requests == 1
    assert res.excluded_unknown_requests == 1
    assert res.passed_count == 0
    # Zero-pass policy -> cost_per_successful_outcome_usd is None
    assert res.cost_per_successful_outcome_usd is None
    # Short-circuited at compact ($0.00100000), did NOT burn mid/frontier
    assert res.total_cost_usd == Decimal("0.00100000")
    assert res.decisions[0].attempted_tiers == [ModelTier.COMPACT]


def test_zero_pass_strictly_dominated_and_d2_oracle_all_fail_fallback() -> None:
    workload = generate_bundled_workload()
    catalog = load_bundled_catalog()
    traces = asyncio.run(DeterministicFixtureAdapter().execute_workload(workload, catalog))
    _, matrix_map, complete_ids, excluded_ids, unknown_burned = build_evaluation_matrix(
        workload, traces
    )
    results, frontier_ids = evaluate_policy_suite(
        workload, default_policy_suite(), matrix_map, complete_ids, excluded_ids, unknown_burned
    )
    by_id = {r.policy_id: r for r in results}

    # Oracle is non-deployable, so it must NOT be in deployable 2D Pareto frontier
    assert "ear-oracle-tau-0.02" not in frontier_ids

    # Check D2 all-fail fallback on req-059 and req-060 (where Frontier has partial score 0.666667 > 0.0)
    oracle_res = by_id["ear-oracle-tau-0.02"]
    dec_59 = next(d for d in oracle_res.decisions if d.request_id == "req-059")
    assert dec_59.oracle_all_failed is True
    assert dec_59.selected_tier == ModelTier.FRONTIER

    # Verify every point on the 2D frontier is uniquely reachable by select_policy_for_floor
    for fid in frontier_ids:
        target_pol = by_id[fid]
        selected, _ = select_policy_for_floor(results, target_pol.pass_rate)
        assert selected is not None
        assert selected.policy_id == fid

    # Infeasible floor (e.g., 0.99 when max pass_rate is 0.966667) returns None + closest
    sel_none, closest = select_policy_for_floor(results, 0.99)
    assert sel_none is None
    assert closest.pass_rate == max(r.pass_rate for r in results if r.deployable)


def test_zero_pass_policy_dominated_by_any_passing_policy() -> None:
    p_zero = PolicyResult(
        policy_id="zero-pass",
        display_name="Zero",
        kind=PolicyKind.STATIC_TIER,
        deployable=True,
        in_sample_fit=False,
        evaluated_requests=10,
        excluded_unknown_requests=0,
        passed_count=0,
        pass_rate=0.0,
        total_cost_usd=Decimal("0.00001000"),
        cost_savings_vs_baseline_usd=Decimal("0.01000000"),
        cost_savings_vs_baseline_pct=99.0,
        cost_per_successful_outcome_usd=None,
        p95_latency_ms=50.0,
        tier_counts={ModelTier.COMPACT: 10, ModelTier.MID: 0, ModelTier.FRONTIER: 0},
        provenance_breakdown=ProvenanceBreakdown(),
        decisions=[],
    )
    p_pass = p_zero.model_copy(
        update={
            "policy_id": "one-pass",
            "passed_count": 1,
            "pass_rate": 0.10,
            "total_cost_usd": Decimal("0.00500000"),
            "cost_per_successful_outcome_usd": Decimal("0.00500000"),
        }
    )
    frontier = compute_2d_pareto_frontier([p_zero, p_pass])
    assert frontier == ["one-pass"]
