from __future__ import annotations

import asyncio
import threading
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cost_aware_model_router.adapters import load_bundled_catalog, sha256_hex
from cost_aware_model_router.resilience import (
    BoundedTTLCache,
    CircuitBreakerConfig,
    CircuitState,
    FallbackChainAdapter,
    ProviderCircuitBreaker,
)
from cost_aware_model_router.schemas import (
    ComplexityFeatures,
    EvalRule,
    EvalType,
    ExecutionTrace,
    ModelTier,
    PricingCatalog,
    ProvenanceTag,
    TaskCategory,
    WorkloadItem,
)


class FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_item(request_id: str = "req-001", prompt: str = "Classify hello") -> WorkloadItem:
    return WorkloadItem(
        request_id=request_id,
        org_id="org_test",
        agent_id="agent_test",
        category=TaskCategory.CLASSIFICATION,
        prompt=prompt,
        features=ComplexityFeatures(prompt_tokens_est=32),
        evaluators=[EvalRule(eval_type=EvalType.EXACT_MATCH, expected_text="HELLO")],
    )


def _make_healthy_trace(item: WorkloadItem, tier: ModelTier, model_id: str) -> ExecutionTrace:
    resp = f"ok:{tier.value}"
    return ExecutionTrace(
        request_id=item.request_id,
        tier=tier,
        model_id=model_id,
        response_text=resp,
        prompt_sha256=sha256_hex(item.prompt),
        response_sha256=sha256_hex(resp),
        input_tokens=32,
        output_tokens=8,
        latency_ms=42.0,
        cost_usd=Decimal("0.00010000"),
        tokens_provenance=ProvenanceTag.MEASURED,
        latency_provenance=ProvenanceTag.MEASURED,
        cost_provenance=ProvenanceTag.MEASURED,
    )


def _make_error_trace(
    item: WorkloadItem, tier: ModelTier, model_id: str, error_code: str = "provider_503"
) -> ExecutionTrace:
    return ExecutionTrace(
        request_id=item.request_id,
        tier=tier,
        model_id=model_id,
        response_text="",
        prompt_sha256=sha256_hex(item.prompt),
        response_sha256=sha256_hex(""),
        input_tokens=0,
        output_tokens=0,
        latency_ms=0.0,
        cost_usd=Decimal("0"),
        tokens_provenance=ProvenanceTag.UNKNOWN,
        latency_provenance=ProvenanceTag.UNKNOWN,
        cost_provenance=ProvenanceTag.UNKNOWN,
        error_code=error_code,
    )


class StubAdapter:
    def __init__(self, failing_tiers: set[ModelTier]) -> None:
        self.failing_tiers = failing_tiers
        self.seen_catalog_tiers: list[set[ModelTier]] = []

    async def execute_workload(
        self, workload: list[WorkloadItem], catalog: PricingCatalog
    ) -> dict[tuple[str, ModelTier], ExecutionTrace]:
        self.seen_catalog_tiers.append(set(catalog.models.keys()))
        results: dict[tuple[str, ModelTier], ExecutionTrace] = {}
        for item in workload:
            for tier, spec in catalog.models.items():
                if tier in self.failing_tiers:
                    results[(item.request_id, tier)] = _make_error_trace(item, tier, spec.model_id)
                else:
                    results[(item.request_id, tier)] = _make_healthy_trace(item, tier, spec.model_id)
        return results


def test_circuit_breaker_config_is_frozen_and_forbids_extra() -> None:
    cfg = CircuitBreakerConfig()
    assert cfg.failure_threshold == 3
    assert cfg.recovery_timeout_seconds == 30.0
    assert cfg.success_threshold == 2

    with pytest.raises(ValidationError):
        CircuitBreakerConfig(unexpected_field=True)  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        cfg.failure_threshold = 5


