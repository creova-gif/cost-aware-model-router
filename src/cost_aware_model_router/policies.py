from __future__ import annotations

import math
from decimal import Decimal
from typing import assert_never

from cost_aware_model_router.evaluators import evaluate_trace
from cost_aware_model_router.schemas import (
    TIER_ORDER,
    CellEvaluation,
    EvalRule,
    EvalType,
    ExecutionTrace,
    MetricProvenance,
    ModelTier,
    PolicyKind,
    PolicyResult,
    PolicySpec,
    ProvenanceBreakdown,
    ProvenanceTag,
    RoutingDecision,
    WorkloadItem,
    quantize_usd,
)


def default_policy_suite() -> list[PolicySpec]:
    """Return the Milestone 1 candidate policy suite (deployable policies + offline oracle)."""
    return [
        PolicySpec(
            policy_id="all-compact",
            display_name="All-Compact (Static)",
            kind=PolicyKind.STATIC_TIER,
            deployable=True,
            in_sample_fit=False,
            target_tier=ModelTier.COMPACT,
        ),
        PolicySpec(
            policy_id="threshold-aggressive",
            display_name="Threshold-Aggressive (c<0.45, m<0.75)",
            kind=PolicyKind.COMPLEXITY_THRESHOLD,
            deployable=True,
            in_sample_fit=True,
            compact_max_score=0.45,
            mid_max_score=0.75,
        ),
        PolicySpec(
            policy_id="threshold-balanced",
            display_name="Threshold-Balanced (c<0.30, m<0.65)",
            kind=PolicyKind.COMPLEXITY_THRESHOLD,
            deployable=True,
            in_sample_fit=True,
            compact_max_score=0.30,
            mid_max_score=0.65,
        ),
        PolicySpec(
            policy_id="cascade-escalation",
            display_name="Cascade-Escalation (Compact -> Mid -> Frontier)",
            kind=PolicyKind.CASCADE_ESCALATION,
            deployable=True,
            in_sample_fit=False,
        ),
        PolicySpec(
            policy_id="threshold-conservative",
            display_name="Threshold-Conservative (c<0.20, m<0.45)",
            kind=PolicyKind.COMPLEXITY_THRESHOLD,
            deployable=True,
            in_sample_fit=True,
            compact_max_score=0.20,
            mid_max_score=0.45,
        ),
        PolicySpec(
            policy_id="all-mid",
            display_name="All-Mid (Static)",
            kind=PolicyKind.STATIC_TIER,
            deployable=True,
            in_sample_fit=False,
            target_tier=ModelTier.MID,
        ),
        PolicySpec(
            policy_id="all-frontier",
            display_name="All-Frontier (Baseline)",
            kind=PolicyKind.STATIC_TIER,
            deployable=True,
            in_sample_fit=False,
            target_tier=ModelTier.FRONTIER,
        ),
        PolicySpec(
            policy_id="ear-oracle-tau-0.02",
            display_name="EAR-Oracle (tau=0.02) [ORACLE UPPER BOUND - non-deployable]",
            kind=PolicyKind.EAR_ORACLE,
            deployable=False,
            in_sample_fit=False,
            tau=0.02,
        ),
    ]


def compute_p95_latency(latencies: list[float]) -> float:
    if not latencies:
        return 0.0
    ordered = sorted(latencies)
    idx = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return round(ordered[idx], 2)


def _check_latency_sla_rules(rules: list[EvalRule], cumulative_latency_ms: float) -> bool:
    for rule in rules:
        if (
            rule.eval_type == EvalType.LATENCY_SLA
            and rule.max_latency_ms is not None
            and cumulative_latency_ms > rule.max_latency_ms
        ):
            return False
    return True


