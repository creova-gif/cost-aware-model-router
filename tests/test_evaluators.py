from __future__ import annotations

from decimal import Decimal

import pytest

from cost_aware_model_router.evaluators import evaluate_trace, validate_safe_regex
from cost_aware_model_router.schemas import (
    EvalRule,
    EvalType,
    ExecutionTrace,
    ModelTier,
    ProvenanceTag,
)


def _make_trace(
    response_text: str,
    *,
    latency_ms: float = 250.0,
    tokens_prov: ProvenanceTag = ProvenanceTag.MODELED,
    error_code: str | None = None,
) -> ExecutionTrace:
    return ExecutionTrace(
        request_id="req-test",
        tier=ModelTier.COMPACT,
        model_id="model-test",
        response_text=response_text,
        prompt_sha256="a" * 64,
        response_sha256="b" * 64,
        input_tokens=100,
        output_tokens=50,
        latency_ms=latency_ms,
        cost_usd=Decimal("0.00010000"),
        tokens_provenance=tokens_prov,
        latency_provenance=tokens_prov,
        cost_provenance=tokens_prov,
        error_code=error_code,
    )


def test_json_schema_and_field_predicates() -> None:
    trace = _make_trace('{"customer_id": "c1", "invoice_total": 45.0, "currency": "USD"}')
    rules = [
        EvalRule(
            eval_type=EvalType.JSON_SCHEMA,
            schema_fields={"customer_id": "str", "invoice_total": "number", "currency": "str"},
        ),
        EvalRule(
            eval_type=EvalType.JSON_FIELD_PREDICATE,
            predicates={"currency": {"eq": "USD"}, "invoice_total": {"min": 10, "max": 100}},
        ),
    ]
    verdict = evaluate_trace(trace, rules)
    assert verdict.passed is True
    assert verdict.content_passed is True
    assert verdict.score == 1.0

    bad_trace = _make_trace('{"customer_id": "c1", "invoice_total": true, "currency": "EUR"}')
    bad_verdict = evaluate_trace(bad_trace, rules)
    assert bad_verdict.passed is False
    assert 0.0 < bad_verdict.score < 1.0
    assert any("invoice_total" in r for r in bad_verdict.failure_reasons)


def test_redos_guard_rejects_nested_quantifiers_and_overlong_patterns() -> None:
    with pytest.raises(ValueError, match="nested quantifier"):
        validate_safe_regex(r"(a+)+$")
    with pytest.raises(ValueError, match="exceeds limit"):
        validate_safe_regex("a" * 300)

    trace = _make_trace("aaaaab")
    verdict = evaluate_trace(trace, [EvalRule(eval_type=EvalType.REGEX_MATCH, pattern=r"(a+)+$")])
    assert verdict.passed is False
    assert any("unsafe pattern" in r for r in verdict.failure_reasons)


def test_latency_sla_and_unknown_trace_handling() -> None:
    trace = _make_trace("BILLING", latency_ms=1800.0)
    rules = [
        EvalRule(eval_type=EvalType.EXACT_MATCH, allowed_values=["BILLING", "TECHNICAL"]),
        EvalRule(eval_type=EvalType.LATENCY_SLA, max_latency_ms=1500.0),
    ]
    verdict = evaluate_trace(trace, rules)
    assert verdict.content_passed is True
    assert verdict.latency_passed is False
    assert verdict.passed is False

    err_trace = _make_trace("", tokens_prov=ProvenanceTag.UNKNOWN, error_code="http_500")
    err_verdict = evaluate_trace(err_trace, rules)
    assert err_verdict.verdict_provenance == ProvenanceTag.UNKNOWN
    assert err_verdict.passed is False
