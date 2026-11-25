from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from cost_aware_model_router.policies import (
    build_evaluation_matrix,
    evaluate_policy_suite,
)
from cost_aware_model_router.schemas import (
    ExecutionTrace,
    ModelTier,
    PolicyKind,
    PolicyResult,
    PolicySpec,
    ProvenanceTag,
    WorkloadItem,
)
from cost_aware_model_router.telemetry import split_workload_holdout

DEFAULT_COMPACT_GRID: tuple[float, ...] = (
    0.0,
    0.15,
    0.20,
    0.25,
    0.30,
    0.35,
    0.40,
    0.45,
    0.50,
    0.55,
)
DEFAULT_MID_GRID: tuple[float, ...] = (
    0.0,
    0.35,
    0.40,
    0.45,
    0.50,
    0.55,
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
)


class RubricCriterion(BaseModel):
    """Single declarative domain rubric criterion for deterministic or LLM judging."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    required_keywords: list[str] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)
    min_length: int = Field(default=1, ge=0)
    weight: float = Field(default=1.0, gt=0.0)


class JudgeVerdict(BaseModel):
    """Structured quality verdict produced by an LLMJudge implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    tier: ModelTier
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    criterion_scores: dict[str, float]
    judge_id: str = Field(min_length=1)
    provenance: ProvenanceTag
    rationale: str = Field(min_length=1)


@runtime_checkable
class LLMJudge(Protocol):
    """Pluggable protocol for response quality judges (deterministic rubrics or external LLMs)."""

    def evaluate_response(
        self,
        request_id: str,
        tier: ModelTier,
        prompt: str,
        response_text: str,
    ) -> JudgeVerdict: ...


class DeterministicRubricJudge:
    """Zero-cost deterministic rubric judge evaluating keyword, length, and safety criteria."""

    def __init__(
        self,
        criteria: list[RubricCriterion],
        *,
        pass_threshold: float = 0.80,
        judge_id: str = "deterministic-rubric-v1",
        provenance: ProvenanceTag = ProvenanceTag.MEASURED,
        case_sensitive: bool = False,
    ) -> None:
        if not criteria:
            raise ValueError("criteria must contain at least one RubricCriterion")
        if not (0.0 <= pass_threshold <= 1.0):
            raise ValueError("pass_threshold must be between 0.0 and 1.0")
        seen_ids: set[str] = set()
        for criterion in criteria:
            if criterion.criterion_id in seen_ids:
                raise ValueError(f"Duplicate criterion_id: {criterion.criterion_id!r}")
            seen_ids.add(criterion.criterion_id)

        self._criteria = list(criteria)
        self._pass_threshold = pass_threshold
        self._judge_id = judge_id
        self._provenance = provenance
        self._case_sensitive = case_sensitive

    @property
    def criteria(self) -> list[RubricCriterion]:
        return list(self._criteria)

    @property
    def pass_threshold(self) -> float:
        return self._pass_threshold

    @property
    def judge_id(self) -> str:
        return self._judge_id

    @property
    def provenance(self) -> ProvenanceTag:
        return self._provenance

    def _evaluate_criterion(
        self, criterion: RubricCriterion, response_text: str
    ) -> tuple[float, str | None]:
        stripped = response_text.strip()
        if len(stripped) < criterion.min_length:
            return (
                0.0,
                f"{criterion.criterion_id}: length {len(stripped)} < min_length {criterion.min_length}",
            )

        haystack = response_text if self._case_sensitive else response_text.lower()
        triggered_forbidden: list[str] = []
        for kw in criterion.forbidden_keywords:
            needle = kw if self._case_sensitive else kw.lower()
            if needle and needle in haystack:
                triggered_forbidden.append(kw)
        if triggered_forbidden:
            return (
                0.0,
                f"{criterion.criterion_id}: forbidden keywords present ({', '.join(triggered_forbidden)})",
            )

        if not criterion.required_keywords:
            return 1.0, None

        matched = 0
        missing: list[str] = []
        for kw in criterion.required_keywords:
            needle = kw if self._case_sensitive else kw.lower()
            if needle in haystack:
                matched += 1
            else:
                missing.append(kw)

        total_req = len(criterion.required_keywords)
        c_score = round(matched / total_req, 6) if total_req > 0 else 1.0
        if missing:
            return (
                c_score,
                f"{criterion.criterion_id}: missing required keywords ({', '.join(missing)})",
            )
        return 1.0, None

    def evaluate_response(
        self,
        request_id: str,
        tier: ModelTier,
        prompt: str,
        response_text: str,
    ) -> JudgeVerdict:
        _ = prompt
        criterion_scores: dict[str, float] = {}
        total_weight = 0.0
        weighted_sum = 0.0
        notes: list[str] = []

        for criterion in self._criteria:
            c_score, failure_note = self._evaluate_criterion(criterion, response_text)
            criterion_scores[criterion.criterion_id] = c_score
            total_weight += criterion.weight
            weighted_sum += c_score * criterion.weight
            if failure_note is not None:
                notes.append(failure_note)

        overall_score = round(weighted_sum / total_weight, 6) if total_weight > 0 else 0.0
        passed = (overall_score + 1e-9) >= self._pass_threshold
        if notes:
            status_prefix = "PASSED" if passed else "FAILED"
            rationale = (
                f"{status_prefix} (score={overall_score:.2f}, threshold={self._pass_threshold:.2f}): "
                + "; ".join(notes)
            )
        else:
            rationale = (
                f"PASSED (score={overall_score:.2f}, threshold={self._pass_threshold:.2f}): "
                f"all {len(self._criteria)} rubric criteria satisfied."
            )

        return JudgeVerdict(
            request_id=request_id,
            tier=tier,
            passed=passed,
            score=overall_score,
            criterion_scores=criterion_scores,
            judge_id=self._judge_id,
            provenance=self._provenance,
            rationale=rationale,
        )


