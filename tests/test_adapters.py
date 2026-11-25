from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    OpenAICompatibleAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
    validate_endpoint_security,
)
from cost_aware_model_router.schemas import ModelTier, ProvenanceTag


def test_endpoint_security_blocks_plaintext_http_auth_to_remote_host() -> None:
    with pytest.raises(ValueError, match="plaintext http://"):
        validate_endpoint_security("http://example.com/v1", "sk-secret-123")
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        validate_endpoint_security("file:///etc/passwd", None)
    # Loopback HTTP (e.g. Ollama) and HTTPS are permitted
    validate_endpoint_security("http://127.0.0.1:11434/v1", "ollama-local")
    validate_endpoint_security("https://api.openai.com/v1", "sk-live-key")


def test_deterministic_fixture_adapter_runs_60_items_across_3_tiers() -> None:
    workload = generate_bundled_workload()
    catalog = load_bundled_catalog()
    assert len(workload) == 60
    adapter = DeterministicFixtureAdapter()
    traces = asyncio.run(adapter.execute_workload(workload, catalog))
    assert len(traces) == 180
    for trace in traces.values():
        assert trace.tokens_provenance == ProvenanceTag.MODELED
        assert trace.cost_provenance == ProvenanceTag.MODELED
        assert trace.cost_usd > Decimal("0")


def test_openai_compatible_adapter_handles_missing_usage_401_fail_fast_and_spend_cap() -> None:
    workload = generate_bundled_workload()[:2]
    catalog = load_bundled_catalog()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        model = body["model"]
        if "flash-lite" in model:
            # Missing usage tokens -> must emit UNKNOWN provenance, never $0.00 MEASURED
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"customer_id":"c1","invoice_total":20,"currency":"USD"}'
                            }
                        }
                    ]
                },
            )
        if "mid" in model:
            # 401 -> fail fast for tier
            return httpx.Response(401, json={"error": "unauthorized"})
        # Frontier -> valid response with usage
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"customer_id":"c1","invoice_total":20,"currency":"USD"}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            },
        )

    transport = httpx.MockTransport(handler)
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        max_concurrency=1,
        max_retries=1,
        max_cost_usd=Decimal("0.00200000"),
        transport=transport,
    )
    traces = asyncio.run(adapter.execute_workload(workload, catalog))

    # Compact missing usage -> tokens_provenance=UNKNOWN, cost_provenance=UNKNOWN
    c_trace = traces[("req-001", ModelTier.COMPACT)]
    assert c_trace.tokens_provenance == ProvenanceTag.UNKNOWN
    assert c_trace.cost_provenance == ProvenanceTag.UNKNOWN
    assert c_trace.error_code == "missing_usage_tokens"

    # Mid 401 -> fatal_http_401
    m_trace = traces[("req-001", ModelTier.MID)]
    assert m_trace.tokens_provenance == ProvenanceTag.UNKNOWN
    assert m_trace.error_code == "fatal_http_401"

    # Frontier req-001 -> MEASURED tokens, MODELED catalog cost (0.00240000 > 0.00200000 cap)
    f_trace_1 = traces[("req-001", ModelTier.FRONTIER)]
    assert f_trace_1.tokens_provenance == ProvenanceTag.MEASURED
    assert f_trace_1.cost_provenance == ProvenanceTag.MODELED
    assert f_trace_1.cost_usd == Decimal("0.00240000")

    # Frontier req-002 -> circuit breaker tripped after req-001 exceeded $0.0020 cap
    f_trace_2 = traces[("req-002", ModelTier.FRONTIER)]
    assert f_trace_2.tokens_provenance == ProvenanceTag.UNKNOWN
    assert f_trace_2.error_code == "max_cost_usd_circuit_breaker_tripped"
