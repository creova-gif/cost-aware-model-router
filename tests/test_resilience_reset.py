from __future__ import annotations

from cost_aware_model_router.resilience import (
    CircuitBreakerConfig,
    CircuitState,
    ProviderCircuitBreaker,
)
from cost_aware_model_router.schemas import ModelTier


class FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_reset_single_tier_closes_only_target_tier() -> None:
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=30.0,
            success_threshold=2,
        ),
        clock=clock,
    )

    breaker.record_failure(ModelTier.COMPACT)
    breaker.record_failure(ModelTier.COMPACT)
    breaker.record_failure(ModelTier.MID)
    breaker.record_failure(ModelTier.MID)

    assert breaker.state(ModelTier.COMPACT) == CircuitState.OPEN
    assert breaker.state(ModelTier.MID) == CircuitState.OPEN
    assert breaker.allow_request(ModelTier.COMPACT) is False
    assert breaker.allow_request(ModelTier.MID) is False

    breaker.reset(ModelTier.COMPACT)

    assert breaker.state(ModelTier.COMPACT) == CircuitState.CLOSED
    assert breaker.allow_request(ModelTier.COMPACT) is True
    assert breaker.state(ModelTier.MID) == CircuitState.OPEN
    assert breaker.allow_request(ModelTier.MID) is False
    assert breaker.state(ModelTier.FRONTIER) == CircuitState.CLOSED

    # Confirm COMPACT failure counter was reset (1 failure < threshold 2 stays CLOSED).
    breaker.record_failure(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.CLOSED


def test_reset_all_closes_open_and_half_open_tiers_and_resets_counters() -> None:
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=30.0,
            success_threshold=2,
        ),
        clock=clock,
    )

    # Drive COMPACT to OPEN, then advance clock to HALF_OPEN with 1 partial success.
    breaker.record_failure(ModelTier.COMPACT)
    breaker.record_failure(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.OPEN
    clock.advance(30.0)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.HALF_OPEN
    breaker.record_success(ModelTier.COMPACT)
    assert breaker.state(ModelTier.COMPACT) == CircuitState.HALF_OPEN

    # Drive MID to OPEN.
    breaker.record_failure(ModelTier.MID)
    breaker.record_failure(ModelTier.MID)
    assert breaker.state(ModelTier.MID) == CircuitState.OPEN

    # Leave FRONTIER CLOSED with 1 recorded failure (threshold is 2).
    breaker.record_failure(ModelTier.FRONTIER)
    assert breaker.state(ModelTier.FRONTIER) == CircuitState.CLOSED

    breaker.reset()

    assert breaker.snapshot() == {
        ModelTier.COMPACT: CircuitState.CLOSED,
        ModelTier.MID: CircuitState.CLOSED,
        ModelTier.FRONTIER: CircuitState.CLOSED,
    }
    for tier in ModelTier:
        assert breaker.allow_request(tier) is True

    # Failure counters must be zeroed: a single failure on any tier keeps it CLOSED.
    for tier in ModelTier:
        breaker.record_failure(tier)
        assert breaker.state(tier) == CircuitState.CLOSED
        breaker.record_failure(tier)
        assert breaker.state(tier) == CircuitState.OPEN
