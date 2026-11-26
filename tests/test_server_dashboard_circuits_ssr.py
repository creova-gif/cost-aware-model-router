from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from typing import Any

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


def _extract_snapshot(html_output: str) -> dict[str, Any]:
    match = re.search(
        r'<script id="finops-snapshot" type="application/json">(.*?)</script>',
        html_output,
        re.DOTALL,
    )
    assert match is not None
    parsed = json.loads(match.group(1))
    assert isinstance(parsed, dict)
    return parsed


def test_dashboard_ssr_embeds_disabled_circuits_when_no_fallback_adapter() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert '"circuits":{"enabled":false,"circuits":{}}' in resp.text
        snapshot = _extract_snapshot(resp.text)
        assert snapshot["circuits"] == {"enabled": False, "circuits": {}}


def test_dashboard_ssr_embeds_live_circuits_including_open_state() -> None:
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

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        initial_resp = client.get("/dashboard")
        assert initial_resp.status_code == 200
        assert '"circuits":{"enabled":true,' in initial_resp.text
        initial_snapshot = _extract_snapshot(initial_resp.text)
        assert initial_snapshot["circuits"] == {
            "enabled": True,
            "circuits": {
                "compact": "closed",
                "mid": "closed",
                "frontier": "closed",
            },
        }

        breaker.record_failure(ModelTier.COMPACT)
        breaker.record_failure(ModelTier.COMPACT)

        tripped_resp = client.get("/dashboard")
        assert tripped_resp.status_code == 200
        assert '"circuits":{"enabled":true,' in tripped_resp.text
        tripped_snapshot = _extract_snapshot(tripped_resp.text)
        assert tripped_snapshot["circuits"] == {
            "enabled": True,
            "circuits": {
                "compact": "open",
                "mid": "closed",
                "frontier": "closed",
            },
        }
