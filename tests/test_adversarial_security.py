from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    OpenAICompatibleAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
    validate_endpoint_security,
)
from cost_aware_model_router.evaluators import (
    MAX_REGEX_PATTERN_LENGTH,
    evaluate_single_rule,
    validate_safe_regex,
)
from cost_aware_model_router.policies import (
    build_evaluation_matrix,
    default_policy_suite,
    evaluate_policy_suite,
)
from cost_aware_model_router.receipt import (
    build_savings_receipt,
    compute_receipt_digest,
    render_receipt_markdown,
    verify_receipt_dict,
    verify_receipt_file,
)
from cost_aware_model_router.report import (
    escape_json_for_script_tag,
    render_pareto_explorer_html,
)
from cost_aware_model_router.schemas import (
    EvalRule,
    EvalType,
    ModelTier,
    ProvenanceTag,
    SavingsReceipt,
    TaskCategory,
    WorkloadItem,
    quantize_usd,
)
from cost_aware_model_router.thermostat import (
    AgentBudgetThermostat,
    BudgetPolicy,
    ThermostatAction,
)


def _build_sample_receipt(workload_name: str = "sec-audit") -> SavingsReceipt:
    catalog = load_bundled_catalog()
    workload = generate_bundled_workload()[:6]
    adapter = DeterministicFixtureAdapter()
    traces = asyncio.run(adapter.execute_workload(workload, catalog))
    matrix_list, matrix_map, complete_ids, unknown_ids, burned_map = build_evaluation_matrix(
        workload, traces
    )
    policies = default_policy_suite()
    results, frontier_ids = evaluate_policy_suite(
        workload, policies, matrix_map, complete_ids, unknown_ids, burned_map
    )
    return build_savings_receipt(
        workload_name=workload_name,
        workload=workload,
        catalog=catalog,
        evaluation_matrix=matrix_list,
        policy_results=results,
        pareto_optimal_ids=frontier_ids,
        complete_case_count=len(complete_ids),
        excluded_unknown_count=len(unknown_ids),
    )


# ---------------------------------------------------------------------------
# 1. Tenant Isolation (TB3)
# ---------------------------------------------------------------------------


def test_tb3_tenant_isolation_budget_exhaustion_never_throttles_other_tenant() -> None:
    """Exhausting 100% of org_alpha budget never throttles or blocks org_beta with identical agent_id."""
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_alpha",
                agent_id="shared_agent_name",
                hourly_budget_usd=Decimal("1.00000000"),
                soft_downgrade_ratio=0.80,
                hard_cap_ratio=1.00,
            ),
            BudgetPolicy(
                org_id="org_beta",
                agent_id="shared_agent_name",
                hourly_budget_usd=Decimal("1.00000000"),
                soft_downgrade_ratio=0.80,
                hard_cap_ratio=1.00,
            ),
        ]
    )

    thermostat.record_outcome(
        request_id="alpha-exhaust-1",
        org_id="org_alpha",
        agent_id="shared_agent_name",
        category=TaskCategory.REASONING,
        tier=ModelTier.FRONTIER,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("1.05000000"),
        input_tokens=1000,
        output_tokens=500,
    )

    alpha_dec = thermostat.evaluate_request(
        request_id="alpha-req-2",
        org_id="org_alpha",
        agent_id="shared_agent_name",
        prompt="Analyze quarterly balance sheet.",
        requested_tier=ModelTier.FRONTIER,
    )
    assert not alpha_dec.allowed
    assert alpha_dec.action == ThermostatAction.BUDGET_CIRCUIT_BREAKER

    beta_dec = thermostat.evaluate_request(
        request_id="beta-req-1",
        org_id="org_beta",
        agent_id="shared_agent_name",
        prompt="Analyze quarterly balance sheet.",
        requested_tier=ModelTier.FRONTIER,
    )
    assert beta_dec.allowed
    assert beta_dec.action == ThermostatAction.ALLOW_REQUESTED_TIER
    assert beta_dec.approved_tier == ModelTier.FRONTIER
    assert beta_dec.remaining_budget_usd == Decimal("1.00000000")


