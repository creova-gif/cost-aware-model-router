from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from http.server import ThreadingHTTPServer

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    OpenAIRouterGateway,
)
from cost_aware_model_router.governance import TenantAuthRegistry, TenantRole
from cost_aware_model_router.learner import ComplexityBucket, ContextualBanditRouter
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import ModelTier, PolicySpec, TaskCategory
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


def test_gateway_bandit_router_updates_arm_stats_across_requests() -> None:
    bandit = ContextualBanditRouter()
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("all-frontier"),
        bandit_router=bandit,
    )

    async def _run() -> list[ModelTier]:
        tiers: list[ModelTier] = []
        for idx in range(4):
            req = ChatCompletionRequest(
                request_id=f"req-{idx:03d}",
                category=TaskCategory.CLASSIFICATION,
                messages=[
                    ChatMessage(
                        role="user",
                        content=f"Classify ticket #{idx} into billing or support.",
                    )
                ],
            )
            resp = await gateway.handle_chat_completion(req)
            tiers.append(resp.router_metadata.selected_tier)
        return tiers

    selected_tiers = asyncio.run(_run())
    # First 3 requests explore compact, mid, frontier in TIER_ORDER; 4th uses UCB1.
    assert selected_tiers[:3] == [ModelTier.COMPACT, ModelTier.MID, ModelTier.FRONTIER]

    ctx = (TaskCategory.CLASSIFICATION, ComplexityBucket.LOW)
    total_pulls = sum(bandit.get_arm_stats(ctx, tier).pulls for tier in ModelTier)
    assert total_pulls == 4
    for tier in ModelTier:
        stats = bandit.get_arm_stats(ctx, tier)
        assert stats.pulls >= 1
        assert stats.passes == stats.pulls
        assert stats.total_cost_usd > Decimal("0")

    # Explicit x_policy_id bypasses bandit selection and update.
    override_resp = asyncio.run(
        gateway.handle_chat_completion(
            ChatCompletionRequest(
                request_id="req-099",
                category=TaskCategory.CLASSIFICATION,
                x_policy_id="all-frontier",
                messages=[ChatMessage(role="user", content="Classify ticket #99.")],
            )
        )
    )
    assert override_resp.router_metadata.selected_tier == ModelTier.FRONTIER
    assert sum(bandit.get_arm_stats(ctx, tier).pulls for tier in ModelTier) == 4