def build_evaluation_matrix(
    workload: list[WorkloadItem],
    traces: dict[tuple[str, ModelTier], ExecutionTrace],
) -> tuple[
    list[CellEvaluation],
    dict[tuple[str, ModelTier], CellEvaluation],
    set[str],
    set[str],
    dict[str, Decimal],
]:
    """Evaluate all (request_id, tier) traces and compute the complete-case intersection."""
    matrix_list: list[CellEvaluation] = []
    matrix_map: dict[tuple[str, ModelTier], CellEvaluation] = {}
    complete_case_ids: set[str] = set()
    excluded_unknown_ids: set[str] = set()
    unknown_burned_by_req: dict[str, Decimal] = {}

    for item in workload:
        req_unknown = False
        req_burned = Decimal("0.00000000")
        for tier in TIER_ORDER:
            trace = traces.get((item.request_id, tier))
            if trace is None:
                prov = MetricProvenance(
                    tokens_provenance=ProvenanceTag.UNKNOWN,
                    latency_provenance=ProvenanceTag.UNKNOWN,
                    verdict_provenance=ProvenanceTag.UNKNOWN,
                    cost_provenance=ProvenanceTag.UNKNOWN,
                )
                cell = CellEvaluation(
                    request_id=item.request_id,
                    category=item.category,
                    tier=tier,
                    model_id="missing",
                    prompt_sha256="",
                    response_sha256="",
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0.0,
                    cost_usd=Decimal("0"),
                    passed=False,
                    content_passed=False,
                    latency_passed=False,
                    score=0.0,
                    content_score=0.0,
                    provenance=prov,
                    failure_reasons=["missing_trace"],
                    error_code="missing_trace",
                )
            else:
                verdict = evaluate_trace(trace, item.evaluators)
                prov = MetricProvenance(
                    tokens_provenance=trace.tokens_provenance,
                    latency_provenance=trace.latency_provenance,
                    verdict_provenance=verdict.verdict_provenance,
                    cost_provenance=trace.cost_provenance,
                )
                cell = CellEvaluation(
                    request_id=item.request_id,
                    category=item.category,
                    tier=tier,
                    model_id=trace.model_id,
                    prompt_sha256=trace.prompt_sha256,
                    response_sha256=trace.response_sha256,
                    input_tokens=trace.input_tokens,
                    output_tokens=trace.output_tokens,
                    latency_ms=trace.latency_ms,
                    cost_usd=trace.cost_usd,
                    passed=verdict.passed,
                    content_passed=verdict.content_passed,
                    latency_passed=verdict.latency_passed,
                    score=verdict.score,
                    content_score=verdict.content_score,
                    provenance=prov,
                    failure_reasons=verdict.failure_reasons,
                    error_code=trace.error_code,
                )
            matrix_list.append(cell)
            matrix_map[(item.request_id, tier)] = cell
            if cell.provenance.is_any_unknown:
                req_unknown = True
            elif cell.provenance.tokens_provenance == ProvenanceTag.MEASURED:
                req_burned = quantize_usd(req_burned + cell.cost_usd)

        if req_unknown:
            excluded_unknown_ids.add(item.request_id)
            unknown_burned_by_req[item.request_id] = req_burned
        else:
            complete_case_ids.add(item.request_id)

    return matrix_list, matrix_map, complete_case_ids, excluded_unknown_ids, unknown_burned_by_req


def _combine_provenance(cells: list[CellEvaluation]) -> MetricProvenance:
    if any(c.provenance.is_any_unknown for c in cells):
        return MetricProvenance(
            tokens_provenance=ProvenanceTag.UNKNOWN,
            latency_provenance=ProvenanceTag.UNKNOWN,
            verdict_provenance=ProvenanceTag.UNKNOWN,
            cost_provenance=ProvenanceTag.UNKNOWN,
        )
    tok = (
        ProvenanceTag.MEASURED
        if all(c.provenance.tokens_provenance == ProvenanceTag.MEASURED for c in cells)
        else ProvenanceTag.MODELED
    )
    lat = (
        ProvenanceTag.MEASURED
        if all(c.provenance.latency_provenance == ProvenanceTag.MEASURED for c in cells)
        else ProvenanceTag.MODELED
    )
    ver = (
        ProvenanceTag.MEASURED
        if all(c.provenance.verdict_provenance == ProvenanceTag.MEASURED for c in cells)
        else ProvenanceTag.MODELED
    )
    cost = (
        ProvenanceTag.MEASURED
        if all(c.provenance.cost_provenance == ProvenanceTag.MEASURED for c in cells)
        else ProvenanceTag.MODELED
    )
    return MetricProvenance(
        tokens_provenance=tok,
        latency_provenance=lat,
        verdict_provenance=ver,
        cost_provenance=cost,
    )


