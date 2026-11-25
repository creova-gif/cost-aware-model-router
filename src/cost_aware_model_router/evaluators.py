from __future__ import annotations

import json
import re
from typing import Any, assert_never

from cost_aware_model_router.schemas import (
    EvalRule,
    EvalType,
    EvalVerdict,
    ExecutionTrace,
    ProvenanceTag,
)

MAX_REGEX_PATTERN_LENGTH = 256
# Reject nested quantifiers such as (a+)+, (a*)*, (.+)+ that cause catastrophic backtracking (ReDoS).
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*?][^()]*\)[+*?{]")


def validate_safe_regex(pattern: str) -> re.Pattern[str]:
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        raise ValueError(
            f"Regex pattern length {len(pattern)} exceeds limit {MAX_REGEX_PATTERN_LENGTH}"
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise ValueError(f"Unsafe nested quantifier in regex pattern: {pattern!r}")
    return re.compile(pattern)


def _check_json_type(val: Any, expected_type: str) -> bool:
    if expected_type == "str":
        return isinstance(val, str)
    if expected_type == "int":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected_type == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if expected_type == "bool":
        return isinstance(val, bool)
    if expected_type == "list":
        return isinstance(val, list)
    if expected_type == "dict":
        return isinstance(val, dict)
    return False


def _resolve_dotted_path(obj: Any, path: str) -> tuple[bool, Any]:
    current: Any = obj
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def evaluate_single_rule(
    rule: EvalRule, response_text: str, latency_ms: float
) -> tuple[bool, float, str | None]:
    match rule.eval_type:
        case EvalType.JSON_SCHEMA:
            if not rule.schema_fields:
                return False, 0.0, "json_schema: missing schema_fields definition"
            try:
                parsed = json.loads(response_text)
            except json.JSONDecodeError as exc:
                return False, 0.0, f"json_schema: invalid JSON ({exc.msg})"
            if not isinstance(parsed, dict):
                return False, 0.0, "json_schema: top-level JSON value is not an object"
            matched = 0
            failures: list[str] = []
            for key, expected_type in rule.schema_fields.items():
                if key not in parsed:
                    failures.append(f"missing key '{key}'")
                elif not _check_json_type(parsed[key], expected_type):
                    failures.append(f"key '{key}' failed type '{expected_type}'")
                else:
                    matched += 1
            total = len(rule.schema_fields)
            score = matched / total if total > 0 else 1.0
            if failures:
                return False, round(score, 6), f"json_schema: {', '.join(failures)}"
            return True, 1.0, None

        case EvalType.REGEX_MATCH:
            if not rule.pattern:
                return False, 0.0, "regex_match: missing pattern"
            try:
                compiled = validate_safe_regex(rule.pattern)
            except (ValueError, re.error) as exc:
                return False, 0.0, f"regex_match: invalid/unsafe pattern ({exc})"
            flags = 0 if rule.case_sensitive else re.IGNORECASE
            if re.search(compiled.pattern, response_text, flags=flags):
                return True, 1.0, None
            return False, 0.0, f"regex_match: pattern {rule.pattern!r} not found"

        case EvalType.EXACT_MATCH:
            norm = response_text.strip()
            if rule.expected_text is not None:
                cmp_a = norm if rule.case_sensitive else norm.lower()
                cmp_b = (
                    rule.expected_text.strip()
                    if rule.case_sensitive
                    else rule.expected_text.strip().lower()
                )
                if cmp_a == cmp_b:
                    return True, 1.0, None
                return False, 0.0, f"exact_match: expected {rule.expected_text!r}, got {norm!r}"
            if rule.allowed_values:
                allowed = (
                    [v.strip() for v in rule.allowed_values]
                    if rule.case_sensitive
                    else [v.strip().lower() for v in rule.allowed_values]
                )
                probe = norm if rule.case_sensitive else norm.lower()
                if probe in allowed:
                    return True, 1.0, None
                return False, 0.0, f"exact_match: {norm!r} not in allowed_values"
            return False, 0.0, "exact_match: missing expected_text or allowed_values"

        case EvalType.JSON_FIELD_PREDICATE:
            if not rule.predicates:
                return False, 0.0, "json_field_predicate: missing predicates"
            try:
                parsed_obj = json.loads(response_text)
            except json.JSONDecodeError as exc:
                return False, 0.0, f"json_field_predicate: invalid JSON ({exc.msg})"
            matched_preds = 0
            pred_failures: list[str] = []
            for path, checks in rule.predicates.items():
                found, value = _resolve_dotted_path(parsed_obj, path)
                if not found:
                    pred_failures.append(f"path '{path}' missing")
                    continue
                path_ok = True
                for op, target in checks.items():
                    if op == "eq" and value != target:
                        path_ok = False
                        pred_failures.append(f"{path} != {target!r}")
                    elif op == "min" and (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or value < target
                    ):
                        path_ok = False
                        pred_failures.append(f"{path} < {target}")
                    elif op == "max" and (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or value > target
                    ):
                        path_ok = False
                        pred_failures.append(f"{path} > {target}")
                    elif op == "contains" and (
                        not isinstance(value, (str, list)) or target not in value
                    ):
                        path_ok = False
                        pred_failures.append(f"{path} missing {target!r}")
                    elif op not in ("eq", "min", "max", "contains"):
                        path_ok = False
                        pred_failures.append(f"unsupported predicate op '{op}'")
                if path_ok:
                    matched_preds += 1
            total_preds = len(rule.predicates)
            pred_score = matched_preds / total_preds if total_preds > 0 else 1.0
            if pred_failures:
                return (
                    False,
                    round(pred_score, 6),
                    f"json_field_predicate: {', '.join(pred_failures)}",
                )
            return True, 1.0, None

        case EvalType.LATENCY_SLA:
            if rule.max_latency_ms is None:
                return False, 0.0, "latency_sla: missing max_latency_ms"
            if latency_ms <= rule.max_latency_ms:
                return True, 1.0, None
            return (
                False,
                0.0,
                f"latency_sla: {latency_ms:.1f}ms > max {rule.max_latency_ms:.1f}ms",
            )

        case _ as unreachable:
            assert_never(unreachable)


def evaluate_trace(
    trace: ExecutionTrace,
    rules: list[EvalRule],
    *,
    override_latency_ms: float | None = None,
) -> EvalVerdict:
    """Evaluate an ExecutionTrace against declarative rules (zero eval/exec)."""
    if (
        trace.error_code is not None
        or trace.tokens_provenance == ProvenanceTag.UNKNOWN
        or trace.latency_provenance == ProvenanceTag.UNKNOWN
        or trace.cost_provenance == ProvenanceTag.UNKNOWN
    ):
        return EvalVerdict(
            request_id=trace.request_id,
            tier=trace.tier,
            passed=False,
            content_passed=False,
            latency_passed=False,
            score=0.0,
            content_score=0.0,
            verdict_provenance=ProvenanceTag.UNKNOWN,
            failure_reasons=[f"execution_unknown:{trace.error_code or 'missing_provenance'}"],
        )

    if not rules:
        return EvalVerdict(
            request_id=trace.request_id,
            tier=trace.tier,
            passed=False,
            content_passed=False,
            latency_passed=False,
            score=0.0,
            content_score=0.0,
            verdict_provenance=ProvenanceTag.UNKNOWN,
            failure_reasons=["missing_evaluators"],
        )

    effective_latency = override_latency_ms if override_latency_ms is not None else trace.latency_ms
    content_passed = True
    latency_passed = True
    total_weight = 0.0
    weighted_sum = 0.0
    content_weight = 0.0
    content_weighted_sum = 0.0
    failures: list[str] = []

    for rule in rules:
        ok, rule_score, reason = evaluate_single_rule(rule, trace.response_text, effective_latency)
        total_weight += rule.weight
        weighted_sum += rule_score * rule.weight
        if rule.eval_type == EvalType.LATENCY_SLA:
            if not ok:
                latency_passed = False
        else:
            content_weight += rule.weight
            content_weighted_sum += rule_score * rule.weight
            if not ok:
                content_passed = False
        if not ok and reason:
            failures.append(reason)

    overall_score = round(weighted_sum / total_weight, 6) if total_weight > 0 else 0.0
    c_score = (
        round(content_weighted_sum / content_weight, 6) if content_weight > 0 else overall_score
    )
    verdict_prov = (
        ProvenanceTag.MEASURED
        if trace.tokens_provenance == ProvenanceTag.MEASURED
        else ProvenanceTag.MODELED
    )
    return EvalVerdict(
        request_id=trace.request_id,
        tier=trace.tier,
        passed=content_passed and latency_passed,
        content_passed=content_passed,
        latency_passed=latency_passed,
        score=overall_score,
        content_score=c_score,
        verdict_provenance=verdict_prov,
        failure_reasons=failures,
    )
