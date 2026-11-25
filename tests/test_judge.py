from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cost_aware_model_router.judge import (
    DeterministicRubricJudge,
    HoldoutGeneralizationReport,
    JudgeVerdict,
    LLMJudge,
    OutOfSamplePolicyAuditor,
    RubricCriterion,
)
from cost_aware_model_router.schemas import (
    ComplexityFeatures,
    EvalRule,
    EvalType,
    ExecutionTrace,
    ModelTier,
    ProvenanceTag,
    TaskCategory,
    WorkloadItem,
)
from cost_aware_model_router.telemetry import split_workload_holdout


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_synthetic_workload_and_traces(
    count: int = 20,
) -> tuple[list[WorkloadItem], dict[tuple[str, ModelTier], ExecutionTrace]]:
    workload: list[WorkloadItem] = []
    traces: dict[tuple[str, ModelTier], ExecutionTrace] = {}
    categories = [
        TaskCategory.CLASSIFICATION,
        TaskCategory.SUMMARIZATION,
        TaskCategory.CODE_SQL,
        TaskCategory.REASONING,
    ]

    for idx in range(count):
        req_id = f"req_{idx:03d}"
        cat = categories[idx % len(categories)]
        # Create easy, medium, and hard requests across complexity spectrum
        bucket = idx % 3
        if bucket == 0:
            features = ComplexityFeatures(
                prompt_tokens_est=120,
                has_code_or_sql=False,
                schema_depth=0,
                multi_step_reasoning=False,
                domain_difficulty=0.1,
            )
        elif bucket == 1:
            features = ComplexityFeatures(
                prompt_tokens_est=480,
                has_code_or_sql=True,
                schema_depth=1,
                multi_step_reasoning=False,
                domain_difficulty=0.4,
            )
        else:
            features = ComplexityFeatures(
                prompt_tokens_est=1100,
                has_code_or_sql=True,
                schema_depth=4,
                multi_step_reasoning=True,
                domain_difficulty=0.9,
            )

        prompt_text = f"Prompt {req_id} for {cat.value}"
        item = WorkloadItem(
            request_id=req_id,
            category=cat,
            prompt=prompt_text,
            features=features,
            evaluators=[EvalRule(eval_type=EvalType.EXACT_MATCH, expected_text="VALID_RESPONSE")],
        )
        workload.append(item)

        # Compact passes bucket 0 only; Mid passes buckets 0 and 1; Frontier passes all buckets
        compact_resp = "VALID_RESPONSE" if bucket == 0 else "INCOMPLETE"
        mid_resp = "VALID_RESPONSE" if bucket in (0, 1) else "INCOMPLETE"
        frontier_resp = "VALID_RESPONSE"

        traces[(req_id, ModelTier.COMPACT)] = ExecutionTrace(
            request_id=req_id,
            tier=ModelTier.COMPACT,
            model_id="gemini-2.5-flash-lite",
            response_text=compact_resp,
            prompt_sha256=_sha256(prompt_text),
            response_sha256=_sha256(compact_resp),
            input_tokens=150,
            output_tokens=60,
            latency_ms=110.0,
            cost_usd=Decimal("0.00003900"),
            tokens_provenance=ProvenanceTag.MEASURED,
            latency_provenance=ProvenanceTag.MEASURED,
            cost_provenance=ProvenanceTag.MEASURED,
        )
        traces[(req_id, ModelTier.MID)] = ExecutionTrace(
            request_id=req_id,
            tier=ModelTier.MID,
            model_id="gpt-4o-mini",
            response_text=mid_resp,
            prompt_sha256=_sha256(prompt_text),
            response_sha256=_sha256(mid_resp),
            input_tokens=150,
            output_tokens=60,
            latency_ms=260.0,
            cost_usd=Decimal("0.00005850"),
            tokens_provenance=ProvenanceTag.MEASURED,
            latency_provenance=ProvenanceTag.MEASURED,
            cost_provenance=ProvenanceTag.MEASURED,
        )
        traces[(req_id, ModelTier.FRONTIER)] = ExecutionTrace(
            request_id=req_id,
            tier=ModelTier.FRONTIER,
            model_id="claude-3-7-sonnet",
            response_text=frontier_resp,
            prompt_sha256=_sha256(prompt_text),
            response_sha256=_sha256(frontier_resp),
            input_tokens=150,
            output_tokens=60,
            latency_ms=720.0,
            cost_usd=Decimal("0.00135000"),
            tokens_provenance=ProvenanceTag.MEASURED,
            latency_provenance=ProvenanceTag.MEASURED,
            cost_provenance=ProvenanceTag.MEASURED,
        )

    return workload, traces


