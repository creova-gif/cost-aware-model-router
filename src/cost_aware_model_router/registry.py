from __future__ import annotations

import asyncio
from decimal import Decimal

from cost_aware_model_router.adapters import ProviderAdapter, sha256_hex
from cost_aware_model_router.schemas import (
    TIER_ORDER,
    ExecutionTrace,
    ModelTier,
    PricingCatalog,
    ProvenanceTag,
    WorkloadItem,
)


def _missing_adapter_trace(
    request_id: str,
    tier: ModelTier,
    model_id: str,
    prompt_sha256: str,
) -> ExecutionTrace:
    """Build a typed UNKNOWN-provenance trace when no adapter or catalog spec is available."""
    return ExecutionTrace(
        request_id=request_id,
        tier=tier,
        model_id=model_id,
        response_text="",
        prompt_sha256=prompt_sha256,
        response_sha256=sha256_hex(""),
        input_tokens=0,
        output_tokens=0,
        latency_ms=0.0,
        cost_usd=Decimal("0"),
        tokens_provenance=ProvenanceTag.UNKNOWN,
        latency_provenance=ProvenanceTag.UNKNOWN,
        cost_provenance=ProvenanceTag.UNKNOWN,
        error_code="missing_provider_adapter",
    )


class MultiProviderAdapter:
    """Routes each ModelTier to heterogeneous ProviderAdapter backends.

    Resolution precedence for a tier:
    1. ``tier_adapters[tier]`` (explicit tier override)
    2. ``provider_adapters[catalog.models[tier].provider]`` (provider-name routing)
    3. ``fallback_adapter`` (optional default)

    If a tier's ``TierModelSpec`` is missing from ``PricingCatalog`` or no adapter
    resolves for it, a typed ``ExecutionTrace`` with ``ProvenanceTag.UNKNOWN`` and
    ``error_code="missing_provider_adapter"`` is emitted.
    """

    def __init__(
        self,
        tier_adapters: dict[ModelTier, ProviderAdapter] | None = None,
        provider_adapters: dict[str, ProviderAdapter] | None = None,
        fallback_adapter: ProviderAdapter | None = None,
    ) -> None:
        self._tier_adapters: dict[ModelTier, ProviderAdapter] = dict(tier_adapters or {})
        self._provider_adapters: dict[str, ProviderAdapter] = dict(provider_adapters or {})
        self._fallback_adapter: ProviderAdapter | None = fallback_adapter

    def resolve_adapter(self, tier: ModelTier, catalog: PricingCatalog) -> ProviderAdapter | None:
        """Resolve the backend adapter for ``tier`` given ``catalog``, or ``None``."""
        spec = catalog.models.get(tier)
        if spec is None:
            return None
        if tier in self._tier_adapters:
            return self._tier_adapters[tier]
        if spec.provider in self._provider_adapters:
            return self._provider_adapters[spec.provider]
        return self._fallback_adapter

    async def execute_workload(
        self, workload: list[WorkloadItem], catalog: PricingCatalog
    ) -> dict[tuple[str, ModelTier], ExecutionTrace]:
        """Execute all ``(WorkloadItem, ModelTier)`` pairs concurrently across assigned adapters."""
        if not workload:
            return {}

        results: dict[tuple[str, ModelTier], ExecutionTrace] = {}
        # Group tiers by adapter identity so shared adapters execute once with pooled clients/caps.
        adapter_groups: dict[int, tuple[ProviderAdapter, list[ModelTier]]] = {}
        missing_tiers: list[tuple[ModelTier, str]] = []

        for tier in TIER_ORDER:
            spec = catalog.models.get(tier)
            adapter = self.resolve_adapter(tier, catalog)
            if spec is None or adapter is None:
                model_id = spec.model_id if spec is not None else "unconfigured"
                missing_tiers.append((tier, model_id))
                continue
            key = id(adapter)
            if key not in adapter_groups:
                adapter_groups[key] = (adapter, [])
            adapter_groups[key][1].append(tier)

        for item in workload:
            p_hash = sha256_hex(item.prompt)
            for tier, model_id in missing_tiers:
                results[(item.request_id, tier)] = _missing_adapter_trace(
                    request_id=item.request_id,
                    tier=tier,
                    model_id=model_id,
                    prompt_sha256=p_hash,
                )

        if not adapter_groups:
            return results

        coros = []
        group_tiers: list[list[ModelTier]] = []
        for adapter, tiers in adapter_groups.values():
            sub_catalog = PricingCatalog(
                catalog_version=catalog.catalog_version,
                models={t: catalog.models[t] for t in tiers},
            )
            coros.append(adapter.execute_workload(workload, sub_catalog))
            group_tiers.append(tiers)

        adapter_outputs = await asyncio.gather(*coros)

        for tiers, output in zip(group_tiers, adapter_outputs, strict=True):
            for item in workload:
                for tier in tiers:
                    trace = output.get((item.request_id, tier))
                    if trace is None:
                        spec = catalog.models[tier]
                        trace = _missing_adapter_trace(
                            request_id=item.request_id,
                            tier=tier,
                            model_id=spec.model_id,
                            prompt_sha256=sha256_hex(item.prompt),
                        )
                    results[(item.request_id, tier)] = trace

        return results
