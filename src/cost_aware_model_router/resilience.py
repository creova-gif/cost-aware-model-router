from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cost_aware_model_router.adapters import ProviderAdapter, sha256_hex
from cost_aware_model_router.schemas import (
    TIER_ORDER,
    ExecutionTrace,
    ModelTier,
    PricingCatalog,
    ProvenanceTag,
    WorkloadItem,
)

DEFAULT_FALLBACK_ORDER: dict[ModelTier, tuple[ModelTier, ...]] = {
    ModelTier.COMPACT: (ModelTier.MID, ModelTier.FRONTIER),
    ModelTier.MID: (ModelTier.FRONTIER, ModelTier.COMPACT),
    ModelTier.FRONTIER: (ModelTier.MID, ModelTier.COMPACT),
}


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_threshold: int = Field(default=3, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, ge=0.0)
    success_threshold: int = Field(default=2, ge=1)


@dataclass
class _TierCircuitEntry:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    opened_at: float | None = None


class ProviderCircuitBreaker:
    """Thread-safe per-ModelTier circuit breaker state machine."""

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[ModelTier, _TierCircuitEntry] = {
            tier: _TierCircuitEntry() for tier in ModelTier
        }

    def _refresh_entry_locked(self, entry: _TierCircuitEntry) -> None:
        if entry.state == CircuitState.OPEN and entry.opened_at is not None:
            elapsed = self._clock() - entry.opened_at
            if elapsed >= self._config.recovery_timeout_seconds:
                entry.state = CircuitState.HALF_OPEN
                entry.consecutive_failures = 0
                entry.consecutive_successes = 0
                entry.opened_at = None

    def allow_request(self, tier: ModelTier) -> bool:
        with self._lock:
            entry = self._entries[tier]
            self._refresh_entry_locked(entry)
            return entry.state != CircuitState.OPEN

    def record_success(self, tier: ModelTier) -> None:
        with self._lock:
            entry = self._entries[tier]
            self._refresh_entry_locked(entry)
            if entry.state == CircuitState.HALF_OPEN:
                entry.consecutive_successes += 1
                if entry.consecutive_successes >= self._config.success_threshold:
                    entry.state = CircuitState.CLOSED
                    entry.consecutive_failures = 0
                    entry.consecutive_successes = 0
                    entry.opened_at = None
            elif entry.state == CircuitState.CLOSED:
                entry.consecutive_failures = 0

    def record_failure(self, tier: ModelTier) -> None:
        with self._lock:
            entry = self._entries[tier]
            self._refresh_entry_locked(entry)
            if entry.state == CircuitState.HALF_OPEN:
                entry.state = CircuitState.OPEN
                entry.opened_at = self._clock()
                entry.consecutive_failures = 0
                entry.consecutive_successes = 0
            elif entry.state == CircuitState.CLOSED:
                entry.consecutive_failures += 1
                if entry.consecutive_failures >= self._config.failure_threshold:
                    entry.state = CircuitState.OPEN
                    entry.opened_at = self._clock()
                    entry.consecutive_successes = 0
            elif entry.state == CircuitState.OPEN:
                entry.opened_at = self._clock()

    def state(self, tier: ModelTier) -> CircuitState:
        with self._lock:
            entry = self._entries[tier]
            self._refresh_entry_locked(entry)
            return entry.state

    def snapshot(self) -> dict[ModelTier, CircuitState]:
        with self._lock:
            for entry in self._entries.values():
                self._refresh_entry_locked(entry)
            return {tier: entry.state for tier, entry in self._entries.items()}


def _is_trace_healthy(trace: ExecutionTrace | None) -> bool:
    if trace is None:
        return False
    if trace.error_code is not None:
        return False
    return ProvenanceTag.UNKNOWN not in (
        trace.tokens_provenance,
        trace.latency_provenance,
        trace.cost_provenance,
    )


