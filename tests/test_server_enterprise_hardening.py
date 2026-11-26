from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

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
from cost_aware_model_router.server import _resolve_request_id, create_router_http_server

_UUID4_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
_EXPECTED_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


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


def test_resolve_request_id_validation_and_generation() -> None:
    assert _resolve_request_id({"X-Request-Id": "req-123_abc.xyz"}) == "req-123_abc.xyz"
    assert _resolve_request_id({"X-Request-Id": "a" * 128}) == "a" * 128

    for invalid in ("", "bad id", "bad\r\nid", "bad\nid", "a" * 129, "bad/id"):
        generated = _resolve_request_id({"X-Request-Id": invalid})
        assert _UUID4_HEX_RE.fullmatch(generated) is not None
        assert generated != invalid

    assert _UUID4_HEX_RE.fullmatch(_resolve_request_id({})) is not None


def test_request_id_header_over_http_omitted_valid_and_invalid() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        omitted_resp = client.get("/healthz")
        assert omitted_resp.status_code == 200
        omitted_id = omitted_resp.headers.get("X-Request-Id")
        assert omitted_id is not None
        assert _UUID4_HEX_RE.fullmatch(omitted_id) is not None

        valid_id = "trace_01.req-abc"
        echoed_resp = client.get("/healthz", headers={"X-Request-Id": valid_id})
        assert echoed_resp.status_code == 200
        assert echoed_resp.headers.get("X-Request-Id") == valid_id

        invalid_space = "bad request id"
        replaced_space_resp = client.get("/healthz", headers={"X-Request-Id": invalid_space})
        assert replaced_space_resp.status_code == 200
        replaced_space_id = replaced_space_resp.headers.get("X-Request-Id")
        assert replaced_space_id is not None
        assert replaced_space_id != invalid_space
        assert _UUID4_HEX_RE.fullmatch(replaced_space_id) is not None

        too_long = "x" * 129
        replaced_len_resp = client.get("/healthz", headers={"X-Request-Id": too_long})
        assert replaced_len_resp.status_code == 200
        replaced_len_id = replaced_len_resp.headers.get("X-Request-Id")
        assert replaced_len_id is not None
        assert replaced_len_id != too_long
        assert _UUID4_HEX_RE.fullmatch(replaced_len_id) is not None


def test_security_headers_present_on_core_routes() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        for route in ("/healthz", "/readyz", "/v1/models", "/dashboard"):
            resp = client.get(route)
            assert resp.status_code == 200
            for header_name, expected_val in _EXPECTED_SECURITY_HEADERS.items():
                assert resp.headers.get(header_name) == expected_val
            req_id = resp.headers.get("X-Request-Id")
            assert req_id is not None
            assert _UUID4_HEX_RE.fullmatch(req_id) is not None


def test_readyz_reflects_circuit_breaker_readiness() -> None:
    plain_gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )
    with _running_server(plain_gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}

    breaker = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=60.0)
    )
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
        resp_closed = client.get("/readyz")
        assert resp_closed.status_code == 200
        assert resp_closed.json() == {"status": "ready"}

        # Open only two of three tiers -> still ready.
        breaker.record_failure(ModelTier.COMPACT)
        breaker.record_failure(ModelTier.MID)
        resp_partial = client.get("/readyz")
        assert resp_partial.status_code == 200
        assert resp_partial.json() == {"status": "ready"}

        # Open the final tier -> all tier circuits open -> 503 not_ready.
        breaker.record_failure(ModelTier.FRONTIER)
        resp_open = client.get("/readyz")
        assert resp_open.status_code == 503
        assert resp_open.json() == {
            "status": "not_ready",
            "reason": "all_tier_circuits_open",
        }
        for header_name, expected_val in _EXPECTED_SECURITY_HEADERS.items():
            assert resp_open.headers.get(header_name) == expected_val