def test_rubric_criterion_and_judge_verdict_schemas_are_frozen_and_strict() -> None:
    criterion = RubricCriterion(
        criterion_id="sql_safety",
        description="Must contain SELECT and WHERE, and never DROP TABLE.",
        required_keywords=["SELECT", "WHERE"],
        forbidden_keywords=["DROP TABLE"],
        min_length=15,
        weight=2.0,
    )
    assert criterion.criterion_id == "sql_safety"
    with pytest.raises(ValidationError):
        RubricCriterion.model_validate(
            {
                "criterion_id": "bad",
                "description": "extra field not allowed",
                "unknown_field": 123,
            }
        )

    verdict = JudgeVerdict(
        request_id="req_01",
        tier=ModelTier.COMPACT,
        passed=True,
        score=1.0,
        criterion_scores={"sql_safety": 1.0},
        judge_id="deterministic-rubric-v1",
        provenance=ProvenanceTag.MEASURED,
        rationale="All rubric criteria passed.",
    )
    assert verdict.passed is True
    with pytest.raises(ValidationError):
        JudgeVerdict.model_validate(
            {
                "request_id": "req_01",
                "tier": "compact",
                "passed": True,
                "score": 1.5,  # > 1.0 invalid
                "criterion_scores": {},
                "judge_id": "j1",
                "provenance": "measured",
                "rationale": "bad score",
            }
        )


def test_deterministic_rubric_judge_satisfies_llm_judge_protocol_and_scores_accurately() -> None:
    criteria = [
        RubricCriterion(
            criterion_id="completeness",
            description="Includes root cause and remediation steps",
            required_keywords=["root cause", "remediation"],
            forbidden_keywords=["TODO", "placeholder"],
            min_length=25,
            weight=0.6,
        ),
        RubricCriterion(
            criterion_id="conciseness_and_tone",
            description="Professional incident summary",
            required_keywords=["incident"],
            forbidden_keywords=["idk"],
            min_length=10,
            weight=0.4,
        ),
    ]
    judge = DeterministicRubricJudge(
        criteria=criteria,
        pass_threshold=0.80,
        judge_id="domain-incident-rubric",
        provenance=ProvenanceTag.MEASURED,
    )
    assert isinstance(judge, LLMJudge)

    good_verdict = judge.evaluate_response(
        request_id="req_100",
        tier=ModelTier.MID,
        prompt="Summarize incident #42.",
        response_text=(
            "Incident #42 summary: the root cause was connection pool exhaustion; "
            "remediation increased pool limits and added circuit breakers."
        ),
    )
    assert good_verdict.passed is True
    assert good_verdict.score == 1.0
    assert good_verdict.criterion_scores == {"completeness": 1.0, "conciseness_and_tone": 1.0}
    assert good_verdict.provenance == ProvenanceTag.MEASURED
    assert good_verdict.judge_id == "domain-incident-rubric"

    # Partial match: missing 'remediation' on first criterion (score 0.5 * 0.6 + 1.0 * 0.4 = 0.70 < 0.80)
    partial_verdict = judge.evaluate_response(
        request_id="req_101",
        tier=ModelTier.COMPACT,
        prompt="Summarize incident #42.",
        response_text="Incident #42 summary: the root cause was a timeout in downstream service.",
    )
    assert partial_verdict.passed is False
    assert partial_verdict.score == pytest.approx(0.70)
    assert partial_verdict.criterion_scores["completeness"] == pytest.approx(0.5)
    assert partial_verdict.criterion_scores["conciseness_and_tone"] == pytest.approx(1.0)
    assert "missing required keywords (remediation)" in partial_verdict.rationale

    # Forbidden keyword triggers 0.0 on completeness
    forbidden_verdict = judge.evaluate_response(
        request_id="req_102",
        tier=ModelTier.COMPACT,
        prompt="Summarize incident #42.",
        response_text="Incident #42 root cause and remediation: TODO fill this in later.",
    )
    assert forbidden_verdict.passed is False
    assert forbidden_verdict.criterion_scores["completeness"] == 0.0
    assert "forbidden keywords present (TODO)" in forbidden_verdict.rationale


