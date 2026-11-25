from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

from cost_aware_model_router.policies import select_policy_for_floor
from cost_aware_model_router.schemas import (
    CellEvaluation,
    ModelTier,
    PolicyResult,
    PricingCatalog,
    ReceiptIntegrity,
    SavingsReceipt,
    WorkloadItem,
    format_usd_str,
)


def _normalize_for_canonical_json(value: Any) -> Any:
    """
    Recursively normalize values so raw JSON dicts and Pydantic dumps produce
    identical canonical UTF-8 bytes (`allow_nan=False`, fixed float precision, 8-place Decimals).
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return format_usd_str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite float {value!r} forbidden in canonical receipt JSON")
        return f"{value:.6f}"
    if isinstance(value, list):
        return [_normalize_for_canonical_json(item) for item in value]
    if isinstance(value, dict):
        return {
            str(k): _normalize_for_canonical_json(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    raise TypeError(f"Unsupported type in canonical receipt payload: {type(value).__name__}")


def canonical_json_bytes(payload_without_integrity: dict[str, Any]) -> bytes:
    normalized = _normalize_for_canonical_json(payload_without_integrity)
    serialized = json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return serialized.encode("utf-8")


def compute_receipt_digest(payload_without_integrity: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload_without_integrity)).hexdigest()


def compute_workload_sha256(workload: list[WorkloadItem]) -> str:
    rows = [item.model_dump(mode="json") for item in workload]
    return hashlib.sha256(canonical_json_bytes({"workload": rows})).hexdigest()


def build_savings_receipt(
    *,
    workload_name: str,
    workload: list[WorkloadItem],
    catalog: PricingCatalog,
    evaluation_matrix: list[CellEvaluation],
    policy_results: list[PolicyResult],
    pareto_optimal_ids: list[str],
    complete_case_count: int,
    excluded_unknown_count: int,
    default_quality_floor: float = 0.92,
) -> SavingsReceipt:
    org_id = workload[0].org_id if workload else "org_demo"
    workload_hash = compute_workload_sha256(workload)
    selected, closest = select_policy_for_floor(policy_results, default_quality_floor)
    rec_policy = selected if selected is not None else closest
    oracle_policies = [r for r in policy_results if not r.deployable]
    oracle_id = oracle_policies[0].policy_id if oracle_policies else rec_policy.policy_id

    draft = SavingsReceipt(
        org_id=org_id,
        workload_name=workload_name,
        workload_sha256=workload_hash,
        catalog_version=catalog.catalog_version,
        total_workload_requests=len(workload),
        complete_case_requests=complete_case_count,
        excluded_unknown_requests=excluded_unknown_count,
        baseline_policy_id="all-frontier",
        recommended_policy_id=rec_policy.policy_id,
        oracle_policy_id=oracle_id,
        pareto_optimal_policy_ids=pareto_optimal_ids,
        catalog=catalog,
        evaluation_matrix=evaluation_matrix,
        policy_results=policy_results,
        integrity=ReceiptIntegrity(digest="0" * 64),
    )

    raw_dict = json.loads(draft.model_dump_json())
    raw_without_integrity = {k: v for k, v in raw_dict.items() if k != "integrity"}
    digest = compute_receipt_digest(raw_without_integrity)
    return draft.model_copy(update={"integrity": ReceiptIntegrity(digest=digest)})


def verify_receipt_dict(raw_dict: dict[str, Any]) -> tuple[bool, str]:
    """
    Verify a parsed JSON receipt dict:
    1. Hash the parsed raw JSON dict (minus `integrity`) first so float/field canonicalization
       never depends on Pydantic re-dumping defaults.
    2. Validate schema & semantic invariants via Pydantic.
    """
    if not isinstance(raw_dict, dict):
        return False, "Receipt root is not a JSON object"
    integrity_obj = raw_dict.get("integrity")
    if not isinstance(integrity_obj, dict) or not isinstance(integrity_obj.get("digest"), str):
        return False, "Missing or malformed integrity.digest field"

    claimed_digest = integrity_obj["digest"]
    payload_without_integrity = {k: v for k, v in raw_dict.items() if k != "integrity"}
    try:
        actual_digest = compute_receipt_digest(payload_without_integrity)
    except (ValueError, TypeError) as exc:
        return False, f"Canonicalization error: {exc}"

    if actual_digest != claimed_digest:
        return (
            False,
            f"SHA-256 digest mismatch: claimed {claimed_digest[:12]}..., "
            f"computed {actual_digest[:12]}...",
        )

    try:
        receipt = SavingsReceipt.model_validate(raw_dict)
    except Exception as exc:
        return False, f"Schema validation failed: {exc}"

    if (
        receipt.complete_case_requests + receipt.excluded_unknown_requests
        != receipt.total_workload_requests
    ):
        return (
            False,
            "Invariant violation: complete_case_requests + excluded_unknown_requests "
            "!= total_workload_requests",
        )

    for pol in receipt.policy_results:
        if pol.evaluated_requests != receipt.complete_case_requests:
            return (
                False,
                f"Policy {pol.policy_id!r} evaluated_requests ({pol.evaluated_requests}) "
                f"!= complete_case_requests ({receipt.complete_case_requests})",
            )

    return True, f"OK (SHA-256: {actual_digest})"


def verify_receipt_file(path: Path) -> tuple[bool, str]:
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(
            text,
            parse_constant=lambda c: (_ for _ in ()).throw(
                ValueError(f"Non-finite constant {c} forbidden")
            ),
        )
    except Exception as exc:
        return False, f"Failed to read/parse receipt JSON: {exc}"
    return verify_receipt_dict(raw)


def render_receipt_markdown(receipt: SavingsReceipt) -> str:
    by_id = {r.policy_id: r for r in receipt.policy_results}
    base = by_id.get(receipt.baseline_policy_id, receipt.policy_results[0])
    rec = by_id.get(receipt.recommended_policy_id, receipt.policy_results[0])
    oracle = by_id.get(receipt.oracle_policy_id)

    lines: list[str] = [
        f"# Auditable Savings Receipt — `{receipt.workload_name}`",
        "",
        f"- **Receipt Version:** `{receipt.receipt_version}` | **Org ID:** `{receipt.org_id}`",
        f"- **Workload SHA-256:** `{receipt.workload_sha256}`",
        f"- **Receipt Integrity SHA-256:** `{receipt.integrity.digest}`",
        f"- **Integrity Note:** {receipt.integrity.note}",
        f"- **Complete-Case Evaluated Requests:** `{receipt.complete_case_requests}` / `{receipt.total_workload_requests}` "
        f"(Excluded `unknown`: `{receipt.excluded_unknown_requests}`)",
        "",
        "## Executive Summary",
        "",
        f"- **Baseline Policy (`{base.display_name}`):** `${format_usd_str(base.total_cost_usd)}` total "
        f"| Pass rate: `{base.pass_rate * 100:.1f}%` ({base.passed_count}/{base.evaluated_requests}) "
        f"| P95 Latency: `{base.p95_latency_ms:.0f} ms`",
        f"- **Recommended Deployable Policy (`{rec.display_name}`):** `${format_usd_str(rec.total_cost_usd)}` total "
        f"(`{rec.cost_savings_vs_baseline_pct:+.2f}%` savings, `-${format_usd_str(rec.cost_savings_vs_baseline_usd)}`) "
        f"| Pass rate: `{rec.pass_rate * 100:.1f}%` ({rec.passed_count}/{rec.evaluated_requests}) "
        f"| P95 Latency: `{rec.p95_latency_ms:.0f} ms`",
    ]
    if oracle is not None:
        lines.append(
            f"- **Offline Hindsight Oracle (`{oracle.display_name}`):** `${format_usd_str(oracle.total_cost_usd)}` total "
            f"| Pass rate: `{oracle.pass_rate * 100:.1f}%` ({oracle.passed_count}/{oracle.evaluated_requests}) "
            "*(non-deployable theoretical bound)*"
        )

    pb = rec.provenance_breakdown
    lines.extend(
        [
            "",
            "## Provenance Breakdown (Recommended Policy)",
            "",
            f"- **Measured (live execution + evaluator):** `{pb.measured_requests}` requests (`${format_usd_str(pb.measured_cost_usd)}`)",
            f"- **Modeled (fixture or catalog-priced):** `{pb.modeled_requests}` requests (`${format_usd_str(pb.modeled_cost_usd)}`)",
            f"- **Unknown (excluded from savings math):** `{pb.unknown_excluded_requests}` requests "
            f"(burned spend tracked separately: `${format_usd_str(pb.unknown_burned_cost_usd)}`)",
            "",
            "## Policy Comparison Table",
            "",
            "| Policy | Type | Pareto 2D | In-Sample Fit | Pass Rate | Total Cost (USD) | Cost / Success (Goodput) | Savings vs Baseline | P95 Latency | Tier Mix (C / M / F) |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    for pol in receipt.policy_results:
        p_type = "Deployable" if pol.deployable else "Oracle (Non-Deployable)"
        pareto_mark = "YES" if pol.is_pareto_optimal else "no"
        fit_mark = "YES (grid)" if pol.in_sample_fit else "no"
        goodput_str = (
            f"${format_usd_str(pol.cost_per_successful_outcome_usd)}"
            if pol.cost_per_successful_outcome_usd is not None
            else "∞"
        )
        c_cnt = pol.tier_counts.get(ModelTier.COMPACT, 0)
        m_cnt = pol.tier_counts.get(ModelTier.MID, 0)
        f_cnt = pol.tier_counts.get(ModelTier.FRONTIER, 0)
        lines.append(
            f"| `{pol.policy_id}` | {p_type} | {pareto_mark} | {fit_mark} | "
            f"{pol.pass_rate * 100:.1f}% ({pol.passed_count}/{pol.evaluated_requests}) | "
            f"${format_usd_str(pol.total_cost_usd)} | {goodput_str} | "
            f"{pol.cost_savings_vs_baseline_pct:+.1f}% | {pol.p95_latency_ms:.0f} ms | "
            f"{c_cnt} / {m_cnt} / {f_cnt} |"
        )

    lines.append("")
    return "\n".join(lines)