def test_tb3_tenant_isolation_loop_breaker_and_semantic_cache_no_cross_tenant_bleed() -> None:
    """Loop circuit breaker and semantic prompt cache are strictly isolated per org_id."""
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_alpha",
                agent_id="worker_bot",
                hourly_budget_usd=Decimal("5.00000000"),
                max_loop_repeats=3,
                enable_semantic_cache=True,
            ),
            BudgetPolicy(
                org_id="org_beta",
                agent_id="worker_bot",
                hourly_budget_usd=Decimal("5.00000000"),
                max_loop_repeats=3,
                enable_semantic_cache=True,
            ),
        ]
    )

    shared_prompt = "SELECT secret_api_token FROM tenant_credentials WHERE id = 1;"
    thermostat.record_outcome(
        request_id="alpha-cache-seed",
        org_id="org_alpha",
        agent_id="worker_bot",
        category=TaskCategory.CODE_SQL,
        tier=ModelTier.MID,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("0.01000000"),
        input_tokens=50,
        output_tokens=20,
        prompt=shared_prompt,
        response_text='{"secret": "ALPHA_CONFIDENTIAL_SECRET_999"}',
    )

    # First 2 calls for org_alpha hit org_alpha's semantic cache; 3rd trips loop circuit breaker
    d1 = thermostat.evaluate_request(
        request_id="alpha-loop-1",
        org_id="org_alpha",
        agent_id="worker_bot",
        prompt=shared_prompt,
        requested_tier=ModelTier.MID,
    )
    assert d1.action == ThermostatAction.CACHE_HIT
    assert d1.cached_response == '{"secret": "ALPHA_CONFIDENTIAL_SECRET_999"}'

    d2 = thermostat.evaluate_request(
        request_id="alpha-loop-2",
        org_id="org_alpha",
        agent_id="worker_bot",
        prompt=shared_prompt,
        requested_tier=ModelTier.MID,
    )
    assert d2.action == ThermostatAction.CACHE_HIT

    d3 = thermostat.evaluate_request(
        request_id="alpha-loop-3",
        org_id="org_alpha",
        agent_id="worker_bot",
        prompt=shared_prompt,
        requested_tier=ModelTier.MID,
    )
    assert not d3.allowed
    assert d3.action == ThermostatAction.LOOP_CIRCUIT_BREAKER

    # org_beta sending the exact same prompt with the exact same agent_id must NOT get
    # org_alpha's cached response and must NOT be blocked by org_alpha's loop breaker!
    beta_dec = thermostat.evaluate_request(
        request_id="beta-fresh-1",
        org_id="org_beta",
        agent_id="worker_bot",
        prompt=shared_prompt,
        requested_tier=ModelTier.MID,
    )
    assert beta_dec.allowed
    assert beta_dec.action == ThermostatAction.ALLOW_REQUESTED_TIER
    assert beta_dec.cached_response is None


def test_tb3_schema_strictness_forbids_extra_fields_and_spoofed_attributes() -> None:
    """Domain schemas enforce ConfigDict(extra='forbid', frozen=True) against attribute injection."""
    with pytest.raises(ValidationError):
        WorkloadItem.model_validate(
            {
                "request_id": "req-spoof",
                "org_id": "org_alpha",
                "agent_id": "agent_1",
                "category": "classification",
                "prompt": "Hello",
                "features": {"prompt_tokens_est": 10},
                "evaluators": [{"eval_type": "exact_match", "expected_text": "OK"}],
                "is_admin_override": True,
            }
        )


# ---------------------------------------------------------------------------
# 2. SSRF & Credential Egress Protection (TB4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_url", "api_key"),
    [
        ("ftp://files.internal.corp/v1", None),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
        ("https:///v1/chat/completions", "sk-secret"),
        ("http://169.254.169.254/latest/meta-data", "sk-secret"),
        ("http://remote-llm.example.com/v1", "sk-live-credential"),
    ],
)
def test_tb4_ssrf_and_plaintext_credential_egress_rejected(
    bad_url: str, api_key: str | None
) -> None:
    """validate_endpoint_security and OpenAICompatibleAdapter reject unsafe schemes and plaintext remote auth."""
    with pytest.raises(ValueError):
        validate_endpoint_security(bad_url, api_key)
    with pytest.raises(ValueError):
        OpenAICompatibleAdapter(base_url=bad_url, api_key=api_key)


@pytest.mark.parametrize(
    ("safe_url", "api_key"),
    [
        ("http://localhost:11434/v1", "ollama-local-key"),
        ("http://127.0.0.1:8000/v1", "local-dev-key"),
        ("http://[::1]:8080/v1", "ipv6-local-key"),
        ("https://api.openai.com/v1", "sk-prod-key"),
    ],
)
def test_tb4_loopback_http_and_https_allowed(safe_url: str, api_key: str) -> None:
    """Loopback HTTP and remote HTTPS endpoints are permitted with API keys."""
    validate_endpoint_security(safe_url, api_key)
    adapter = OpenAICompatibleAdapter(base_url=safe_url, api_key=api_key)
    assert adapter is not None