def test_custom_external_llm_judge_plugin_preserves_modeled_provenance() -> None:
    class MockExternalLLMJudge:
        def evaluate_response(
            self,
            request_id: str,
            tier: ModelTier,
            prompt: str,
            response_text: str,
        ) -> JudgeVerdict:
            has_answer = "42" in response_text and len(prompt) > 0
            return JudgeVerdict(
                request_id=request_id,
                tier=tier,
                passed=has_answer,
                score=0.95 if has_answer else 0.10,
                criterion_scores={"correctness": 0.95 if has_answer else 0.10},
                judge_id="external-judge-llm-mock",
                provenance=ProvenanceTag.MODELED,
                rationale="Evaluated by mocked external judge model.",
            )

    custom_judge: LLMJudge = MockExternalLLMJudge()
    assert isinstance(custom_judge, LLMJudge)
    verdict = custom_judge.evaluate_response(
        request_id="req_ext_1",
        tier=ModelTier.FRONTIER,
        prompt="What is the answer?",
        response_text="The answer is 42.",
    )
    assert verdict.passed is True
    assert verdict.provenance == ProvenanceTag.MODELED
    assert verdict.judge_id == "external-judge-llm-mock"


def test_out_of_sample_policy_auditor_fits_train_and_generalizes_to_holdout() -> None:
    workload, traces = _build_synthetic_workload_and_traces(count=24)
    auditor = OutOfSamplePolicyAuditor(workload, traces, holdout_fraction=0.30)
    report = auditor.audit(quality_floor=0.95)

    assert isinstance(report, HoldoutGeneralizationReport)
    assert report.train_requests + report.holdout_requests == 24
    assert report.train_requests >= 14
    assert report.holdout_requests >= 6
    assert report.quality_floor == 0.95
    assert 0.0 < report.selected_compact_max < report.selected_mid_max
    assert report.in_sample_pass_rate >= 0.95
    assert report.out_of_sample_pass_rate >= 0.95
    assert report.holds_quality_floor_out_of_sample is True
    assert report.in_sample_savings_pct > 40.0
    assert report.out_of_sample_savings_pct > 40.0
    assert report.generalization_pass_delta == pytest.approx(
        report.out_of_sample_pass_rate - report.in_sample_pass_rate, abs=1e-6
    )


def test_out_of_sample_policy_auditor_detects_overfit_degradation_on_holdout() -> None:
    workload, traces = _build_synthetic_workload_and_traces(count=20)
    auditor = OutOfSamplePolicyAuditor(holdout_fraction=0.30)

    # Corrupt compact & mid responses ONLY on the holdout split so an in-sample threshold
    # policy that trusts compact/mid degrades sharply out-of-sample.
    _, holdout_items = split_workload_holdout(workload, holdout_fraction=0.30)
    holdout_ids = {item.request_id for item in holdout_items}

    overfit_traces = dict(traces)
    for req_id in holdout_ids:
        for tier in (ModelTier.COMPACT, ModelTier.MID):
            orig = overfit_traces[(req_id, tier)]
            overfit_traces[(req_id, tier)] = orig.model_copy(
                update={"response_text": "BROKEN_ON_HOLDOUT"}
            )

    report = auditor.audit(workload, overfit_traces, quality_floor=0.95)
    assert report.in_sample_pass_rate >= 0.95
    assert report.out_of_sample_pass_rate < 0.50
    assert report.generalization_pass_delta < -0.40
    assert report.holds_quality_floor_out_of_sample is False
