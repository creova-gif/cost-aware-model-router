from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from typing import Any

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import PolicySpec
from cost_aware_model_router.server import _RouterHTTPServer, create_router_http_server


def _policy_by_id(policy_id: str) -> PolicySpec:
    for policy in default_policy_suite():
        if policy.policy_id == policy_id:
            return policy
    raise KeyError(policy_id)


def _make_gateway() -> OpenAIRouterGateway:
    return OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )


@contextmanager
def _running_server(
    gateway: OpenAIRouterGateway,
    *,
    access_log: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[tuple[ThreadingHTTPServer, str]]:
    server = create_router_http_server(gateway, host="127.0.0.1", port=0, access_log=access_log)
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


def test_router_http_server_socket_and_thread_defaults() -> None:
    assert _RouterHTTPServer.allow_reuse_address is True
    assert _RouterHTTPServer.daemon_threads is True
    assert _RouterHTTPServer.request_queue_size == 128


def test_concurrent_burst_requests_succeed_without_connection_resets() -> None:
    gateway = _make_gateway()
    concurrency = 20

    with _running_server(gateway) as (_, base_url):
        barrier = threading.Barrier(concurrency)

        def _get_healthz(idx: int) -> int:
            barrier.wait(timeout=5.0)
            with httpx.Client(base_url=base_url, timeout=10.0) as client:
                resp = client.get("/healthz", headers={"X-Request-Id": f"burst-get-{idx}"})
                assert resp.json() == {"status": "ok"}
                return resp.status_code

        def _post_completion(idx: int) -> int:
            barrier.wait(timeout=5.0)
            with httpx.Client(base_url=base_url, timeout=10.0) as client:
                resp = client.post(
                    "/v1/chat/completions",
                    headers={"X-Request-Id": f"burst-post-{idx}"},
                    json={
                        "messages": [{"role": "user", "content": f"Burst prompt {idx}"}],
                    },
                )
                body = resp.json()
                assert body["object"] == "chat.completion"
                return resp.status_code

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            get_statuses = list(pool.map(_get_healthz, range(concurrency)))
        assert get_statuses == [200] * concurrency

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            post_statuses = list(pool.map(_post_completion, range(concurrency)))
        assert post_statuses == [200] * concurrency


def test_access_log_records_expected_fields_and_redacts_secrets() -> None:
    gateway = _make_gateway()
    records: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _record_log(entry: dict[str, Any]) -> None:
        with lock:
            records.append(entry)

    secret_query = "super-secret-query-token"
    secret_auth_get = "sk-secret-auth-get-123"
    secret_auth_post = "sk-secret-auth-post-456"
    secret_prompt = "TOP_SECRET_PROMPT_PAYLOAD_XYZ"

    with _running_server(gateway, access_log=_record_log) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        get_resp = client.get(
            f"/healthz?api_key={secret_query}",
            headers={
                "X-Request-Id": "req-access-get-1",
                "Authorization": f"Bearer {secret_auth_get}",
            },
        )
        assert get_resp.status_code == 200

        post_resp = client.post(
            "/v1/chat/completions",
            headers={
                "X-Request-Id": "req-access-post-2",
                "Authorization": f"Bearer {secret_auth_post}",
            },
            json={
                "messages": [{"role": "user", "content": secret_prompt}],
            },
        )
        assert post_resp.status_code == 200

    assert len(records) == 2

    get_entry, post_entry = records
    assert set(get_entry.keys()) == {"request_id", "method", "path", "status_code", "duration_ms"}
    assert get_entry["request_id"] == "req-access-get-1"
    assert get_entry["method"] == "GET"
    assert get_entry["path"] == "/healthz"
    assert get_entry["status_code"] == 200
    assert isinstance(get_entry["duration_ms"], float)
    assert get_entry["duration_ms"] >= 0.0

    assert set(post_entry.keys()) == {"request_id", "method", "path", "status_code", "duration_ms"}
    assert post_entry["request_id"] == "req-access-post-2"
    assert post_entry["method"] == "POST"
    assert post_entry["path"] == "/v1/chat/completions"
    assert post_entry["status_code"] == 200
    assert isinstance(post_entry["duration_ms"], float)
    assert post_entry["duration_ms"] >= 0.0

    serialized = json.dumps(records)
    for forbidden in (
        secret_query,
        secret_auth_get,
        secret_auth_post,
        secret_prompt,
        "Authorization",
        "Bearer",
    ):
        assert forbidden not in serialized
