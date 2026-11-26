from __future__ import annotations

from decimal import Decimal

import cost_aware_model_router
from cost_aware_model_router import (
    AgentBudgetThermostat,
    BoundedTTLCache,
    BudgetPolicy,
    CircuitBreakerConfig,
    CircuitState,
    FallbackChainAdapter,
    ModelTier,
    ProvenanceTag,
    ProviderCircuitBreaker,
    TaskCategory,
    ThermostatAction,
)


def _record_passed_prompt(
    thermostat: AgentBudgetThermostat,
    *,
    request_id: str,
    prompt: str,
    response_text: str,
) -> None:
    thermostat.record_outcome(
        request_id=request_id,
        org_id="org-test",
        agent_id="agent-test",
        category=TaskCategory.SUMMARIZATION,
        tier=ModelTier.COMPACT,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("0.001"),
        input_tokens=10,
        output_tokens=10,
        prompt=prompt,
        response_text=response_text,
    )


def test_thermostat_evicts_lru_prompt_entry() -> None:
    policy = BudgetPolicy(
        org_id="org-test",
        hourly_budget_usd=Decimal("10.00"),
        max_loop_repeats=10,
    )
    thermostat = AgentBudgetThermostat(policies=[policy], cache_max_entries=2)

    _record_passed_prompt(
        thermostat,
        request_id="req-1",
        prompt="prompt one",
        response_text="resp-1",
    )
    _record_passed_prompt(
        thermostat,
        request_id="req-2",
        prompt="prompt two",
        response_text="resp-2",
    )

    # Touch prompt one so prompt two becomes the least recently used entry.
    hit_one = thermostat.evaluate_request(
        request_id="req-hit-1",
        org_id="org-test",
        agent_id="agent-test",
        prompt="prompt one",
        requested_tier=ModelTier.COMPACT,
    )
    assert hit_one.action == ThermostatAction.CACHE_HIT
    assert hit_one.cached_response == "resp-1"

    # Inserting a third prompt must evict prompt two (LRU), keeping prompt one.
    _record_passed_prompt(
        thermostat,
        request_id="req-3",
        prompt="prompt three",
        response_text="resp-3",
    )

    miss_two = thermostat.evaluate_request(
        request_id="req-miss-2",
        org_id="org-test",
        agent_id="agent-test",
        prompt="prompt two",
        requested_tier=ModelTier.COMPACT,
    )
    assert miss_two.action == ThermostatAction.ALLOW_REQUESTED_TIER
    assert miss_two.cached_response is None

    still_hit_one = thermostat.evaluate_request(
        request_id="req-hit-1b",
        org_id="org-test",
        agent_id="agent-test",
        prompt="prompt one",
        requested_tier=ModelTier.COMPACT,
    )
    assert still_hit_one.action == ThermostatAction.CACHE_HIT
    assert still_hit_one.cached_response == "resp-1"

    hit_three = thermostat.evaluate_request(
        request_id="req-hit-3",
        org_id="org-test",
        agent_id="agent-test",
        prompt="prompt three",
        requested_tier=ModelTier.COMPACT,
    )
    assert hit_three.action == ThermostatAction.CACHE_HIT
    assert hit_three.cached_response == "resp-3"


def test_thermostat_expires_stale_cache_entries_with_fake_clock() -> None:
    now = 100.0

    def fake_clock() -> float:
        return now

    policy = BudgetPolicy(
        org_id="org-test",
        hourly_budget_usd=Decimal("10.00"),
        max_loop_repeats=10,
    )
    thermostat = AgentBudgetThermostat(
        policies=[policy],
        cache_ttl_seconds=10.0,
        clock=fake_clock,
    )

    _record_passed_prompt(
        thermostat,
        request_id="req-1",
        prompt="ttl prompt",
        response_text="cached-value",
    )

    now = 109.9
    fresh_decision = thermostat.evaluate_request(
        request_id="req-2",
        org_id="org-test",
        agent_id="agent-test",
        prompt="ttl prompt",
        requested_tier=ModelTier.COMPACT,
    )
    assert fresh_decision.action == ThermostatAction.CACHE_HIT
    assert fresh_decision.cached_response == "cached-value"

    now = 110.0
    expired_decision = thermostat.evaluate_request(
        request_id="req-3",
        org_id="org-test",
        agent_id="agent-test",
        prompt="ttl prompt",
        requested_tier=ModelTier.COMPACT,
    )
    assert expired_decision.action == ThermostatAction.ALLOW_REQUESTED_TIER
    assert expired_decision.cached_response is None


def test_resilience_symbols_exported_at_package_root() -> None:
    expected_symbols = {
        "BoundedTTLCache": BoundedTTLCache,
        "CircuitBreakerConfig": CircuitBreakerConfig,
        "CircuitState": CircuitState,
        "FallbackChainAdapter": FallbackChainAdapter,
        "ProviderCircuitBreaker": ProviderCircuitBreaker,
    }
    for name, symbol in expected_symbols.items():
        assert getattr(cost_aware_model_router, name) is symbol
        assert name in cost_aware_model_router.__all__