def _evaluate_item_under_policy(
    item: WorkloadItem,
    policy: PolicySpec,
    matrix_map: dict[tuple[str, ModelTier], CellEvaluation],
) -> RoutingDecision:
    c_cell = matrix_map[(item.request_id, ModelTier.COMPACT)]
    m_cell = matrix_map[(item.request_id, ModelTier.MID)]
    f_cell = matrix_map[(item.request_id, ModelTier.FRONTIER)]
    frontier_req = (not c_cell.passed) and (not m_cell.passed) and f_cell.passed

    match policy.kind:
        case PolicyKind.STATIC_TIER:
            target = policy.target_tier or ModelTier.FRONTIER
            chosen = matrix_map[(item.request_id, target)]
            return RoutingDecision(
                request_id=item.request_id,
                category=item.category,
                policy_id=policy.policy_id,
                selected_tier=target,
                attempted_tiers=[target],
                cost_usd=chosen.cost_usd,
                baseline_cost_usd=f_cell.cost_usd,
                latency_ms=chosen.latency_ms,
                passed=chosen.passed,
                score=chosen.score,
                frontier_required=frontier_req,
                regressed_vs_baseline=f_cell.passed and (not chosen.passed),
                oracle_all_failed=False,
                provenance=chosen.provenance,
                explanation=f"Static policy routed directly to {target.value}.",
            )

        case PolicyKind.COMPLEXITY_THRESHOLD:
            c_max = policy.compact_max_score if policy.compact_max_score is not None else 0.30
            m_max = policy.mid_max_score if policy.mid_max_score is not None else 0.65
            c_score = item.features.weighted_score()
            if c_score < c_max:
                target = ModelTier.COMPACT
                reason = (
                    f"Complexity score {c_score:.2f} < compact threshold {c_max:.2f} "
                    f"(tokens={item.features.prompt_tokens_est}, code={item.features.has_code_or_sql})."
                )
            elif c_score < m_max:
                target = ModelTier.MID
                reason = (
                    f"Complexity score {c_score:.2f} in [{c_max:.2f}, {m_max:.2f}) -> mid tier "
                    f"(code={item.features.has_code_or_sql}, depth={item.features.schema_depth})."
                )
            else:
                target = ModelTier.FRONTIER
                reason = (
                    f"Complexity score {c_score:.2f} >= {m_max:.2f} -> escalated to frontier "
                    f"(multi_step={item.features.multi_step_reasoning}, diff={item.features.domain_difficulty})."
                )
            chosen = matrix_map[(item.request_id, target)]
            return RoutingDecision(
                request_id=item.request_id,
                category=item.category,
                policy_id=policy.policy_id,
                selected_tier=target,
                attempted_tiers=[target],
                cost_usd=chosen.cost_usd,
                baseline_cost_usd=f_cell.cost_usd,
                latency_ms=chosen.latency_ms,
                passed=chosen.passed,
                score=chosen.score,
                frontier_required=frontier_req,
                regressed_vs_baseline=f_cell.passed and (not chosen.passed),
                oracle_all_failed=False,
                provenance=chosen.provenance,
                explanation=reason,
            )

        case PolicyKind.CASCADE_ESCALATION:
            attempted: list[CellEvaluation] = []
            for tier in TIER_ORDER:
                cell = matrix_map[(item.request_id, tier)]
                attempted.append(cell)
                cum_latency = round(sum(c.latency_ms for c in attempted), 2)
                sla_ok = _check_latency_sla_rules(item.evaluators, cum_latency)
                if not sla_ok:
                    # Short-circuit: escalating further can only increase cumulative latency.
                    cum_cost = quantize_usd(sum((c.cost_usd for c in attempted), Decimal("0")))
                    return RoutingDecision(
                        request_id=item.request_id,
                        category=item.category,
                        policy_id=policy.policy_id,
                        selected_tier=tier,
                        attempted_tiers=[c.tier for c in attempted],
                        cost_usd=cum_cost,
                        baseline_cost_usd=f_cell.cost_usd,
                        latency_ms=cum_latency,
                        passed=False,
                        score=0.0,
                        frontier_required=frontier_req,
                        regressed_vs_baseline=f_cell.passed,
                        oracle_all_failed=False,
                        provenance=_combine_provenance(attempted),
                        explanation=(
                            f"Cascade short-circuited at {tier.value}: cumulative latency "
                            f"{cum_latency:.1f}ms violated latency_sla."
                        ),
                    )
                if cell.content_passed:
                    cum_cost = quantize_usd(sum((c.cost_usd for c in attempted), Decimal("0")))
                    chain_str = " -> ".join(c.tier.value for c in attempted)
                    return RoutingDecision(
                        request_id=item.request_id,
                        category=item.category,
                        policy_id=policy.policy_id,
                        selected_tier=tier,
                        attempted_tiers=[c.tier for c in attempted],
                        cost_usd=cum_cost,
                        baseline_cost_usd=f_cell.cost_usd,
                        latency_ms=cum_latency,
                        passed=True,
                        score=cell.score,
                        frontier_required=frontier_req,
                        regressed_vs_baseline=False,
                        oracle_all_failed=False,
                        provenance=_combine_provenance(attempted),
                        explanation=(
                            f"Cascade passed at {tier.value} (chain: {chain_str}; "
                            f"cumulative latency {cum_latency:.1f}ms)."
                        ),
                    )

            # All 3 tiers failed content check in cascade
            cum_cost = quantize_usd(sum((c.cost_usd for c in attempted), Decimal("0")))
            cum_latency = round(sum(c.latency_ms for c in attempted), 2)
            return RoutingDecision(
                request_id=item.request_id,
                category=item.category,
                policy_id=policy.policy_id,
                selected_tier=ModelTier.FRONTIER,
                attempted_tiers=[c.tier for c in attempted],
                cost_usd=cum_cost,
                baseline_cost_usd=f_cell.cost_usd,
                latency_ms=cum_latency,
                passed=False,
                score=f_cell.score,
                frontier_required=frontier_req,
                regressed_vs_baseline=f_cell.passed,
                oracle_all_failed=False,
                provenance=_combine_provenance(attempted),
                explanation="Cascade exhausted compact -> mid -> frontier; all tiers failed evaluator.",
            )

        case PolicyKind.EAR_ORACLE:
            tau = policy.tau if policy.tau is not None else 0.02
            cells = [c_cell, m_cell, f_cell]
            max_score = max(c.score for c in cells)
            passing_candidates = [
                c for c in cells if c.passed and (max_score - c.score) <= (tau + 1e-9)
            ]
            if passing_candidates:
                winner = min(
                    passing_candidates,
                    key=lambda c: (c.cost_usd, c.latency_ms, TIER_ORDER.index(c.tier)),
                )
                return RoutingDecision(
                    request_id=item.request_id,
                    category=item.category,
                    policy_id=policy.policy_id,
                    selected_tier=winner.tier,
                    attempted_tiers=[winner.tier],
                    cost_usd=winner.cost_usd,
                    baseline_cost_usd=f_cell.cost_usd,
                    latency_ms=winner.latency_ms,
                    passed=True,
                    score=winner.score,
                    frontier_required=frontier_req,
                    regressed_vs_baseline=False,
                    oracle_all_failed=False,
                    provenance=winner.provenance,
                    explanation=(
                        f"EAR-Oracle selected {winner.tier.value} (score {winner.score:.2f} within "
                        f"tau={tau:.2f} of max {max_score:.2f} at lowest cost)."
                    ),
                )

            # D2 locked rule: when all 3 tiers fail, pick highest-scoring tier,
            # tie-break by lowest cost -> lowest latency -> compact < mid < frontier,
            # and set oracle_all_failed=True.
            fallback = min(
                cells,
                key=lambda c: (-c.score, c.cost_usd, c.latency_ms, TIER_ORDER.index(c.tier)),
            )
            return RoutingDecision(
                request_id=item.request_id,
                category=item.category,
                policy_id=policy.policy_id,
                selected_tier=fallback.tier,
                attempted_tiers=[fallback.tier],
                cost_usd=fallback.cost_usd,
                baseline_cost_usd=f_cell.cost_usd,
                latency_ms=fallback.latency_ms,
                passed=False,
                score=fallback.score,
                frontier_required=frontier_req,
                regressed_vs_baseline=f_cell.passed,
                oracle_all_failed=True,
                provenance=fallback.provenance,
                explanation=(
                    f"All 3 tiers failed; D2 fallback selected {fallback.tier.value} "
                    f"(highest partial score {fallback.score:.2f}, tie-break lowest cost)."
                ),
            )

        case _ as unreachable:
            assert_never(unreachable)


