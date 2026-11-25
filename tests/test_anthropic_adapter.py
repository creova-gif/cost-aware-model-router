from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from cost_aware_model_router.adapters import (
    AnthropicAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
)
from cost_aware_model_router.schemas import ModelTier, ProvenanceTag


def test_anthropic_adapter_rejects_plaintext_http_with_api_key() -> None:
    with pytest.raises(ValueError, match="plaintext http://"):
        AnthropicAdapter(base_url="http://api.anthropic.com", api_key="sk-ant-secret")
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        AnthropicAdapter(base_url="ftp://api.anthropic.com", api_key="sk-ant-secret")

    # Loopback HTTP and remote HTTPS are permitted
    AnthropicAdapter(base_url="http://127.0.0.1:8080", api_key="sk-local")
    AnthropicAdapter(base_url="https://api.anthropic.com", api_key="sk-ant-live")


def test_anthropic_adapter_normal_messages_execution_and_headers() -> None:
    workload = generate_bundled_workload()[:1]
    catalog = load_bundled_catalog()
    captured_requests: list[tuple[str, dict[str, str], dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured_requests.append((str(request.url), dict(request.headers), payload))
        return httpx.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": '{"customer_id":"cust_001","invoice_total":126.5,"currency":"USD"}',
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    adapter = AnthropicAdapter(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        anthropic_version="2023-06-01",
        max_tokens=512,
        max_concurrency=1,
        transport=httpx.MockTransport(handler),
    )
    traces = asyncio.run(adapter.execute_workload(workload, catalog))

    assert len(captured_requests) == 3
    for url, headers, payload in captured_requests:
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "sk-ant-test"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in headers
        assert payload["max_tokens"] == 512
        assert payload["messages"] == [{"role": "user", "content": workload[0].prompt}]

    for tier in ModelTier:
        trace = traces[("req-001", tier)]
        spec = catalog.models[tier]
        assert trace.input_tokens == 100
        assert trace.output_tokens == 50
        assert trace.tokens_provenance == ProvenanceTag.MEASURED
        assert trace.latency_provenance == ProvenanceTag.MEASURED
        assert trace.cost_provenance == ProvenanceTag.MODELED
        assert trace.cost_usd == spec.compute_cost_usd(100, 50)
        assert trace.error_code is None


def test_anthropic_adapter_missing_usage_emits_unknown_provenance() -> None:
    workload = generate_bundled_workload()[:1]
    catalog = load_bundled_catalog()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}]},
        )

    adapter = AnthropicAdapter(
        api_key="sk-ant-test",
        max_concurrency=1,
        transport=httpx.MockTransport(handler),
    )
    traces = asyncio.run(adapter.execute_workload(workload, catalog))

    for tier in ModelTier:
        trace = traces[("req-001", tier)]
        assert trace.response_text == "ok"
        assert trace.tokens_provenance == ProvenanceTag.UNKNOWN
        assert trace.latency_provenance == ProvenanceTag.MEASURED
        assert trace.cost_provenance == ProvenanceTag.UNKNOWN
        assert trace.cost_usd == Decimal("0")
        assert trace.error_code == "missing_usage_tokens"


def test_anthropic_adapter_401_fail_fast_per_tier() -> None:
    workload = generate_bundled_workload()[:2]
    catalog = load_bundled_catalog()
    call_counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        model = str(body["model"])
        call_counts[model] = call_counts.get(model, 0) + 1
        if model == catalog.models[ModelTier.MID].model_id:
            return httpx.Response(401, json={"error": {"type": "authentication_error"}})
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    adapter = AnthropicAdapter(
        api_key="sk-ant-test",
        max_concurrency=1,
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )
    traces = asyncio.run(adapter.execute_workload(workload, catalog))

    # Mid tier fails fast on req-001 and skips HTTP call for req-002
    assert call_counts[catalog.models[ModelTier.MID].model_id] == 1
    assert traces[("req-001", ModelTier.MID)].error_code == "fatal_http_401"
    assert traces[("req-002", ModelTier.MID)].error_code == "fatal_http_401"
    assert traces[("req-001", ModelTier.MID)].tokens_provenance == ProvenanceTag.UNKNOWN

    # Other tiers still succeed
    assert traces[("req-002", ModelTier.COMPACT)].tokens_provenance == ProvenanceTag.MEASURED
    assert traces[("req-002", ModelTier.FRONTIER)].tokens_provenance == ProvenanceTag.MEASURED


def test_anthropic_adapter_429_retries_with_retry_after() -> None:
    workload = generate_bundled_workload()[:1]
    catalog = load_bundled_catalog()
    attempts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        model = str(body["model"])
        attempts[model] = attempts.get(model, 0) + 1
        if model == catalog.models[ModelTier.COMPACT].model_id and attempts[model] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate_limited"})
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "recovered"}],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        )

    adapter = AnthropicAdapter(
        api_key="sk-ant-test",
        max_concurrency=1,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )
    traces = asyncio.run(adapter.execute_workload(workload, catalog))

    assert attempts[catalog.models[ModelTier.COMPACT].model_id] == 2
    c_trace = traces[("req-001", ModelTier.COMPACT)]
    assert c_trace.response_text == "recovered"
    assert c_trace.tokens_provenance == ProvenanceTag.MEASURED
    assert c_trace.cost_provenance == ProvenanceTag.MODELED


def test_anthropic_adapter_max_cost_usd_circuit_breaker() -> None:
    workload = generate_bundled_workload()[:2]
    catalog = load_bundled_catalog()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 120, "output_tokens": 40},
            },
        )

    adapter = AnthropicAdapter(
        api_key="sk-ant-test",
        max_concurrency=1,
        max_cost_usd=Decimal("0.00200000"),
        transport=httpx.MockTransport(handler),
    )
    traces = asyncio.run(adapter.execute_workload(workload, catalog))

    # req-001 Frontier costs 0.00240000 > 0.00200000 cap, tripping the breaker for req-002
    assert traces[("req-001", ModelTier.FRONTIER)].cost_usd == Decimal("0.00240000")
    assert traces[("req-001", ModelTier.FRONTIER)].cost_provenance == ProvenanceTag.MODELED

    for tier in ModelTier:
        t2 = traces[("req-002", tier)]
        assert t2.tokens_provenance == ProvenanceTag.UNKNOWN
        assert t2.error_code == "max_cost_usd_circuit_breaker_tripped"


def test_anthropic_adapter_blocks_redirects_and_malformed_body() -> None:
    workload = generate_bundled_workload()[:1]
    catalog = load_bundled_catalog()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        model = str(body["model"])
        if model == catalog.models[ModelTier.COMPACT].model_id:
            return httpx.Response(302, headers={"Location": "https://evil.example/v1/messages"})
        if model == catalog.models[ModelTier.MID].model_id:
            return httpx.Response(200, json={"content": []})
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    adapter = AnthropicAdapter(
        api_key="sk-ant-test",
        max_concurrency=1,
        transport=httpx.MockTransport(handler),
    )
    traces = asyncio.run(adapter.execute_workload(workload, catalog))

    assert traces[("req-001", ModelTier.COMPACT)].error_code == "unexpected_http_302"
    assert traces[("req-001", ModelTier.MID)].error_code == "malformed_response_body"
    assert traces[("req-001", ModelTier.FRONTIER)].tokens_provenance == ProvenanceTag.MEASURED
