from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from typing import Any

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import PolicySpec
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


def _parse_sse_frames(raw_text: str) -> list[str]:
    assert raw_text.endswith("data: [DONE]\n\n")
    frames = [frame for frame in raw_text.split("\n\n") if frame]
    payloads: list[str] = []
    for frame in frames:
        assert frame.startswith("data: ")
        payloads.append(frame[len("data: ") :])
    return payloads


def test_chat_completions_sse_streaming_returns_chunks_and_done() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Say hello in one sentence."}],
                "stream": True,
                "x_org_id": "org_sse",
                "x_agent_id": "sse_bot",
            },
        )

        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "text/event-stream; charset=utf-8"

        payloads = _parse_sse_frames(resp.text)
        assert len(payloads) == 3
        assert payloads[-1] == "[DONE]"

        first_chunk: dict[str, Any] = json.loads(payloads[0])
        final_chunk: dict[str, Any] = json.loads(payloads[1])

        assert first_chunk["object"] == "chat.completion.chunk"
        assert first_chunk["id"] == final_chunk["id"]
        assert first_chunk["created"] == final_chunk["created"]
        assert first_chunk["model"] == final_chunk["model"]
        assert len(first_chunk["choices"]) == 1
        assert first_chunk["choices"][0]["index"] == 0
        assert first_chunk["choices"][0]["delta"]["role"] == "assistant"
        assert len(first_chunk["choices"][0]["delta"]["content"]) > 0
        assert first_chunk["choices"][0]["finish_reason"] is None

        assert final_chunk["object"] == "chat.completion.chunk"
        assert len(final_chunk["choices"]) == 1
        assert final_chunk["choices"][0]["index"] == 0
        assert final_chunk["choices"][0]["delta"] == {}
        assert final_chunk["choices"][0]["finish_reason"] == "stop"
        assert final_chunk["usage"]["total_tokens"] > 0
        assert final_chunk["x_router_metadata"]["org_id"] == "org_sse"
        assert final_chunk["x_router_metadata"]["agent_id"] == "sse_bot"


def test_chat_completions_stream_false_returns_json_completion() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Say hello in one sentence."}],
                "stream": False,
            },
        )

        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert body["choices"][0]["finish_reason"] == "stop"