class HoldoutGeneralizationReport(BaseModel):
    """Out-of-sample generalization audit report for threshold routing policies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    train_requests: int = Field(ge=1)
    holdout_requests: int = Field(ge=1)
    quality_floor: float = Field(ge=0.0, le=1.0)
    selected_compact_max: float = Field(ge=0.0, le=1.0)
    selected_mid_max: float = Field(ge=0.0, le=1.0)
    in_sample_pass_rate: float = Field(ge=0.0, le=1.0)
    out_of_sample_pass_rate: float = Field(ge=0.0, le=1.0)
    generalization_pass_delta: float
    in_sample_savings_pct: float
    out_of_sample_savings_pct: float
    holds_quality_floor_out_of_sample: bool


class OutOfSamplePolicyAuditor:
    """
    Splits a workload into disjoint train and holdout partitions, fits a complexity-threshold
    policy on the training split to minimize cost subject to quality_floor, and audits the
    frozen policy out-of-sample on the unseen holdout partition.
    """

    def __init__(
        self,
        workload: list[WorkloadItem] | None = None,
        traces: dict[tuple[str, ModelTier], ExecutionTrace] | None = None,
        *,
        holdout_fraction: float = 0.30,
        quality_tolerance: float = 0.03,
        compact_grid: tuple[float, ...] = DEFAULT_COMPACT_GRID,
        mid_grid: tuple[float, ...] = DEFAULT_MID_GRID,
    ) -> None:
        if not (0.0 < holdout_fraction < 1.0):
            raise ValueError("holdout_fraction must be strictly between 0.0 and 1.0")
        if quality_tolerance < 0.0:
            raise ValueError("quality_tolerance must be non-negative")
        self._workload = list(workload) if workload is not None else None
        self._traces = dict(traces) if traces is not None else None
        self._holdout_fraction = holdout_fraction
        self._quality_tolerance = quality_tolerance
        self._compact_grid = compact_grid
        self._mid_grid = mid_grid

    def _build_candidate_grid_specs(self) -> list[PolicySpec]:
        specs: list[PolicySpec] = []
        seen_pairs: set[tuple[float, float]] = set()
        for c_max in self._compact_grid:
            for m_max in self._mid_grid:
                if c_max > m_max:
                    continue
                if c_max == m_max and c_max != 0.0:
                    continue
                pair = (round(c_max, 4), round(m_max, 4))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                policy_id = f"grid-c{pair[0]:.2f}-m{pair[1]:.2f}"
                specs.append(
                    PolicySpec(
                        policy_id=policy_id,
                        display_name=f"Grid Threshold (c<{pair[0]:.2f}, m<{pair[1]:.2f})",
                        kind=PolicyKind.COMPLEXITY_THRESHOLD,
                        deployable=True,
                        in_sample_fit=True,
                        compact_max_score=pair[0],
                        mid_max_score=pair[1],
                    )
                )
        return specs

    @staticmethod
    def _select_best_train_policy(
        train_results: list[PolicyResult],
        specs_by_id: dict[str, PolicySpec],
        quality_floor: float,
    ) -> tuple[PolicyResult, PolicySpec]:
        feasible = [r for r in train_results if (r.pass_rate + 1e-9) >= quality_floor]
        if feasible:
            best = min(
                feasible,
                key=lambda r: (
                    r.total_cost_usd,
                    r.p95_latency_ms,
                    -r.pass_rate,
                    r.policy_id,
                ),
            )
            return best, specs_by_id[best.policy_id]

        closest = max(
            train_results,
            key=lambda r: (
                r.pass_rate,
                -r.total_cost_usd,
                -r.p95_latency_ms,
            ),
        )
        return closest, specs_by_id[closest.policy_id]

    def audit(
        self,
        workload: list[WorkloadItem] | float | None = None,
        traces: dict[tuple[str, ModelTier], ExecutionTrace] | None = None,
        *,
        quality_floor: float = 0.90,
        holdout_fraction: float | None = None,
    ) -> HoldoutGeneralizationReport:
        if isinstance(workload, (int, float)):
            effective_floor = float(workload)
            effective_workload = self._workload
        else:
            effective_floor = quality_floor
            effective_workload = workload if workload is not None else self._workload

        effective_traces = traces if traces is not None else self._traces
        effective_fraction = (
            holdout_fraction if holdout_fraction is not None else self._holdout_fraction
        )

        if effective_workload is None or effective_traces is None:
            raise ValueError("workload and traces must be provided to OutOfSamplePolicyAuditor")
        if not (0.0 <= effective_floor <= 1.0):
            raise ValueError("quality_floor must be between 0.0 and 1.0")

        train_workload, holdout_workload = split_workload_holdout(
            effective_workload, holdout_fraction=effective_fraction
        )

        _, train_matrix_map, train_complete, train_unknown, train_burned = build_evaluation_matrix(
            train_workload, effective_traces
        )
        if not train_complete:
            raise ValueError("No complete-case requests available in training split")

        candidate_specs = self._build_candidate_grid_specs()
        specs_by_id = {spec.policy_id: spec for spec in candidate_specs}

        train_results, _ = evaluate_policy_suite(
            train_workload,
            candidate_specs,
            train_matrix_map,
            train_complete,
            train_unknown,
            train_burned,
        )
        selected_train_result, selected_spec = self._select_best_train_policy(
            train_results, specs_by_id, effective_floor
        )

        c_max = (
            selected_spec.compact_max_score if selected_spec.compact_max_score is not None else 0.0
        )
        m_max = selected_spec.mid_max_score if selected_spec.mid_max_score is not None else 0.0

        frozen_holdout_spec = PolicySpec(
            policy_id="frozen-holdout-threshold",
            display_name=f"Frozen Holdout Threshold (c<{c_max:.2f}, m<{m_max:.2f})",
            kind=PolicyKind.COMPLEXITY_THRESHOLD,
            deployable=True,
            in_sample_fit=False,
            compact_max_score=c_max,
            mid_max_score=m_max,
        )
        all_compact_spec = PolicySpec(
            policy_id="all-compact",
            display_name="All-Compact (Static)",
            kind=PolicyKind.STATIC_TIER,
            deployable=True,
            in_sample_fit=False,
            target_tier=ModelTier.COMPACT,
        )
        all_frontier_spec = PolicySpec(
            policy_id="all-frontier",
            display_name="All-Frontier (Baseline)",
            kind=PolicyKind.STATIC_TIER,
            deployable=True,
            in_sample_fit=False,
            target_tier=ModelTier.FRONTIER,
        )

        _, holdout_matrix_map, holdout_complete, holdout_unknown, holdout_burned = (
            build_evaluation_matrix(holdout_workload, effective_traces)
        )
        if not holdout_complete:
            raise ValueError("No complete-case requests available in holdout split")

        holdout_results, _ = evaluate_policy_suite(
            holdout_workload,
            [all_compact_spec, frozen_holdout_spec, all_frontier_spec],
            holdout_matrix_map,
            holdout_complete,
            holdout_unknown,
            holdout_burned,
        )
        holdout_by_id = {r.policy_id: r for r in holdout_results}
        frozen_holdout_result = holdout_by_id["frozen-holdout-threshold"]

        in_pass = selected_train_result.pass_rate
        out_pass = frozen_holdout_result.pass_rate
        pass_delta = round(out_pass - in_pass, 6)
        holds_floor = (out_pass + self._quality_tolerance + 1e-9) >= effective_floor

        return HoldoutGeneralizationReport(
            train_requests=selected_train_result.evaluated_requests,
            holdout_requests=frozen_holdout_result.evaluated_requests,
            quality_floor=effective_floor,
            selected_compact_max=c_max,
            selected_mid_max=m_max,
            in_sample_pass_rate=in_pass,
            out_of_sample_pass_rate=out_pass,
            generalization_pass_delta=pass_delta,
            in_sample_savings_pct=selected_train_result.cost_savings_vs_baseline_pct,
            out_of_sample_savings_pct=frozen_holdout_result.cost_savings_vs_baseline_pct,
            holds_quality_floor_out_of_sample=holds_floor,
        )