def compute_2d_pareto_frontier(results: list[PolicyResult]) -> list[str]:
    """
    D3 Locked Rule: Exact 2D Pareto frontier over deployable policies on (total_cost_usd, pass_rate).
    - Zero-pass policies (passed_count == 0) are strictly dominated by any policy with passed_count > 0.
    - Among policies with identical (total_cost_usd, pass_rate), tie-break by lowest p95_latency_ms
      then lexicographical policy_id so every point on the frontier is unique and reachable by the 1D slider.
    """
    deployable = [r for r in results if r.deployable]
    if not deployable:
        return []

    has_positive_pass = any(r.passed_count > 0 for r in deployable)
    candidates = [r for r in deployable if (r.passed_count > 0 or not has_positive_pass)]

    frontier_ids: list[str] = []
    for cand in candidates:
        dominated = False
        for other in candidates:
            if other.policy_id == cand.policy_id:
                continue
            better_or_equal_quality = other.pass_rate >= cand.pass_rate
            cheaper_or_equal_cost = other.total_cost_usd <= cand.total_cost_usd
            strictly_better_2d = (other.pass_rate > cand.pass_rate) or (
                other.total_cost_usd < cand.total_cost_usd
            )
            if better_or_equal_quality and cheaper_or_equal_cost:
                if strictly_better_2d:
                    dominated = True
                    break
                # Identical 2D (cost, pass_rate): tie-break by p95_latency_ms then policy_id
                if (other.p95_latency_ms, other.policy_id) < (
                    cand.p95_latency_ms,
                    cand.policy_id,
                ):
                    dominated = True
                    break
        if not dominated:
            frontier_ids.append(cand.policy_id)

    # Sort frontier from lowest cost to highest cost (and lowest pass_rate to highest)
    by_id = {r.policy_id: r for r in deployable}
    frontier_ids.sort(
        key=lambda pid: (
            by_id[pid].total_cost_usd,
            by_id[pid].pass_rate,
            by_id[pid].p95_latency_ms,
            pid,
        )
    )
    return frontier_ids