def test_tb4_open_redirect_301_302_not_followed() -> None:
    """OpenAICompatibleAdapter sets follow_redirects=False and marks 301/302 redirects as UNKNOWN errors."""
    redirect_attempts: list[str] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        redirect_attempts.append(str(request.url))
        if "evil.attacker.example" in str(request.url):
            pytest.fail("Adapter followed redirect to external untrusted host!")
        return httpx.Response(
            status_code=302,
            headers={"Location": "https://evil.attacker.example/exfiltrate"},
            json={"error": "redirecting"},
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key="sk-sensitive-token",
        max_retries=0,
        transport=transport,
    )
    catalog = load_bundled_catalog()
    workload = generate_bundled_workload()[:1]
    traces = asyncio.run(adapter.execute_workload(workload, catalog))

    assert len(redirect_attempts) == 3  # 1 request across 3 tiers, never following the 302
    for tier in ModelTier:
        trace = traces[(workload[0].request_id, tier)]
        assert trace.tokens_provenance == ProvenanceTag.UNKNOWN
        assert trace.cost_provenance == ProvenanceTag.UNKNOWN
        assert trace.error_code == "unexpected_http_302"


# ---------------------------------------------------------------------------
# 3. Evaluator Type Confusion & ReDoS (TB3)
# ---------------------------------------------------------------------------


def test_tb3_evaluator_boolean_int_type_confusion_blocked() -> None:
    """Python's isinstance(True, int) trap must NOT allow booleans to pass 'int' or 'number' checks."""
    schema_rule = EvalRule(
        eval_type=EvalType.JSON_SCHEMA,
        schema_fields={"count": "int", "amount": "number", "flag": "bool"},
    )
    passed, score, reason = evaluate_single_rule(
        schema_rule,
        json.dumps({"count": True, "amount": False, "flag": True}),
        latency_ms=100.0,
    )
    assert not passed
    assert score == pytest.approx(1.0 / 3.0, abs=1e-5)
    assert reason is not None
    assert "key 'count' failed type 'int'" in reason
    assert "key 'amount' failed type 'number'" in reason

    pred_rule = EvalRule(
        eval_type=EvalType.JSON_FIELD_PREDICATE,
        predicates={"count": {"min": 0, "max": 10}},
    )
    pred_ok_true, _, pred_reason_true = evaluate_single_rule(
        pred_rule, json.dumps({"count": True}), latency_ms=50.0
    )
    assert not pred_ok_true
    assert pred_reason_true is not None

    pred_ok_false, _, pred_reason_false = evaluate_single_rule(
        pred_rule, json.dumps({"count": False}), latency_ms=50.0
    )
    assert not pred_ok_false
    assert pred_reason_false is not None


@pytest.mark.parametrize(
    "unsafe_pattern",
    [
        "(x+)+",
        "([a-z]*)*",
        "(a|aa+)+",
        "(.+)+",
        "a" * (MAX_REGEX_PATTERN_LENGTH + 1),
    ],
)
def test_tb3_redos_nested_quantifiers_and_overlong_patterns_rejected(
    unsafe_pattern: str,
) -> None:
    """validate_safe_regex and evaluate_single_rule reject ReDoS nested quantifiers and >256 char patterns."""
    with pytest.raises(ValueError):
        validate_safe_regex(unsafe_pattern)

    rule = EvalRule(eval_type=EvalType.REGEX_MATCH, pattern=unsafe_pattern)
    ok, score, reason = evaluate_single_rule(rule, "xxxx", latency_ms=10.0)
    assert not ok
    assert score == 0.0
    assert reason is not None
    assert "invalid/unsafe pattern" in reason


# ---------------------------------------------------------------------------
# 4. Receipt Integrity & NaN/Inf Smuggling (TB5a)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("non_finite_literal", ["NaN", "Infinity", "-Infinity"])
def test_tb5a_receipt_rejects_nan_and_infinity_smuggling(
    tmp_path: Path, non_finite_literal: str
) -> None:
    """verify_receipt_file, verify_receipt_dict, and quantize_usd reject NaN, Infinity, and -Infinity."""
    with pytest.raises(ValueError):
        quantize_usd(float(non_finite_literal))

    receipt = _build_sample_receipt()
    raw_dict: dict[str, Any] = json.loads(receipt.model_dump_json())
    raw_dict["policy_results"][0]["pass_rate"] = float(non_finite_literal)

    ok_dict, msg_dict = verify_receipt_dict(raw_dict)
    assert not ok_dict
    assert "Non-finite float" in msg_dict or "Canonicalization error" in msg_dict

    # Also test raw JSON file containing bare NaN / Infinity token
    valid_json_text = receipt.model_dump_json()
    corrupted_json_text = valid_json_text.replace(
        '"p95_latency_ms":', f'"smuggled":{non_finite_literal},"p95_latency_ms":', 1
    )
    corrupted_path = tmp_path / f"corrupted_{non_finite_literal}.json"
    corrupted_path.write_text(corrupted_json_text, encoding="utf-8")

    ok_file, msg_file = verify_receipt_file(corrupted_path)
    assert not ok_file
    assert "Non-finite constant" in msg_file