def test_gateway_httpx_auth_rbac_isolation_and_rate_limit() -> None:
    now = 100.0
    registry = TenantAuthRegistry(clock=lambda: now)
    client_token, _ = registry.issue_key(
        "org_alpha",
        TenantRole.ROUTER_CLIENT,
        rate_limit_rps=0.001,
        burst_capacity=2,
    )
    auditor_token, _ = registry.issue_key(
        "org_alpha",
        TenantRole.AUDITOR,
        rate_limit_rps=10.0,
        burst_capacity=10,
    )

    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_alpha",
                agent_id="agent_gateway",
                hourly_budget_usd=Decimal("10.00000000"),
            )
        ]
    )
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        shadow_policy=_policy_by_id("all-frontier"),
        thermostat=thermostat,
        auth_registry=registry,
    )

    payload = {
        "model": "auto",
        "category": "classification",
        "messages": [{"role": "user", "content": "Classify this billing question."}],
    }

    async def _run() -> None:
        nonlocal now
        async with httpx.AsyncClient(
            transport=gateway.as_httpx_transport(),
            base_url="http://router.local",
        ) as client:
            # 401: missing Authorization header
            missing_chat = await client.post("/v1/chat/completions", json=payload)
            assert missing_chat.status_code == 401
            assert "error" in missing_chat.json()

            for path in ("/v1/finops/shadow-summary", "/v1/finops/economics", "/metrics"):
                missing_finops = await client.get(path)
                assert missing_finops.status_code == 401

            # 403: wrong RBAC role
            wrong_role_chat = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {auditor_token}"},
                json=payload,
            )
            assert wrong_role_chat.status_code == 403

            for path in ("/v1/finops/shadow-summary", "/v1/finops/economics", "/metrics"):
                wrong_role_finops = await client.get(
                    path,
                    headers={"Authorization": f"Bearer {client_token}"},
                )
                assert wrong_role_finops.status_code == 403

            # 403: cross-tenant x-org-id spoofing (header and body)
            spoof_header = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {client_token}",
                    "X-Org-Id": "org_beta",
                },
                json=payload,
            )
            assert spoof_header.status_code == 403

            spoof_body = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {client_token}"},
                json={**payload, "x_org_id": "org_beta"},
            )
            assert spoof_body.status_code == 403

            spoof_econ = await client.get(
                "/v1/finops/economics",
                params={"org_id": "org_beta"},
                headers={"Authorization": f"Bearer {auditor_token}"},
            )
            assert spoof_econ.status_code == 403

            # 200: valid router_client binds org_id from auth decision
            ok_chat_1 = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {client_token}"},
                json=payload,
            )
            assert ok_chat_1.status_code == 200
            assert ok_chat_1.json()["router_metadata"]["org_id"] == "org_alpha"

            ok_chat_2 = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {client_token}",
                    "X-Org-Id": "org_alpha",
                },
                json={
                    **payload,
                    "messages": [{"role": "user", "content": "Classify this shipping question."}],
                },
            )
            assert ok_chat_2.status_code == 200

            # 429: burst_capacity=2 exhausted for org_alpha router_client
            rate_limited = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {client_token}"},
                json=payload,
            )
            assert rate_limited.status_code == 429
            assert "rate limit" in rate_limited.json()["error"]["message"].lower()

            # Advance clock so org_alpha token bucket refills for auditor calls.
            now += 10000.0

            # 200: valid auditor on FinOps & metrics endpoints
            shadow_resp = await client.get(
                "/v1/finops/shadow-summary",
                headers={"Authorization": f"Bearer {auditor_token}"},
            )
            assert shadow_resp.status_code == 200
            assert shadow_resp.json()["total_shadow_requests"] == 2

            econ_resp = await client.get(
                "/v1/finops/economics",
                params={"org_id": "org_alpha"},
                headers={"Authorization": f"Bearer {auditor_token}"},
            )
            assert econ_resp.status_code == 200
            assert econ_resp.json()["total_requests"] == 2

            metrics_resp = await client.get(
                "/metrics",
                headers={"Authorization": f"Bearer {auditor_token}"},
            )
            assert metrics_resp.status_code == 200
            assert "cost_router_requests_total" in metrics_resp.text

    asyncio.run(_run())


def test_live_http_server_enforces_auth_while_keeping_healthz_public() -> None:
    registry = TenantAuthRegistry()
    client_token, _ = registry.issue_key("org_live", TenantRole.ROUTER_CLIENT)
    auditor_token, _ = registry.issue_key("org_live", TenantRole.AUDITOR)

    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_live",
                agent_id="agent_gateway",
                hourly_budget_usd=Decimal("10.00000000"),
            )
        ]
    )
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        shadow_policy=_policy_by_id("all-frontier"),
        thermostat=thermostat,
        auth_registry=registry,
    )

    chat_payload = {
        "model": "auto",
        "category": "json_extraction",
        "messages": [{"role": "user", "content": "Extract order_id from ticket #42."}],
    }

    with (
        _running_server(gateway) as (_, base_url),
        httpx.Client(base_url=base_url, timeout=5.0) as client,
    ):
        # /healthz remains unauthenticated
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        # Unauthenticated requests to protected routes return 401
        assert client.post("/v1/chat/completions", json=chat_payload).status_code == 401
        assert client.get("/dashboard").status_code == 401
        assert client.get("/v1/finops/shadow-summary").status_code == 401
        assert client.get("/v1/finops/economics").status_code == 401
        assert client.get("/metrics").status_code == 401

        # RBAC & cross-tenant checks on live server
        assert (
            client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {auditor_token}"},
                json=chat_payload,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {client_token}",
                    "X-Org-Id": "org_spoofed",
                },
                json=chat_payload,
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/dashboard",
                headers={"Authorization": f"Bearer {client_token}"},
            ).status_code
            == 403
        )

        # Valid router_client succeeds and binds org_id
        chat_ok = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {client_token}"},
            json=chat_payload,
        )
        assert chat_ok.status_code == 200
        assert chat_ok.json()["router_metadata"]["org_id"] == "org_live"

        # Valid auditor succeeds on /dashboard and /v1/finops/economics
        dash_ok = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {auditor_token}"},
        )
        assert dash_ok.status_code == 200
        assert "text/html" in dash_ok.headers["content-type"]

        econ_ok = client.get(
            "/v1/finops/economics",
            params={"org_id": "org_live"},
            headers={"Authorization": f"Bearer {auditor_token}"},
        )
        assert econ_ok.status_code == 200
        assert econ_ok.json()["total_requests"] == 1