def select_policy_for_floor(
    results: list[PolicyResult], q_min: float
) -> tuple[PolicyResult | None, PolicyResult]:
    """
    Deterministic 1D slider selection rule (matches inline JS in pareto-explorer.html):
    Among deployable Pareto-optimal policies with pass_rate >= q_min, pick minimum total_cost_usd,
    tie-breaking by p95_latency_ms -> -pass_rate -> policy_id.
    If none meet q_min, return (None, closest_feasible_policy).
    """
    frontier = [r for r in results if r.deployable and r.is_pareto_optimal]
    pool = frontier if frontier else [r for r in results if r.deployable]
    if not pool:
        pool = results

    eligible = [r for r in pool if r.pass_rate + 1e-9 >= q_min]
    if eligible:
        selected = min(
            eligible,
            key=lambda r: (r.total_cost_usd, r.p95_latency_ms, -r.pass_rate, r.policy_id),
        )
        return selected, selected

    closest = max(
        pool,
        key=lambda r: (r.pass_rate, -r.total_cost_usd, -r.p95_latency_ms),
    )
    return None, closest


def evaluate_policy_suite(
    workload: list[WorkloadItem],
    policies: list[PolicySpec],
    matrix_map: dict[tuple[str, ModelTier], CellEvaluation],
    complete_case_ids: set[str],
    excluded_unknown_ids: set[str],
    unknown_burned_by_req: dict[str, Decimal],
) -> tuple[list[PolicyResult], list[str]]:
    complete_items = [item for item in workload if item.request_id in complete_case_ids]
    excluded_count = len(excluded_unknown_ids)
    total_unknown_burned = quantize_usd(sum(unknown_burned_by_req.values(), Decimal("0.00000000")))

    # Compute baseline (all-frontier) total cost over complete_case_ids first
    baseline_cost = quantize_usd(
        sum(
            (matrix_map[(item.request_id, ModelTier.FRONTIER)].cost_usd for item in complete_items),
            Decimal("0.00000000"),
        )
    )

    raw_results: list[PolicyResult] = []
    for spec in policies:
        decisions: list[RoutingDecision] = []
        tier_counts: dict[ModelTier, int] = {
            ModelTier.COMPACT: 0,
            ModelTier.MID: 0,
            ModelTier.FRONTIER: 0,
        }
        measured_reqs = 0
        measured_cost = Decimal("0.00000000")
        modeled_reqs = 0
        modeled_cost = Decimal("0.00000000")

        for item in complete_items:
            dec = _evaluate_item_under_policy(item, spec, matrix_map)
            decisions.append(dec)
            tier_counts[dec.selected_tier] += 1
            if dec.provenance.summary_tag == ProvenanceTag.MEASURED:
                measured_reqs += 1
                measured_cost = quantize_usd(measured_cost + dec.cost_usd)
            else:
                modeled_reqs += 1
                modeled_cost = quantize_usd(modeled_cost + dec.cost_usd)

        eval_count = len(decisions)
        passed_count = sum(1 for d in decisions if d.passed)
        pass_rate = round(passed_count / eval_count, 6) if eval_count > 0 else 0.0
        total_cost = quantize_usd(sum((d.cost_usd for d in decisions), Decimal("0.00000000")))
        savings_usd = quantize_usd(baseline_cost - total_cost)
        savings_pct = (
            round(float((savings_usd / baseline_cost) * Decimal("100.0")), 2)
            if baseline_cost > Decimal("0")
            else 0.0
        )
        goodput = quantize_usd(total_cost / Decimal(passed_count)) if passed_count > 0 else None
        p95_ms = compute_p95_latency([d.latency_ms for d in decisions])

        raw_results.append(
            PolicyResult(
                policy_id=spec.policy_id,
                display_name=spec.display_name,
                kind=spec.kind,
                deployable=spec.deployable,
                in_sample_fit=spec.in_sample_fit,
                evaluated_requests=eval_count,
                excluded_unknown_requests=excluded_count,
                passed_count=passed_count,
                pass_rate=pass_rate,
                total_cost_usd=total_cost,
                cost_savings_vs_baseline_usd=savings_usd,
                cost_savings_vs_baseline_pct=savings_pct,
                cost_per_successful_outcome_usd=goodput,
                p95_latency_ms=p95_ms,
                tier_counts=tier_counts,
                is_pareto_optimal=False,
                provenance_breakdown=ProvenanceBreakdown(
                    measured_requests=measured_reqs,
                    measured_cost_usd=measured_cost,
                    modeled_requests=modeled_reqs,
                    modeled_cost_usd=modeled_cost,
                    unknown_excluded_requests=excluded_count,
                    unknown_burned_cost_usd=total_unknown_burned,
                ),
                decisions=decisions,
            )
        )

    frontier_ids = compute_2d_pareto_frontier(raw_results)
    frontier_set = set(frontier_ids)
    final_results = [
        r.model_copy(update={"is_pareto_optimal": r.policy_id in frontier_set}) for r in raw_results
    ]
    return final_results, frontier_ids