def test_circuit_breaker_full_lifecycle_transitions() -> None:
    clock = FakeClock(initial=10.0)
    cfg = CircuitBreakerConfig(
        failure_threshold=3,
        recovery_timeout_seconds=30.0,
        success_threshold=2,
    )
    breaker = ProviderCircuitBreaker(config=cfg, clock=clock)

    assert breaker.state(ModelTier.COMPACT) == CircuitState.CLOSED
    assert breaker.allow_request(ModelTier.COMPACT) is True

    # Two failures keep circuit CLOSED; a success resets failure streak
    breaker.record_failure(ModelTier.COMPACT)
    breaker.record_failure(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.CLOSED
    breaker.record_success(ModelTier.COMPACT)

    breaker.record_failure(ModelTier.COMPACT)
    breaker.record_failure(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.CLOSED

    # Third consecutive failure trips CLOSED -> OPEN
    breaker.record_failure(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.OPEN
    assert breaker.allow_request(ModelTier.COMPACT) is False

    # Other tiers remain unaffected
    assert breaker.state(ModelTier.MID) == CircuitState.CLOSED
    assert breaker.allow_request(ModelTier.MID) is True

    # Before timeout expires, still OPEN
    clock.advance(29.9)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.OPEN
    assert breaker.allow_request(ModelTier.COMPACT) is False

    # At timeout, OPEN -> HALF_OPEN
    clock.advance(0.1)
    assert breaker.allow_request(ModelTier.COMPACT) is True
    assert breaker.state(ModelTier.COMPACT) == CircuitState.HALF_OPEN

    # One success is not enough when success_threshold=2
    breaker.record_success(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.HALF_OPEN

    # Second success closes circuit HALF_OPEN -> CLOSED
    breaker.record_success(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.CLOSED
    assert breaker.allow_request(ModelTier.COMPACT) is True


def test_circuit_breaker_half_open_failure_reopens() -> None:
    clock = FakeClock(initial=0.0)
    cfg = CircuitBreakerConfig(
        failure_threshold=1,
        recovery_timeout_seconds=10.0,
        success_threshold=2,
    )
    breaker = ProviderCircuitBreaker(config=cfg, clock=clock)

    breaker.record_failure(ModelTier.MID)
    assert breaker.state(ModelTier.MID) == CircuitState.OPEN

    clock.advance(10.0)
    assert breaker.state(ModelTier.MID) == CircuitState.HALF_OPEN

    breaker.record_failure(ModelTier.MID)
    assert breaker.state(ModelTier.MID) == CircuitState.OPEN
    assert breaker.allow_request(ModelTier.MID) is False


def test_circuit_breaker_thread_safety() -> None:
    breaker = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(failure_threshold=100, recovery_timeout_seconds=1.0)
    )
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(50):
                breaker.allow_request(ModelTier.COMPACT)
                breaker.record_failure(ModelTier.COMPACT)
                breaker.record_success(ModelTier.MID)
                breaker.state(ModelTier.COMPACT)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_fallback_chain_adapter_falls_back_compact_to_mid_to_frontier() -> None:
    catalog = load_bundled_catalog()
    item = _make_item()
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=30.0),
        clock=clock,
    )
    stub = StubAdapter(failing_tiers={ModelTier.COMPACT, ModelTier.MID})
    adapter = FallbackChainAdapter(inner=stub, breaker=breaker)

    traces = asyncio.run(adapter.execute_workload([item], catalog))

    compact_trace = traces[(item.request_id, ModelTier.COMPACT)]
    mid_trace = traces[(item.request_id, ModelTier.MID)]
    frontier_trace = traces[(item.request_id, ModelTier.FRONTIER)]

    assert compact_trace.tier == ModelTier.FRONTIER
    assert compact_trace.error_code is None
    assert compact_trace.tokens_provenance == ProvenanceTag.MEASURED
    assert mid_trace.tier == ModelTier.FRONTIER
    assert frontier_trace.tier == ModelTier.FRONTIER

    assert breaker.state(ModelTier.COMPACT) == CircuitState.OPEN
    assert breaker.state(ModelTier.MID) == CircuitState.OPEN
    assert breaker.state(ModelTier.FRONTIER) == CircuitState.CLOSED


def test_fallback_chain_adapter_skips_open_circuit_and_recovers() -> None:
    catalog = load_bundled_catalog()
    item = _make_item()
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=30.0),
        clock=clock,
    )
    breaker.record_failure(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.OPEN

    stub = StubAdapter(failing_tiers=set())
    adapter = FallbackChainAdapter(inner=stub, breaker=breaker)

    traces = asyncio.run(adapter.execute_workload([item], catalog))

    # Inner adapter was never asked to run COMPACT while its circuit was OPEN
    assert stub.seen_catalog_tiers == [{ModelTier.MID, ModelTier.FRONTIER}]
    compact_trace = traces[(item.request_id, ModelTier.COMPACT)]
    assert compact_trace.tier == ModelTier.MID
    assert compact_trace.error_code is None


def test_fallback_chain_adapter_emits_unknown_when_all_tiers_unavailable() -> None:
    catalog = load_bundled_catalog()
    item = _make_item()
    breaker = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=30.0)
    )
    stub = StubAdapter(failing_tiers={ModelTier.COMPACT, ModelTier.MID, ModelTier.FRONTIER})
    adapter = FallbackChainAdapter(inner=stub, breaker=breaker)

    traces = asyncio.run(adapter.execute_workload([item], catalog))

    for tier in (ModelTier.COMPACT, ModelTier.MID, ModelTier.FRONTIER):
        trace = traces[(item.request_id, tier)]
        assert trace.tier == tier
        assert trace.tokens_provenance == ProvenanceTag.UNKNOWN
        assert trace.latency_provenance == ProvenanceTag.UNKNOWN
        assert trace.cost_provenance == ProvenanceTag.UNKNOWN
        assert trace.error_code == "all_fallback_tiers_unavailable"
        assert trace.cost_usd == Decimal("0.00000000")


def test_bounded_ttl_cache_lru_eviction_and_ttl_and_tenant_isolation() -> None:
    clock = FakeClock(initial=1000.0)
    cache = BoundedTTLCache(max_entries=2, ttl_seconds=60.0, clock=clock)

    h1 = sha256_hex("prompt-1")
    h2 = sha256_hex("prompt-2")
    h3 = sha256_hex("prompt-3")

    cache.set("org_a", h1, "resp_a1")
    cache.set("org_a", h2, "resp_a2")
    assert cache.size() == 2

    # Refresh h1 so h2 becomes least recently used
    assert cache.get("org_a", h1) == "resp_a1"

    # Inserting h3 evicts h2, keeping h1 and h3
    cache.set("org_a", h3, "resp_a3")
    assert cache.size() == 2
    assert cache.get("org_a", h2) is None
    assert cache.get("org_a", h1) == "resp_a1"
    assert cache.get("org_a", h3) == "resp_a3"

    # Tenant isolation: same prompt_hash under org_b is distinct from org_a
    cache.set("org_b", h1, "resp_b1")
    assert cache.get("org_b", h1) == "resp_b1"
    assert cache.get("org_a", h1) is None  # evicted because max_entries=2 (org_a:h3, org_b:h1)

    # TTL expiration
    clock.advance(59.0)
    assert cache.get("org_b", h1) == "resp_b1"
    clock.advance(1.0)
    assert cache.get("org_b", h1) is None
    assert cache.size() == 0


def test_bounded_ttl_cache_rejects_invalid_params() -> None:
    with pytest.raises(ValueError, match="max_entries"):
        BoundedTTLCache(max_entries=0)
    with pytest.raises(ValueError, match="ttl_seconds"):
        BoundedTTLCache(ttl_seconds=0.0)
