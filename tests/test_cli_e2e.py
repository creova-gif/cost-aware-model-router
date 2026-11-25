from __future__ import annotations

import json
import time
from pathlib import Path

from cost_aware_model_router.cli import main


def test_cli_demo_and_verify_receipt_end_to_end(tmp_path: Path) -> None:
    out_dir = tmp_path / "demo_out"
    start = time.perf_counter()
    rc_demo = main(["demo", "--out", str(out_dir)])
    elapsed = time.perf_counter() - start

    assert rc_demo == 0
    assert elapsed < 5.0

    receipt_json = out_dir / "savings-receipt.json"
    receipt_md = out_dir / "savings-receipt.md"
    explorer_html = out_dir / "pareto-explorer.html"

    assert receipt_json.is_file()
    assert receipt_md.is_file()
    assert explorer_html.is_file()

    # Verify unmodified receipt passes
    rc_verify = main(["verify-receipt", str(receipt_json)])
    assert rc_verify == 0

    # Tamper with receipt file and confirm non-zero exit code
    data = json.loads(receipt_json.read_text(encoding="utf-8"))
    data["complete_case_requests"] = 999
    tampered_path = tmp_path / "tampered-receipt.json"
    tampered_path.write_text(json.dumps(data), encoding="utf-8")

    rc_tampered = main(["verify-receipt", str(tampered_path)])
    assert rc_tampered != 0


def test_cli_replay_from_custom_jsonl(tmp_path: Path) -> None:
    workload_file = tmp_path / "custom.jsonl"
    row = {
        "request_id": "custom-001",
        "org_id": "org_custom",
        "agent_id": "agent_custom",
        "category": "classification",
        "prompt": "Classify ticket #1",
        "features": {
            "prompt_tokens_est": 80,
            "has_code_or_sql": False,
            "schema_depth": 0,
            "multi_step_reasoning": False,
            "domain_difficulty": 0.1,
        },
        "evaluators": [
            {
                "eval_type": "exact_match",
                "allowed_values": ["BILLING", "TECHNICAL", "ACCOUNT"],
            }
        ],
    }
    workload_file.write_text(json.dumps(row) + "\n", encoding="utf-8")

    out_dir = tmp_path / "replay_out"
    rc = main(["replay", "--workload", str(workload_file), "--out", str(out_dir)])
    assert rc == 0
    assert main(["verify-receipt", str(out_dir / "savings-receipt.json")]) == 0
