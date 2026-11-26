from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import PolicySpec
from cost_aware_model_router.server import create_router_http_server

_EXPECTED_PERMISSIONS_POLICY = "geolocation=(), microphone=(), camera=()"
_EXPECTED_DASHBOARD_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


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


def _make_gateway() -> OpenAIRouterGateway:
    return OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )


def test_healthz_includes_permissions_policy_header() -> None:
    gateway = _make_gateway()
    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.headers.get("Permissions-Policy") == _EXPECTED_PERMISSIONS_POLICY


def test_dashboard_includes_permissions_policy_and_csp_headers() -> None:
    gateway = _make_gateway()
    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert resp.headers.get("Permissions-Policy") == _EXPECTED_PERMISSIONS_POLICY
        csp = resp.headers.get("Content-Security-Policy")
        assert csp == _EXPECTED_DASHBOARD_CSP
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'none'" in csp
