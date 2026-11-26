from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from http.server import ThreadingHTTPServer

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.governance import TenantAuthRegistry, TenantRole
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import ModelTier, PolicySpec
from cost_aware_model_router.server import create_router_http_server
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy


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


def test_rate_limited_429_includes_retry_after_header() -> None:
    now = 100.0
    registry = TenantAuthRegistry(clock=lambda: now)
    client_token, _ = registry.issue_key(
        "org_alpha",
        TenantRole.ROUTER_CLIENT,
        rate_limit_rps=0.001,
        burst_capacity=1,
    )
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        auth_registry=registry,
    )

    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    headers = {"Authorization": f"Bearer {client_token}"}

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        first = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert first.status_code == 200
        assert "Retry-After" not in first.headers

        limited = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert limited.status_code == 429
        assert limited.headers.get("Retry-After") == "1"


def test_thermostat_blocked_429_includes_retry_after_header() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_demo",
                agent_id="loop_agent",
                hourly_budget_usd=Decimal("0.00000100"),
                soft_downgrade_ratio=0.50,
                hard_cap_ratio=1.00,
                max_loop_repeats=2,
                min_quality_tier=ModelTier.FRONTIER,
            )
        ]
    )
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("all-frontier"),
        thermostat=thermostat,
    )

    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Prove quorum invariant step-by-step"}],
    }
    headers = {"x-org-id": "org_demo", "x-agent-id": "loop_agent"}

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        first = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert first.status_code == 200
        assert "Retry-After" not in first.headers

        blocked = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert blocked.status_code == 429
        assert blocked.headers.get("Retry-After") == "1"
        assert blocked.json()["error"]["type"] == "loop_circuit_breaker"
