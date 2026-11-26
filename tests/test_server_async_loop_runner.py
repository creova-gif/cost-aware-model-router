from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import (
    ExecutionTrace,
    ModelTier,
    PolicySpec,
    PricingCatalog,
    WorkloadItem,
)
from cost_aware_model_router.server import _RouterHTTPServer, create_router_http_server


def _policy_by_id(policy_id: str) -> PolicySpec:
    for policy in default_policy_suite():
        if policy.policy_id == policy_id:
            return policy
    raise KeyError(policy_id)


class _LoopTrackingAdapter(DeterministicFixtureAdapter):
    """Adapter that records the OS thread ID and running event loop ID for each call."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.observed_thread_ids: list[int] = []
        self.observed_loop_ids: list[int] = []

    async def execute_workload(
        self, workload: list[WorkloadItem], catalog: PricingCatalog
    ) -> dict[tuple[str, ModelTier], ExecutionTrace]:
        loop = asyncio.get_running_loop()
        thread_id = threading.get_ident()
        await asyncio.sleep(0.01)
        with self._lock:
            self.observed_thread_ids.append(thread_id)
            self.observed_loop_ids.append(id(loop))
        return await super().execute_workload(workload, catalog)


def test_concurrent_post_chat_completions_share_single_event_loop_thread() -> None:
    adapter = _LoopTrackingAdapter()
    gateway = OpenAIRouterGateway(
        adapter=adapter,
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )
    server = create_router_http_server(gateway, host="127.0.0.1", port=0)
    raw_host, port = server.server_address[:2]
    host = raw_host.decode("utf-8") if isinstance(raw_host, bytes) else raw_host
    base_url = f"http://{host}:{port}"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def _send_request(idx: int) -> tuple[int, str]:
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "request_id": f"req-{idx:03d}",
                    "category": "json_extraction",
                    "messages": [{"role": "user", "content": f"Extract total #{idx} as JSON."}],
                },
            )
            return resp.status_code, str(resp.json().get("object"))

    try:
        total_requests = 16
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_send_request, range(total_requests)))

        assert results == [(200, "chat.completion")] * total_requests
        assert len(adapter.observed_thread_ids) == total_requests
        assert len(set(adapter.observed_thread_ids)) == 1
        assert len(set(adapter.observed_loop_ids)) == 1
        assert adapter.observed_thread_ids[0] != threading.get_ident()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)


def test_server_close_shuts_down_background_event_loop_cleanly() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )
    server = create_router_http_server(gateway, host="127.0.0.1", port=0)
    assert isinstance(server, _RouterHTTPServer)
    assert server.loop_runner._thread.is_alive()

    server.server_close()
    assert not server.loop_runner._thread.is_alive()

    # Calling server_close() again is idempotent and does not raise or hang.
    server.server_close()
    assert not server.loop_runner._thread.is_alive()

    async def _noop() -> None:
        return None

    with pytest.raises(RuntimeError, match="closed"):
        server.loop_runner.run(_noop())
