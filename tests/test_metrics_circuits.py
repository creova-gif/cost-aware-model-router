from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.metrics import render_prometheus_metrics
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.resilience import (
    CircuitBreakerConfig,
    FallbackChainAdapter,
    ProviderCircuitBreaker,
)
from cost_aware_model_router.schemas import ModelTier, PolicySpec
from cost_aware_model_router.server import create_router_http_server


def _policy_by_id(policy_id: str) -> PolicySpec:
    for p in default_policy_suite():
        if p.policy_id == policy_id:
            return p
    raise KeyError(policy_id)


@contextmanager
def _running_server(gateway: OpenAIRouterGateway) -> Iterator[tuple[ThreadingHTTPServer, str]]:
    server = create_router_http_server(gateway, host="127.0.0.1", port=0)
    raw_host, port = server.server_address[:2]
    host = raw_host.decode("utf-8") if isinstance(raw_host, bytes) else raw_host
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_render_prometheus_metrics_circuit_states_and_escaping() -> None:
    output = render_prometheus_metrics(
        [],
        circuits={
            "compact": "closed",
            "mid": "half_open",
            "frontier": "open",
            'tier"with\\slash\nline': "unrecognized",
        },
    )

    assert (
        "# HELP cost_router_circuit_state "
        "Provider circuit breaker state (0=closed, 1=half_open, 2=open)."
    ) in output
    assert "# TYPE cost_router_circuit_state gauge" in output
    assert 'cost_router_circuit_state{tier="compact"} 0' in output
    assert 'cost_router_circuit_state{tier="mid"} 1' in output
    assert 'cost_router_circuit_state{tier="frontier"} 2' in output
    assert 'cost_router_circuit_state{tier="tier\\"with\\\\slash\\nline"} -1' in output


def test_render_prometheus_metrics_omits_circuits_when_empty_or_none() -> None:
    assert "cost_router_circuit_state" not in render_prometheus_metrics([], circuits=None)
    assert "cost_router_circuit_state" not in render_prometheus_metrics([], circuits={})


def test_server_http_metrics_includes_circuit_state_when_enabled() -> None:
    breaker = ProviderCircuitBreaker(config=CircuitBreakerConfig(failure_threshold=2))
    breaker.record_failure(ModelTier.FRONTIER)
    breaker.record_failure(ModelTier.FRONTIER)

    fallback_adapter = FallbackChainAdapter(
        inner=DeterministicFixtureAdapter(),
        breaker=breaker,
    )
    gateway = OpenAIRouterGateway(
        adapter=fallback_adapter,
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        text = resp.text
        assert "# TYPE cost_router_circuit_state gauge" in text
        assert 'cost_router_circuit_state{tier="compact"} 0' in text
        assert 'cost_router_circuit_state{tier="mid"} 0' in text
        assert 'cost_router_circuit_state{tier="frontier"} 2' in text


def test_server_http_metrics_omits_circuit_state_when_disabled() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "cost_router_circuit_state" not in resp.text
