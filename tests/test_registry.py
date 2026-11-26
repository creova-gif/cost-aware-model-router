from __future__ import annotations

import asyncio
import inspect
import json
from decimal import Decimal

import httpx

from cost_aware_model_router.adapters import (
    AnthropicAdapter,
    DeterministicFixtureAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
)
from cost_aware_model_router.registry import MultiProviderAdapter
from cost_aware_model_router.schemas import (
    ExecutionTrace,
    ModelTier,
    PricingCatalog,
    ProvenanceTag,
    TierModelSpec,
    WorkloadItem,
)


def _make_heterogeneous_catalog() -> PricingCatalog:
    return PricingCatalog(
        catalog_version="2026-11-heterogeneous",
        models={
            ModelTier.COMPACT: TierModelSpec(
                tier=ModelTier.COMPACT,
                model_id="qwen2.5:7b-instruct",
                provider="ollama",
                input_usd_per_1m=Decimal("0.00000000"),
                output_usd_per_1m=Decimal("0.00000000"),
                price_source="local-ollama",
            ),
            ModelTier.MID: TierModelSpec(
                tier=ModelTier.MID,
                model_id="gpt-4o-mini",
                provider="openai",
                input_usd_per_1m=Decimal("0.15000000"),
                output_usd_per_1m=Decimal("0.60000000"),
                price_source="openai-catalog",
            ),
            ModelTier.FRONTIER: TierModelSpec(
                tier=ModelTier.FRONTIER,
                model_id="claude-opus-4-1",
                provider="anthropic",
                input_usd_per_1m=Decimal("15.00000000"),
                output_usd_per_1m=Decimal("75.00000000"),
                price_source="anthropic-catalog",
            ),
        },
    )


def _mock_openai_adapter(
    *,
    seen_models: list[str],
    base_url: str = "http://127.0.0.1:11434/v1",
    api_key: str | None = None,
) -> OpenAICompatibleAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_models.append(str(body["model"]))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"customer_id":"cust_001","invoice_total":126.5,"currency":"USD"}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30},
            },
        )

    return OpenAICompatibleAdapter(
        base_url=base_url,
        api_key=api_key,
        transport=httpx.MockTransport(handler),
    )


def _mock_anthropic_adapter(*, seen_models: list[str]) -> AnthropicAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_models.append(str(body["model"]))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": '{"customer_id":"cust_001","invoice_total":126.5,"currency":"USD"}',
                    }
                ],
                "usage": {"input_tokens": 110, "output_tokens": 45},
            },
        )

    return AnthropicAdapter(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        transport=httpx.MockTransport(handler),
    )


def test_protocol_conformance_and_execute_workload_signature() -> None:
    registry = MultiProviderAdapter(fallback_adapter=DeterministicFixtureAdapter())
    # Static ProviderAdapter protocol check (enforced by mypy --strict)
    adapter: ProviderAdapter = registry
    assert hasattr(adapter, "execute_workload")
    assert inspect.iscoroutinefunction(adapter.execute_workload)
    sig = inspect.signature(adapter.execute_workload)
    assert list(sig.parameters.keys()) == ["workload", "catalog"]


def test_heterogeneous_three_tier_routing() -> None:
    workload = generate_bundled_workload()[:3]
    catalog = _make_heterogeneous_catalog()
    ollama_models: list[str] = []
    anthropic_models: list[str] = []

    ollama_adapter = _mock_openai_adapter(seen_models=ollama_models)
    fixture_adapter = DeterministicFixtureAdapter()
    anthropic_adapter = _mock_anthropic_adapter(seen_models=anthropic_models)

    registry: ProviderAdapter = MultiProviderAdapter(
        tier_adapters={
            ModelTier.COMPACT: ollama_adapter,
            ModelTier.MID: fixture_adapter,
            ModelTier.FRONTIER: anthropic_adapter,
        }
    )

    traces = asyncio.run(registry.execute_workload(workload, catalog))
    assert len(traces) == len(workload) * 3

    # Each backend only handled its assigned tier's model
    assert ollama_models == ["qwen2.5:7b-instruct"] * 3
    assert anthropic_models == ["claude-opus-4-1"] * 3

    for item in workload:
        compact = traces[(item.request_id, ModelTier.COMPACT)]
        assert compact.model_id == "qwen2.5:7b-instruct"
        assert compact.tokens_provenance == ProvenanceTag.MEASURED
        assert compact.input_tokens == 100
        assert compact.output_tokens == 30

        mid = traces[(item.request_id, ModelTier.MID)]
        assert mid.model_id == "gpt-4o-mini"
        assert mid.tokens_provenance == ProvenanceTag.MODELED
        assert mid.cost_provenance == ProvenanceTag.MODELED

        frontier = traces[(item.request_id, ModelTier.FRONTIER)]
        assert frontier.model_id == "claude-opus-4-1"
        assert frontier.tokens_provenance == ProvenanceTag.MEASURED
        assert frontier.input_tokens == 110
        assert frontier.output_tokens == 45
        assert frontier.cost_usd > Decimal("0")