def test_tb5a_receipt_detects_tampered_matrix_invariants_and_missing_integrity() -> None:
    """verify_receipt_dict rejects missing integrity envelopes, tampered cells, and broken request-count invariants."""
    receipt = _build_sample_receipt()
    base_dict: dict[str, Any] = json.loads(receipt.model_dump_json())

    # 1. Verify untampered baseline passes
    ok_clean, _ = verify_receipt_dict(base_dict)
    assert ok_clean

    # 2. Missing integrity envelope
    no_integrity = {k: v for k, v in base_dict.items() if k != "integrity"}
    ok_missing, msg_missing = verify_receipt_dict(no_integrity)
    assert not ok_missing
    assert "Missing or malformed integrity.digest" in msg_missing

    # 3. Tampered evaluation_matrix cell without recomputing digest
    tampered_cell = json.loads(receipt.model_dump_json())
    tampered_cell["evaluation_matrix"][0]["passed"] = not tampered_cell["evaluation_matrix"][0][
        "passed"
    ]
    ok_cell, msg_cell = verify_receipt_dict(tampered_cell)
    assert not ok_cell
    assert "SHA-256 digest mismatch" in msg_cell

    # 4. Tampered complete_case_requests invariant EVEN IF attacker recomputes SHA-256 digest
    tampered_inv = json.loads(receipt.model_dump_json())
    tampered_inv["complete_case_requests"] = tampered_inv["total_workload_requests"] + 5
    without_int = {k: v for k, v in tampered_inv.items() if k != "integrity"}
    tampered_inv["integrity"]["digest"] = compute_receipt_digest(without_int)

    ok_inv, msg_inv = verify_receipt_dict(tampered_inv)
    assert not ok_inv
    assert "Invariant violation" in msg_inv


# ---------------------------------------------------------------------------
# 5. HTML/SVG Script Breakout & Line-Separator Injection (TB5a)
# ---------------------------------------------------------------------------


def test_tb5a_escape_json_for_script_tag_neutralizes_breakout_vectors() -> None:
    """escape_json_for_script_tag neutralizes </script>, <!--, -->, \\u2028, \\u2029, and &."""
    hostile_payload = '</script><script>alert("xss")</script><!-- comment -->\u2028\u2029&amp;'
    escaped = escape_json_for_script_tag(json.dumps({"payload": hostile_payload}))

    assert "</script>" not in escaped
    assert "<!--" not in escaped
    assert "-->" not in escaped
    assert "\u2028" not in escaped
    assert "\u2029" not in escaped
    assert "&" not in escaped

    assert "\\u003c/script\\u003e" in escaped
    assert "\\u2028" in escaped
    assert "\\u2029" in escaped
    assert "\\u0026" in escaped

    # Round-trip via JSON.parse equivalent (json.loads) restores exact original string
    restored = json.loads(escaped)
    assert restored["payload"] == hostile_payload


def test_tb5a_render_pareto_explorer_html_blocks_xss_in_workload_name_and_markdown() -> None:
    """render_pareto_explorer_html never emits raw unescaped user strings into HTML or script contexts."""
    hostile_name = "</script><img src=x onerror=alert(1)><!--\u2028\u2029-->"
    receipt = _build_sample_receipt(workload_name=hostile_name)
    md = render_receipt_markdown(receipt)
    html_doc = render_pareto_explorer_html(receipt, md)

    # The raw hostile_name must never appear unescaped in the HTML document
    assert "</script><img src=x onerror=alert(1)>" not in html_doc
    assert "<img src=x onerror=alert(1)>" not in html_doc

    # Extract embedded JSON script tag bodies and verify zero unescaped breakout or line-separator chars
    pareto_script_start = html_doc.index('<script id="pareto-data" type="application/json">')
    pareto_script_end = html_doc.index("</script>", pareto_script_start)
    pareto_script_body = html_doc[pareto_script_start:pareto_script_end]
    assert "\u2028" not in pareto_script_body
    assert "\u2029" not in pareto_script_body
    assert "<!--" not in pareto_script_body
    assert "-->" not in pareto_script_body

    md_script_start = html_doc.index('<script id="receipt-md-data" type="application/json">')
    md_script_end = html_doc.index("</script>", md_script_start)
    md_script_body = html_doc[md_script_start:md_script_end]
    assert "\u2028" not in md_script_body
    assert "\u2029" not in md_script_body
    assert "<!--" not in md_script_body
    assert "-->" not in md_script_body

    # Title and header must contain HTML-entity escaped representation
    assert "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in html_doc
    # Embedded JSON script blocks must only close with their own legitimate </script> tag (3 script blocks total)
    assert html_doc.count("</script>") == 3
