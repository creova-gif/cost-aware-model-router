from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from http.server import ThreadingHTTPServer

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.cli import _build_gateway_for_serve, build_parser
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import ModelTier, PolicySpec
from cost_aware_model_router.server import MAX_REQUEST_BODY_BYTES, create_router_http_server
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy


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


def test_server_healthz_chat_shadow_economics_and_metrics() -> None:
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_acme",
                agent_id="invoice_bot",
                hourly_budget_usd=Decimal("5.00000000"),
            )
        ]
    )
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("all-frontier"),
        thermostat=thermostat,
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(base_url=base_url, timeout=5.0) as client:
        health_resp = client.get("/healthz")
        assert health_resp.status_code == 200
        assert health_resp.json() == {"status": "ok"}

        chat_resp = client.post(
            "/v1/chat/completions",
            headers={
                "x-org-id": "org_acme",
                "x-agent-id": "invoice_bot",
                "x-policy-id": "threshold-balanced",
                "x-shadow-candidate-policy-id": "threshold-aggressive",
            },
            json={
                "model": "auto",
                "request_id": "req-001",
                "category": "json_extraction",
                "messages": [
                    {
                        "role": "user",
                        "content": "Extract customer_id and invoice_total as JSON.",
                    }
                ],
            },
        )
        assert chat_resp.status_code == 200
        body = chat_resp.json()
        assert body["object"] == "chat.completion"
        assert body["router_metadata"]["org_id"] == "org_acme"
        assert body["router_metadata"]["agent_id"] == "invoice_bot"
        assert body["router_metadata"]["policy_id"] == "threshold-balanced"
        assert body["router_metadata"]["shadow_evaluated"] is True
        assert body["router_metadata"]["shadow_policy_id"] == "threshold-aggressive"

        shadow_resp = client.get("/v1/finops/shadow-summary")
        assert shadow_resp.status_code == 200
        shadow_body = shadow_resp.json()
        assert shadow_body["total_shadow_requests"] == 1
        assert len(shadow_body["records"]) == 1
        assert shadow_body["records"][0]["request_id"] == "req-001"

        econ_resp = client.get("/v1/finops/economics", params={"org_id": "org_acme"})
        assert econ_resp.status_code == 200
        econ_body = econ_resp.json()
        assert econ_body["total_requests"] == 1
        assert "org_acme" in econ_body["spend_by_org"]

        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        assert metrics_resp.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert "cost_router_requests_total" in metrics_resp.text


def test_server_economics_without_thermostat_returns_400() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(base_url=base_url, timeout=5.0) as client:
        econ_resp = client.get("/v1/finops/economics")
        assert econ_resp.status_code == 400
        assert "Thermostat" in econ_resp.json()["error"]["message"]

        not_found = client.get("/v1/nonexistent")
        assert not_found.status_code == 404


def test_server_rejects_oversized_payload_with_413() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )
    oversized_body = b"x" * (MAX_REQUEST_BODY_BYTES + 1)

    with _running_server(gateway) as (_, base_url), httpx.Client(base_url=base_url, timeout=5.0) as client:
        resp = client.post(
            "/v1/chat/completions",
            content=oversized_body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413
        assert "error" in resp.json()
        assert "Traceback" not in resp.text


def test_server_validation_error_returns_400_without_stack_trace() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(base_url=base_url, timeout=5.0) as client:
        bad_json = client.post(
            "/v1/chat/completions",
            content=b"{not-valid-json",
            headers={"Content-Type": "application/json"},
        )
        assert bad_json.status_code == 400
        assert "error" in bad_json.json()
        assert "Traceback" not in bad_json.text

        bad_schema = client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": []},
        )
        assert bad_schema.status_code == 400
        assert "error" in bad_schema.json()
        assert "Traceback" not in bad_schema.text


def test_server_thermostat_block_returns_429() -> None:
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

    with _running_server(gateway) as (_, base_url), httpx.Client(base_url=base_url, timeout=5.0) as client:
        first = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert first.status_code == 200

        blocked = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert blocked.status_code == 429
        err = blocked.json()["error"]
        assert err["type"] == "loop_circuit_breaker"
        assert err["org_id"] == "org_demo"
        assert err["agent_id"] == "loop_agent"


def test_cli_serve_parser_and_gateway_builder() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "9090",
            "--policy",
            "threshold-balanced",
            "--shadow-policy",
            "threshold-aggressive",
            "--hourly-budget-usd",
            "2.50",
        ]
    )
    assert args.command == "serve"
    assert args.host == "0.0.0.0"
    assert args.port == 9090
    assert args.policy == "threshold-balanced"
    assert args.shadow_policy == "threshold-aggressive"
    assert args.hourly_budget_usd == Decimal("2.50")

    gateway = _build_gateway_for_serve(
        policy_id=args.policy,
        shadow_policy_id=args.shadow_policy,
        hourly_budget_usd=args.hourly_budget_usd,
    )
    assert gateway._thermostat is not None
    assert gateway._shadow_policy is not None
    assert gateway._shadow_policy.policy_id == "threshold-aggressive"