class FallbackChainAdapter:
    """ProviderAdapter wrapper with circuit-breaker gating and cross-tier fallback chains."""

    def __init__(
        self,
        inner: ProviderAdapter,
        breaker: ProviderCircuitBreaker | None = None,
        fallback_order: dict[ModelTier, tuple[ModelTier, ...]] | None = None,
    ) -> None:
        self._inner = inner
        self._breaker = breaker or ProviderCircuitBreaker()
        self._fallback_order = (
            dict(fallback_order) if fallback_order is not None else dict(DEFAULT_FALLBACK_ORDER)
        )

    @property
    def circuit_breaker(self) -> ProviderCircuitBreaker:
        return self._breaker

    def _candidate_tiers(self, primary_tier: ModelTier) -> tuple[ModelTier, ...]:
        seen: list[ModelTier] = [primary_tier]
        for fallback_tier in self._fallback_order.get(primary_tier, ()):
            if fallback_tier not in seen:
                seen.append(fallback_tier)
        return tuple(seen)

    @staticmethod
    def _unavailable_trace(
        item: WorkloadItem, tier: ModelTier, catalog: PricingCatalog
    ) -> ExecutionTrace:
        model_spec = catalog.models.get(tier)
        model_id = model_spec.model_id if model_spec is not None else "unconfigured"
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
            error_code="all_fallback_tiers_unavailable",
        )

    async def execute_workload(
        self, workload: list[WorkloadItem], catalog: PricingCatalog
    ) -> dict[tuple[str, ModelTier], ExecutionTrace]:
        allowed_models = {
            tier: spec
            for tier, spec in catalog.models.items()
            if self._breaker.allow_request(tier)
        }
        raw_traces: dict[tuple[str, ModelTier], ExecutionTrace] = {}
        if allowed_models:
            filtered_catalog = PricingCatalog(
                catalog_version=catalog.catalog_version,
                models=allowed_models,
            )
            raw_traces = await self._inner.execute_workload(workload, filtered_catalog)

        results: dict[tuple[str, ModelTier], ExecutionTrace] = {}
        for item in workload:
            for tier in TIER_ORDER:
                chosen_trace: ExecutionTrace | None = None
                for candidate_tier in self._candidate_tiers(tier):
                    if not self._breaker.allow_request(candidate_tier):
                        continue
                    candidate_trace = raw_traces.get((item.request_id, candidate_tier))
                    if _is_trace_healthy(candidate_trace):
                        self._breaker.record_success(candidate_tier)
                        chosen_trace = candidate_trace
                        break
                    self._breaker.record_failure(candidate_tier)

                results[(item.request_id, tier)] = (
                    chosen_trace
                    if chosen_trace is not None
                    else self._unavailable_trace(item, tier, catalog)
                )

        return results


class BoundedTTLCache:
    """Thread-safe OrderedDict-backed LRU + TTL cache keyed by (org_id, prompt_sha256)."""

    def __init__(
        self,
        max_entries: int = 1024,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if ttl_seconds <= 0.0:
            raise ValueError("ttl_seconds must be > 0.0")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()

    def _evict_expired_locked(self, now: float) -> None:
        expired_keys = [
            key for key, (_, expires_at) in self._entries.items() if now >= expires_at
        ]
        for key in expired_keys:
            del self._entries[key]

    def get(self, org_id: str, prompt_hash: str) -> str | None:
        with self._lock:
            key = (org_id, prompt_hash)
            entry = self._entries.get(key)
            if entry is None:
                return None
            response_text, expires_at = entry
            if self._clock() >= expires_at:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return response_text

    def set(self, org_id: str, prompt_hash: str, response_text: str) -> None:
        with self._lock:
            now = self._clock()
            self._evict_expired_locked(now)
            key = (org_id, prompt_hash)
            expires_at = now + self._ttl_seconds
            if key in self._entries:
                self._entries[key] = (response_text, expires_at)
                self._entries.move_to_end(key)
                return
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
            self._entries[key] = (response_text, expires_at)

    def size(self) -> int:
        with self._lock:
            self._evict_expired_locked(self._clock())
            return len(self._entries)