def test_provider_routing_and_tier_override_precedence() -> None:
    workload = generate_bundled_workload()[:2]
    catalog = _make_heterogeneous_catalog()

    ollama_models: list[str] = []
    openai_models: list[str] = []
    anthropic_models: list[str] = []

    ollama_adapter = _mock_openai_adapter(seen_models=ollama_models)
    openai_adapter = _mock_openai_adapter(
        seen_models=openai_models,
        base_url="https://api.openai.com/v1",
        api_key="sk-openai-test",
    )
    anthropic_adapter = _mock_anthropic_adapter(seen_models=anthropic_models)
    fixture_override = DeterministicFixtureAdapter()

    registry = MultiProviderAdapter(
        tier_adapters={ModelTier.MID: fixture_override},
        provider_adapters={
            "ollama": ollama_adapter,
            "openai": openai_adapter,
            "anthropic": anthropic_adapter,
        },
    )

    traces = asyncio.run(registry.execute_workload(workload, catalog))
    assert len(traces) == 6
    assert ollama_models == ["qwen2.5:7b-instruct"] * 2
    assert openai_models == []  # Overridden by tier_adapters[ModelTier.MID]
    assert anthropic_models == ["claude-opus-4-1"] * 2
    assert traces[("req-001", ModelTier.MID)].tokens_provenance == ProvenanceTag.MODELED


def test_fallback_adapter_handles_unregistered_tiers() -> None:
    workload = generate_bundled_workload()[:1]
    catalog = _make_heterogeneous_catalog()
    ollama_models: list[str] = []
    ollama_adapter = _mock_openai_adapter(seen_models=ollama_models)

    registry = MultiProviderAdapter(
        tier_adapters={ModelTier.COMPACT: ollama_adapter},
        fallback_adapter=DeterministicFixtureAdapter(),
    )

    traces = asyncio.run(registry.execute_workload(workload, catalog))
    assert traces[("req-001", ModelTier.COMPACT)].tokens_provenance == ProvenanceTag.MEASURED
    assert traces[("req-001", ModelTier.MID)].tokens_provenance == ProvenanceTag.MODELED
    assert traces[("req-001", ModelTier.FRONTIER)].tokens_provenance == ProvenanceTag.MODELED


def test_missing_adapter_and_missing_catalog_spec_emit_unknown_provenance() -> None:
    workload = generate_bundled_workload()[:2]
    full_catalog = _make_heterogeneous_catalog()
    partial_catalog = PricingCatalog(
        catalog_version="2026-11-partial",
        models={
            ModelTier.COMPACT: full_catalog.models[ModelTier.COMPACT],
            ModelTier.FRONTIER: full_catalog.models[ModelTier.FRONTIER],
        },
    )

    ollama_models: list[str] = []
    registry = MultiProviderAdapter(
        tier_adapters={ModelTier.COMPACT: _mock_openai_adapter(seen_models=ollama_models)},
        fallback_adapter=None,
    )

    traces = asyncio.run(registry.execute_workload(workload, partial_catalog))
    assert len(traces) == 6

    for item in workload:
        compact = traces[(item.request_id, ModelTier.COMPACT)]
        assert compact.tokens_provenance == ProvenanceTag.MEASURED
        assert compact.error_code is None

        # Missing TierModelSpec in catalog -> UNKNOWN + missing_provider_adapter
        mid = traces[(item.request_id, ModelTier.MID)]
        assert mid.model_id == "unconfigured"
        assert mid.tokens_provenance == ProvenanceTag.UNKNOWN
        assert mid.latency_provenance == ProvenanceTag.UNKNOWN
        assert mid.cost_provenance == ProvenanceTag.UNKNOWN
        assert mid.cost_usd == Decimal("0")
        assert mid.error_code == "missing_provider_adapter"

        # Present in catalog, but no adapter registered and fallback_adapter=None
        frontier = traces[(item.request_id, ModelTier.FRONTIER)]
        assert frontier.model_id == "claude-opus-4-1"
        assert frontier.tokens_provenance == ProvenanceTag.UNKNOWN
        assert frontier.latency_provenance == ProvenanceTag.UNKNOWN
        assert frontier.cost_provenance == ProvenanceTag.UNKNOWN
        assert frontier.cost_usd == Decimal("0")
        assert frontier.error_code == "missing_provider_adapter"


def test_empty_workload_returns_empty_dict() -> None:
    catalog = load_bundled_catalog()
    registry = MultiProviderAdapter(fallback_adapter=DeterministicFixtureAdapter())
    assert asyncio.run(registry.execute_workload([], catalog)) == {}


def test_concurrent_execution_across_adapters_via_asyncio_gather() -> None:
    workload = generate_bundled_workload()[:1]
    catalog = load_bundled_catalog()
    inner = DeterministicFixtureAdapter()

    class _BarrierAdapter:
        def __init__(self, barrier: asyncio.Barrier) -> None:
            self._barrier = barrier

        async def execute_workload(
            self, wl: list[WorkloadItem], cat: PricingCatalog
        ) -> dict[tuple[str, ModelTier], ExecutionTrace]:
            await asyncio.wait_for(self._barrier.wait(), timeout=2.0)
            return await inner.execute_workload(wl, cat)

    async def _run() -> dict[tuple[str, ModelTier], ExecutionTrace]:
        barrier = asyncio.Barrier(3)
        registry = MultiProviderAdapter(
            tier_adapters={
                ModelTier.COMPACT: _BarrierAdapter(barrier),
                ModelTier.MID: _BarrierAdapter(barrier),
                ModelTier.FRONTIER: _BarrierAdapter(barrier),
            }
        )
        return await registry.execute_workload(workload, catalog)

    traces = asyncio.run(_run())
    assert len(traces) == 3
