from __future__ import annotations

import hashlib
import json
import re
import threading
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from cost_aware_model_router import cli
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
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy


def _policy_by_id(policy_id: str) -> PolicySpec:
    for policy in default_policy_suite():
        if policy.policy_id == policy_id:
            return policy
    raise KeyError(policy_id)


def _parse_kv_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def _start_server(gateway: OpenAIRouterGateway) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = create_router_http_server(gateway, host="127.0.0.1", port=0)
    raw_host, port = server.server_address[:2]
    host = raw_host.decode("utf-8") if isinstance(raw_host, bytes) else raw_host
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://{host}:{port}"


def _stop_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5.0)


def _extract_dashboard_snapshot(html_output: str) -> dict[str, Any]:
    match = re.search(
        r'<script id="finops-snapshot" type="application/json">(.*?)</script>',
        html_output,
        re.DOTALL,
    )
    assert match is not None
    parsed = json.loads(match.group(1))
    assert isinstance(parsed, dict)
    return parsed


def test_e2e_control_plane_enterprise_lifecycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keys_path = tmp_path / "keys.json"
    db_path = tmp_path / "econ.db"

    # 1. Issue ADMIN and ROUTER_CLIENT keys via CLI; verify raw tokens are never persisted.
    rc_admin = cli.main(
        [
            "issue-key",
            "--org-id",
            "org_e2e",
            "--role",
            "admin",
            "--out",
            str(keys_path),
        ]
    )
    assert rc_admin == 0
    admin_out = _parse_kv_output(capsys.readouterr().out)
    admin_token = admin_out["token"]

    rc_client = cli.main(
        [
            "issue-key",
            "--org-id",
            "org_e2e",
            "--role",
            "router_client",
            "--out",
            str(keys_path),
        ]
    )
    assert rc_client == 0
    client_out = _parse_kv_output(capsys.readouterr().out)
    client_key_id = client_out["key_id"]
    client_token = client_out["token"]

    keys_text = keys_path.read_text(encoding="utf-8")
    assert admin_token not in keys_text
    assert client_token not in keys_text
    assert admin_token.split(".", 1)[1] not in keys_text
    assert client_token.split(".", 1)[1] not in keys_text

    keys_payload = json.loads(keys_text)
    assert len(keys_payload["records"]) == 2
    assert {rec["key_sha256"] for rec in keys_payload["records"]} == {
        hashlib.sha256(admin_token.encode("utf-8")).hexdigest(),
        hashlib.sha256(client_token.encode("utf-8")).hexdigest(),
    }

    # 2. Wire TenantAuthRegistry, SQLiteEconomicsStore, Thermostat, and FallbackChainAdapter.
    registry = TenantAuthRegistry.from_json(keys_text)
    store = SQLiteEconomicsStore(db_path)
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_e2e",
                agent_id="agent_e2e",
                hourly_budget_usd=Decimal("0.50000000"),
                soft_downgrade_ratio=0.70,
                hard_cap_ratio=1.00,
                min_quality_tier=ModelTier.COMPACT,
            )
        ]
    )
    breaker = ProviderCircuitBreaker(config=CircuitBreakerConfig(failure_threshold=2))
    fallback_adapter = FallbackChainAdapter(
        inner=DeterministicFixtureAdapter(),
        breaker=breaker,
    )
    catalog = load_bundled_catalog()
    gateway = OpenAIRouterGateway(
        adapter=fallback_adapter,
        catalog=catalog,
        primary_policy=_policy_by_id("threshold-balanced"),
        shadow_policy=_policy_by_id("threshold-aggressive"),
        thermostat=thermostat,
        store=store,
        auth_registry=registry,
    )

    server, thread, base_url = _start_server(gateway)
    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            # 3. Verify /readyz, /v1/models, and /v1/chat/completions + SQLite persistence.
            ready_resp = client.get("/readyz")
            assert ready_resp.status_code == 200
            assert ready_resp.json()["status"] == "ready"

            assert client.get("/v1/models").status_code == 401
            models_resp = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {client_token}"},
            )
            assert models_resp.status_code == 200
            models_body = models_resp.json()
            assert models_body["object"] == "list"
            assert len(models_body["data"]) == len(catalog.models)

            req_id = "e2e-001"
            chat_resp = client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {client_token}",
                    "X-Request-Id": req_id,
                },
                json={
                    "model": "auto",
                    "request_id": req_id,
                    "category": "code_sql",
                    "messages": [{"role": "user", "content": "SELECT 1;"}],
                    "x_org_id": "org_e2e",
                    "x_agent_id": "agent_e2e",
                },
            )
            assert chat_resp.status_code == 200, chat_resp.text
            assert chat_resp.headers.get("X-Request-Id") == req_id
            chat_body = chat_resp.json()
            assert chat_body["choices"]
            assert Decimal(chat_body["usage"]["cost_usd"]) > Decimal("0")

            ledger_entries = store.load_ledger_entries(org_id="org_e2e")
            assert len(ledger_entries) == 1
            assert ledger_entries[0].request_id == req_id
            assert store.summarize_economics(org_id="org_e2e").total_requests == 1
            assert len(store.load_shadow_comparisons(org_id="org_e2e")) == 1
            assert len(store.load_spans()) >= 1

            # 4. Trip circuit breaker; verify circuits API, /metrics, /dashboard, and ADMIN reset.
            breaker.record_failure(ModelTier.COMPACT)
            breaker.record_failure(ModelTier.COMPACT)

            circuits_resp = client.get(
                "/v1/finops/circuits",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert circuits_resp.status_code == 200
            assert circuits_resp.json() == {
                "enabled": True,
                "circuits": {
                    "compact": "open",
                    "mid": "closed",
                    "frontier": "closed",
                },
            }

            metrics_resp = client.get(
                "/metrics",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert metrics_resp.status_code == 200
            assert 'cost_router_circuit_state{tier="compact"} 2' in metrics_resp.text

            dashboard_resp = client.get(
                "/dashboard",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert dashboard_resp.status_code == 200
            snapshot = _extract_dashboard_snapshot(dashboard_resp.text)
            assert snapshot["circuits"] == {
                "enabled": True,
                "circuits": {
                    "compact": "open",
                    "mid": "closed",
                    "frontier": "closed",
                },
            }

            forbidden_reset = client.post(
                "/v1/finops/circuits/reset",
                headers={"Authorization": f"Bearer {client_token}"},
                json={"tier": "compact"},
            )
            assert forbidden_reset.status_code == 403

            admin_reset = client.post(
                "/v1/finops/circuits/reset",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"tier": "compact"},
            )
            assert admin_reset.status_code == 200
            assert admin_reset.json() == {
                "reset": True,
                "tier": "compact",
                "enabled": True,
                "circuits": {
                    "compact": "closed",
                    "mid": "closed",
                    "frontier": "closed",
                },
            }

            restored_metrics = client.get(
                "/metrics",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert restored_metrics.status_code == 200
            assert 'cost_router_circuit_state{tier="compact"} 0' in restored_metrics.text
    finally:
        _stop_server(server, thread)

    # 5. Revoke ROUTER_CLIENT key via CLI, reload registry, and verify 403 rejection.
    rc_revoke = cli.main(
        [
            "revoke-key",
            "--key-id",
            client_key_id,
            "--auth-config",
            str(keys_path),
        ]
    )
    assert rc_revoke == 0
    capsys.readouterr()

    reloaded_registry = TenantAuthRegistry.from_json(keys_path.read_text(encoding="utf-8"))
    decision = reloaded_registry.authenticate_and_authorize(
        f"Bearer {client_token}",
        required_role=TenantRole.ROUTER_CLIENT,
        claimed_org_id="org_e2e",
    )
    assert decision.authorized is False
    assert decision.status_code == 403

    reloaded_gateway = OpenAIRouterGateway(
        adapter=fallback_adapter,
        catalog=catalog,
        primary_policy=_policy_by_id("threshold-balanced"),
        store=store,
        auth_registry=reloaded_registry,
    )
    reloaded_server, reloaded_thread, reloaded_url = _start_server(reloaded_gateway)
    try:
        with httpx.Client(base_url=reloaded_url, timeout=5.0) as client:
            revoked_models = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {client_token}"},
            )
            assert revoked_models.status_code == 403

            revoked_chat = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {client_token}"},
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "SELECT 2;"}],
                },
            )
            assert revoked_chat.status_code == 403

            admin_models = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert admin_models.status_code == 200
    finally:
        _stop_server(reloaded_server, reloaded_thread)
        store.close()
        assert store.is_healthy() is False
