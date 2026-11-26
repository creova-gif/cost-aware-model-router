from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.resilience import (
    CircuitBreakerConfig,
    FallbackChainAdapter,
    ProviderCircuitBreaker,
)
from cost_aware_model_router.schemas import ModelTier, PolicySpec
from cost_aware_model_router.server import create_router_http_server
from cost_aware_model_router.store import SQLiteEconomicsStore


def _policy_by_id(policy_id: str) -> PolicySpec:
    for policy in default_policy_suite():
        if policy.policy_id == policy_id:
            return policy
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


def test_head_healthz_returns_headers_and_empty_body() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        get_resp = client.get("/healthz")
        head_resp = client.head("/healthz")

        assert head_resp.status_code == 200
        assert head_resp.content == b""
        content_length = int(head_resp.headers["Content-Length"])
        assert content_length > 0
        assert content_length == len(get_resp.content)
        assert head_resp.headers.get("X-Request-Id")
        assert head_resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert head_resp.headers.get("X-Frame-Options") == "DENY"
        assert head_resp.headers.get("Referrer-Policy") == "no-referrer"


def test_head_readyz_reflects_store_health_with_empty_body(tmp_path: Path) -> None:
    store = SQLiteEconomicsStore(tmp_path / "economics.db")
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        store=store,
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        ready_resp = client.head("/readyz")
        assert ready_resp.status_code == 200
        assert ready_resp.content == b""
        assert int(ready_resp.headers["Content-Length"]) > 0
        assert ready_resp.headers.get("X-Request-Id")
        assert ready_resp.headers.get("X-Content-Type-Options") == "nosniff"

        store.close()
        unhealthy_resp = client.head("/readyz")
        assert unhealthy_resp.status_code == 503
        assert unhealthy_resp.content == b""
        assert int(unhealthy_resp.headers["Content-Length"]) > 0
        assert unhealthy_resp.headers.get("X-Request-Id")
        assert unhealthy_resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_head_readyz_reflects_circuit_breaker_readiness_with_empty_body() -> None:
    breaker = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=60.0)
    )
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
        ready_resp = client.head("/readyz")
        assert ready_resp.status_code == 200
        assert ready_resp.content == b""
        assert int(ready_resp.headers["Content-Length"]) > 0

        for tier in ModelTier:
            breaker.record_failure(tier)

        open_resp = client.head("/readyz")
        assert open_resp.status_code == 503
        assert open_resp.content == b""
        assert int(open_resp.headers["Content-Length"]) > 0
        assert open_resp.headers.get("X-Request-Id")
        assert open_resp.headers.get("X-Content-Type-Options") == "nosniff"
