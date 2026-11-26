from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.governance import TenantAuthRegistry, TenantRole
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import ModelTier, PolicySpec
from cost_aware_model_router.server import create_router_http_server


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


def test_openapi_json_returns_310_spec_with_bearer_auth_and_core_paths() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    expected_paths = {
        "/healthz",
        "/readyz",
        "/openapi.json",
        "/v1/models",
        "/v1/models/{model_id}",
        "/v1/chat/completions",
        "/v1/finops/economics",
        "/v1/finops/shadow-summary",
        "/v1/finops/circuits",
        "/v1/finops/circuits/reset",
        "/metrics",
        "/dashboard",
    }

    with (
        _running_server(gateway) as (_, base_url),
        httpx.Client(base_url=base_url, timeout=5.0) as client,
    ):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["openapi"] == "3.1.0"
        assert spec["info"]["title"] == "Cost-Aware Model Router API"
        assert spec["components"]["securitySchemes"]["BearerAuth"] == {
            "type": "http",
            "scheme": "bearer",
        }
        assert expected_paths.issubset(set(spec["paths"].keys()))


def test_v1_model_lookup_returns_200_404_and_enforces_router_client_auth() -> None:
    catalog = load_bundled_catalog()
    compact_spec = catalog.models[ModelTier.COMPACT]

    open_gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=catalog,
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with (
        _running_server(open_gateway) as (_, base_url),
        httpx.Client(base_url=base_url, timeout=5.0) as client,
    ):
        found = client.get(f"/v1/models/{quote(compact_spec.model_id, safe='')}")
        assert found.status_code == 200
        assert found.json() == {
            "id": compact_spec.model_id,
            "object": "model",
            "owned_by": compact_spec.provider,
            "tier": ModelTier.COMPACT.value,
        }

        missing = client.get("/v1/models/nonexistent-model")
        assert missing.status_code == 404
        assert missing.json() == {"error": {"message": "Model 'nonexistent-model' not found"}}

    registry = TenantAuthRegistry()
    client_token, _ = registry.issue_key("org_alpha", TenantRole.ROUTER_CLIENT)
    authed_gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=catalog,
        primary_policy=_policy_by_id("threshold-balanced"),
        auth_registry=registry,
    )

    with (
        _running_server(authed_gateway) as (_, base_url),
        httpx.Client(base_url=base_url, timeout=5.0) as client,
    ):
        unauthed = client.get(f"/v1/models/{compact_spec.model_id}")
        assert unauthed.status_code == 401

        authed = client.get(
            f"/v1/models/{compact_spec.model_id}",
            headers={"Authorization": f"Bearer {client_token}"},
        )
        assert authed.status_code == 200
        assert authed.json()["id"] == compact_spec.model_id
        assert authed.json()["tier"] == ModelTier.COMPACT.value

        authed_missing = client.get(
            "/v1/models/nonexistent-model",
            headers={"Authorization": f"Bearer {client_token}"},
        )
        assert authed_missing.status_code == 404
