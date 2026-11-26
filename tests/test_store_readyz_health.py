from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import PolicySpec
from cost_aware_model_router.server import create_router_http_server
from cost_aware_model_router.store import SQLiteEconomicsStore


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


def test_sqlite_economics_store_is_healthy_open_and_closed(tmp_path: Path) -> None:
    store = SQLiteEconomicsStore(tmp_path / "economics.db")
    assert store.is_healthy() is True

    store.close()
    assert store.is_healthy() is False


def test_readyz_reflects_sqlite_economics_store_health(tmp_path: Path) -> None:
    store = SQLiteEconomicsStore(tmp_path / "economics.db")
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        store=store,
    )
    assert gateway.store is store

    with _running_server(gateway) as (_, base_url), httpx.Client(
        base_url=base_url, timeout=5.0
    ) as client:
        ready_resp = client.get("/readyz")
        assert ready_resp.status_code == 200
        assert ready_resp.json() == {"status": "ready"}

        store.close()
        unhealthy_resp = client.get("/readyz")
        assert unhealthy_resp.status_code == 503
        assert unhealthy_resp.json() == {
            "status": "not_ready",
            "reason": "economics_store_unhealthy",
        }
