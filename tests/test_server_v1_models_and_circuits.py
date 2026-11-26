from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.cli import _build_gateway_for_serve, build_parser
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.governance import TenantAuthRegistry, TenantRole
from cost_aware_model_router.learner import ContextualBanditConfig, ContextualBanditRouter
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


def test_v1_models_returns_catalog_and_enforces_router_client_auth() -> None:
    catalog = load_bundled_catalog()
    open_gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=catalog,
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(open_gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.get("/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        expected_data = [
            {
                "id": spec.model_id,
                "object": "model",
                "owned_by": spec.provider,
                "tier": tier.value,
            }
            for tier, spec in catalog.models.items()
        ]
        assert body["data"] == expected_data

    registry = TenantAuthRegistry()
    client_token, _ = registry.issue_key("org_alpha", TenantRole.ROUTER_CLIENT)
    authed_gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=catalog,
        primary_policy=_policy_by_id("threshold-balanced"),
        auth_registry=registry,
    )

    with _running_server(authed_gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        unauthed = client.get("/v1/models")
        assert unauthed.status_code == 401

        authed = client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {client_token}"},
        )
        assert authed.status_code == 200
        assert authed.json()["object"] == "list"
        assert len(authed.json()["data"]) == len(catalog.models)


def test_v1_finops_circuits_without_and_with_fallback_and_auditor_auth() -> None:
    plain_gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(plain_gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.get("/v1/finops/circuits")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "circuits": {}}

    breaker = ProviderCircuitBreaker(config=CircuitBreakerConfig(failure_threshold=2))
    fallback_adapter = FallbackChainAdapter(
        inner=DeterministicFixtureAdapter(),
        breaker=breaker,
    )
    registry = TenantAuthRegistry()
    client_token, _ = registry.issue_key("org_alpha", TenantRole.ROUTER_CLIENT)
    auditor_token, _ = registry.issue_key("org_alpha", TenantRole.AUDITOR)

    resilient_gateway = OpenAIRouterGateway(
        adapter=fallback_adapter,
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        auth_registry=registry,
    )

    with _running_server(resilient_gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        assert client.get("/v1/finops/circuits").status_code == 401
        assert (
            client.get(
                "/v1/finops/circuits",
                headers={"Authorization": f"Bearer {client_token}"},
            ).status_code
            == 403
        )

        initial_resp = client.get(
            "/v1/finops/circuits",
            headers={"Authorization": f"Bearer {auditor_token}"},
        )
        assert initial_resp.status_code == 200
        assert initial_resp.json() == {
            "enabled": True,
            "circuits": {
                "compact": "closed",
                "mid": "closed",
                "frontier": "closed",
            },
        }

        breaker.record_failure(ModelTier.COMPACT)
        breaker.record_failure(ModelTier.COMPACT)

        tripped_resp = client.get(
            "/v1/finops/circuits",
            headers={"Authorization": f"Bearer {auditor_token}"},
        )
        assert tripped_resp.status_code == 200
        assert tripped_resp.json() == {
            "enabled": True,
            "circuits": {
                "compact": "open",
                "mid": "closed",
                "frontier": "closed",
            },
        }


def test_serve_cli_parser_and_build_gateway_with_fallback_and_bandit(tmp_path: Path) -> None:
    bandit = ContextualBanditRouter(config=ContextualBanditConfig(min_quality_floor=0.88))
    policy_path = tmp_path / "bandit-policy.json"
    policy_path.write_text(bandit.to_json(), encoding="utf-8")

    parser = build_parser()
    default_args = parser.parse_args(["serve"])
    assert default_args.enable_fallback is False
    assert default_args.circuit_failure_threshold == 3
    assert default_args.bandit_policy_file is None

    args = parser.parse_args(
        [
            "serve",
            "--enable-fallback",
            "--circuit-failure-threshold",
            "2",
            "--bandit-policy-file",
            str(policy_path),
        ]
    )
    assert args.enable_fallback is True
    assert args.circuit_failure_threshold == 2
    assert args.bandit_policy_file == policy_path

    gateway = _build_gateway_for_serve(
        policy_id=args.policy,
        enable_fallback=args.enable_fallback,
        circuit_failure_threshold=args.circuit_failure_threshold,
        bandit_policy_file=args.bandit_policy_file,
    )
    assert isinstance(gateway.adapter, FallbackChainAdapter)
    assert isinstance(gateway.adapter.circuit_breaker, ProviderCircuitBreaker)
    assert gateway._bandit_router is not None
    assert gateway._bandit_router.config.min_quality_floor == 0.88

    # Verify threshold=2 trips the circuit breaker after 2 failures
    gateway.adapter.circuit_breaker.record_failure(ModelTier.MID)
    assert gateway.adapter.circuit_breaker.snapshot()[ModelTier.MID].value == "closed"
    gateway.adapter.circuit_breaker.record_failure(ModelTier.MID)
    assert gateway.adapter.circuit_breaker.snapshot()[ModelTier.MID].value == "open"

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        circuits_resp = client.get("/v1/finops/circuits")
        assert circuits_resp.status_code == 200
        assert circuits_resp.json() == {
            "enabled": True,
            "circuits": {
                "compact": "closed",
                "mid": "open",
                "frontier": "closed",
            },
        }
