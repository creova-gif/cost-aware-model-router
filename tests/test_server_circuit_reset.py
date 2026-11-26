from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.governance import TenantAuthRegistry, TenantRole
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


def test_circuit_reset_single_tier_and_all_tiers() -> None:
    breaker = ProviderCircuitBreaker(config=CircuitBreakerConfig(failure_threshold=2))
    fallback_adapter = FallbackChainAdapter(
        inner=DeterministicFixtureAdapter(),
        breaker=breaker,
    )
    gateway = OpenAIRouterGateway(
        adapter=fallback_adapter,
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    breaker.record_failure(ModelTier.COMPACT)
    breaker.record_failure(ModelTier.COMPACT)
    breaker.record_failure(ModelTier.MID)
    breaker.record_failure(ModelTier.MID)

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp_single = client.post("/v1/finops/circuits/reset", json={"tier": "compact"})
        assert resp_single.status_code == 200
        assert resp_single.json() == {
            "reset": True,
            "tier": "compact",
            "enabled": True,
            "circuits": {
                "compact": "closed",
                "mid": "open",
                "frontier": "closed",
            },
        }

        resp_all_empty_json = client.post("/v1/finops/circuits/reset", json={})
        assert resp_all_empty_json.status_code == 200
        assert resp_all_empty_json.json() == {
            "reset": True,
            "tier": "all",
            "enabled": True,
            "circuits": {
                "compact": "closed",
                "mid": "closed",
                "frontier": "closed",
            },
        }

        breaker.record_failure(ModelTier.FRONTIER)
        breaker.record_failure(ModelTier.FRONTIER)
        resp_all_empty_body = client.post("/v1/finops/circuits/reset")
        assert resp_all_empty_body.status_code == 200
        assert resp_all_empty_body.json() == {
            "reset": True,
            "tier": "all",
            "enabled": True,
            "circuits": {
                "compact": "closed",
                "mid": "closed",
                "frontier": "closed",
            },
        }


def test_circuit_reset_enforces_admin_rbac() -> None:
    breaker = ProviderCircuitBreaker(config=CircuitBreakerConfig(failure_threshold=2))
    fallback_adapter = FallbackChainAdapter(
        inner=DeterministicFixtureAdapter(),
        breaker=breaker,
    )
    registry = TenantAuthRegistry()
    client_token, _ = registry.issue_key("org_alpha", TenantRole.ROUTER_CLIENT)
    auditor_token, _ = registry.issue_key("org_alpha", TenantRole.AUDITOR)
    admin_token, _ = registry.issue_key("org_alpha", TenantRole.ADMIN)

    gateway = OpenAIRouterGateway(
        adapter=fallback_adapter,
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        auth_registry=registry,
    )

    breaker.record_failure(ModelTier.COMPACT)
    breaker.record_failure(ModelTier.COMPACT)

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        assert client.post("/v1/finops/circuits/reset").status_code == 401
        assert (
            client.post(
                "/v1/finops/circuits/reset",
                headers={"Authorization": f"Bearer {client_token}"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/finops/circuits/reset",
                headers={"Authorization": f"Bearer {auditor_token}"},
            ).status_code
            == 403
        )

        admin_resp = client.post(
            "/v1/finops/circuits/reset",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"tier": "compact"},
        )
        assert admin_resp.status_code == 200
        assert admin_resp.json()["reset"] is True
        assert admin_resp.json()["circuits"]["compact"] == "closed"


def test_circuit_reset_invalid_tier_and_disabled_breaker_return_400() -> None:
    breaker = ProviderCircuitBreaker(config=CircuitBreakerConfig(failure_threshold=2))
    fallback_adapter = FallbackChainAdapter(
        inner=DeterministicFixtureAdapter(),
        breaker=breaker,
    )
    resilient_gateway = OpenAIRouterGateway(
        adapter=fallback_adapter,
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(resilient_gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        invalid_tier_resp = client.post("/v1/finops/circuits/reset", json={"tier": "ultra"})
        assert invalid_tier_resp.status_code == 400
        assert "Invalid model tier" in invalid_tier_resp.json()["error"]["message"]

        bad_json_resp = client.post(
            "/v1/finops/circuits/reset",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert bad_json_resp.status_code == 400

    plain_gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )
    with _running_server(plain_gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        disabled_resp = client.post("/v1/finops/circuits/reset", json={})
        assert disabled_resp.status_code == 400
        assert (
            disabled_resp.json()["error"]["message"]
            == "Circuit breaker is not enabled on this gateway"
        )
