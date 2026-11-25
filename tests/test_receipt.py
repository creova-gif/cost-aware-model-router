from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
)
from cost_aware_model_router.policies import (
    build_evaluation_matrix,
    default_policy_suite,
    evaluate_policy_suite,
)
from cost_aware_model_router.receipt import (
    build_savings_receipt,
    canonical_json_bytes,
    verify_receipt_dict,
    verify_receipt_file,
)


def test_canonical_json_rejects_nan_and_normalizes_key_order() -> None:
    b1 = canonical_json_bytes({"b": 1.5, "a": {"y": 2, "x": 1}})
    b2 = canonical_json_bytes({"a": {"x": 1, "y": 2}, "b": 1.5000000})
    assert b1 == b2

    with pytest.raises(ValueError, match="Non-finite float"):
        canonical_json_bytes({"bad": float("nan")})


def test_receipt_verification_detects_semantic_tampering(tmp_path: Path) -> None:
    workload = generate_bundled_workload()[:10]
    catalog = load_bundled_catalog()
    traces = asyncio.run(DeterministicFixtureAdapter().execute_workload(workload, catalog))
    eval_matrix, matrix_map, complete_ids, excluded_ids, unknown_burned = build_evaluation_matrix(
        workload, traces
    )
    results, frontier_ids = evaluate_policy_suite(
        workload, default_policy_suite(), matrix_map, complete_ids, excluded_ids, unknown_burned
    )
    receipt = build_savings_receipt(
        workload_name="unit-test.jsonl",
        workload=workload,
        catalog=catalog,
        evaluation_matrix=eval_matrix,
        policy_results=results,
        pareto_optimal_ids=frontier_ids,
        complete_case_count=len(complete_ids),
        excluded_unknown_count=len(excluded_ids),
    )

    receipt_path = tmp_path / "savings-receipt.json"
    receipt_path.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")

    ok, msg = verify_receipt_file(receipt_path)
    assert ok is True
    assert "OK (SHA-256:" in msg

    # Tamper with a single cost digit -> verification must fail
    raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    raw["policy_results"][0]["total_cost_usd"] = "0.00000001"
    ok_tampered, msg_tampered = verify_receipt_dict(raw)
    assert ok_tampered is False
    assert "SHA-256 digest mismatch" in msg_tampered
