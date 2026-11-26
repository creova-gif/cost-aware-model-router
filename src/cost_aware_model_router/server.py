from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from cost_aware_model_router.dashboard import render_live_finops_dashboard_html
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    OpenAIRouterGateway,
    ThermostatBlockedError,
)
from cost_aware_model_router.governance import TenantRole
from cost_aware_model_router.metrics import render_prometheus_metrics
from cost_aware_model_router.resilience import CircuitState, ProviderCircuitBreaker
from cost_aware_model_router.schemas import ModelTier

MAX_REQUEST_BODY_BYTES = 1_048_576
_REQUEST_ID_MAX_LEN = 128
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")

_HEADER_TO_FIELD: dict[str, str] = {
    "x-org-id": "x_org_id",
    "x-agent-id": "x_agent_id",
    "x-policy-id": "x_policy_id",
    "x-shadow-candidate-policy-id": "x_shadow_candidate_policy_id",
}


def _resolve_request_id(headers: Message[str, str] | Mapping[str, str]) -> str:
    """Return a validated X-Request-Id header or generate a fresh UUID4 hex correlation ID."""
    raw = headers.get("X-Request-Id")
    if (
        isinstance(raw, str)
        and 0 < len(raw) <= _REQUEST_ID_MAX_LEN
        and _REQUEST_ID_PATTERN.fullmatch(raw) is not None
    ):
        return raw
    return uuid.uuid4().hex


def _build_circuits_snapshot(gateway: OpenAIRouterGateway) -> dict[str, Any]:
    """Return a JSON-serializable circuit-breaker snapshot for FinOps endpoints and SSR."""
    cb = getattr(gateway.adapter, "circuit_breaker", None)
    if isinstance(cb, ProviderCircuitBreaker):
        return {
            "enabled": True,
            "circuits": {tier.value: state.value for tier, state in cb.snapshot().items()},
        }
    return {"enabled": False, "circuits": {}}


class _AsyncLoopRunner:
    """Thread-safe runner owning a single asyncio event loop on a daemon thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="router-async-loop",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            finally:
                self._loop.close()

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Execute a coroutine on the shared background event loop and return its result."""
        with self._lock:
            if self._closed:
                coro.close()
                raise RuntimeError("Async loop runner is closed")
            loop = self._loop
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def close(self) -> None:
        """Stop the background event loop, wait for its thread, and close the loop."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=5.0)


class _RouterHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a shared background asyncio event loop."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
    ) -> None:
        super().__init__(server_address, request_handler_class)
        self.loop_runner = _AsyncLoopRunner()

    def server_close(self) -> None:
        """Shut down the shared background event loop before closing the HTTP server."""
        try:
            self.loop_runner.close()
        finally:
            super().server_close()


def create_router_http_server(
    gateway: OpenAIRouterGateway,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    access_log: Callable[[dict[str, Any]], None] | None = None,
) -> ThreadingHTTPServer:
    """Create a zero-dependency local ThreadingHTTPServer backed by OpenAIRouterGateway."""

    class _RouterRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Suppress default stderr request logging; callers/CLI control output.
            return

        def _current_request_id(self) -> str:
            cached: str | None = getattr(self, "_request_id", None)
            if cached is None:
                cached = _resolve_request_id(self.headers)
                self._request_id = cached
            return cached

        def _send_bytes(
            self,
            status_code: int,
            content_type: str,
            body: bytes,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            request_id = self._current_request_id()
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-Id", request_id)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if extra_headers is not None:
                for header_name, header_value in extra_headers.items():
                    self.send_header(header_name, header_value)
            self.end_headers()
            self.wfile.write(body)
            if access_log is not None:
                request_start: float = getattr(self, "_request_start", time.monotonic())
                access_log(
                    {
                        "request_id": request_id,
                        "method": self.command,
                        "path": urlsplit(self.path).path,
                        "status_code": status_code,
                        "duration_ms": round((time.monotonic() - request_start) * 1000.0, 3),
                    }
                )

        def _send_json(
            self,
            status_code: int,
            payload: dict[str, Any],
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._send_bytes(
                status_code,
                "application/json; charset=utf-8",
                encoded,
                extra_headers=extra_headers,
            )

        def _send_text(self, status_code: int, content_type: str, text: str) -> None:
            self._send_bytes(status_code, content_type, text.encode("utf-8"))

        def _authorize(
            self,
            *,
            required_role: TenantRole,
            claimed_org_id: str | None = None,
        ) -> tuple[bool, str | None]:
            registry = gateway.auth_registry
            if registry is None:
                return True, None
            decision = registry.authenticate_and_authorize(
                self.headers.get("Authorization"),
                required_role=required_role,
                claimed_org_id=claimed_org_id,
            )
            if not decision.authenticated or not decision.authorized or decision.rate_limited:
                extra_headers = {"Retry-After": "1"} if decision.rate_limited else None
                self._send_json(
                    decision.status_code,
                    {"error": {"message": decision.reason}},
                    extra_headers=extra_headers,
                )
                return False, None
            return True, decision.org_id

        def do_GET(self) -> None:
            self._request_start = time.monotonic()
            self._request_id = _resolve_request_id(self.headers)
            try:
                parsed = urlsplit(self.path)
                path = parsed.path

                if path == "/healthz":
                    self._send_json(200, {"status": "ok"})
                    return

                if path == "/readyz":
                    if gateway.store is not None and not gateway.store.is_healthy():
                        self._send_json(
                            503,
                            {
                                "status": "not_ready",
                                "reason": "economics_store_unhealthy",
                            },
                        )
                        return
                    cb = getattr(gateway.adapter, "circuit_breaker", None)
                    if isinstance(cb, ProviderCircuitBreaker):
                        snapshot = cb.snapshot()
                        if snapshot and all(
                            state == CircuitState.OPEN for state in snapshot.values()
                        ):
                            self._send_json(
                                503,
                                {
                                    "status": "not_ready",
                                    "reason": "all_tier_circuits_open",
                                },
                            )
                            return
                    self._send_json(200, {"status": "ready"})
                    return

                if path == "/v1/models":
                    ok, _ = self._authorize(required_role=TenantRole.ROUTER_CLIENT)
                    if not ok:
                        return
                    self._send_json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": spec.model_id,
                                    "object": "model",
                                    "owned_by": spec.provider,
                                    "tier": tier.value,
                                }
                                for tier, spec in gateway.catalog.models.items()
                            ],
                        },
                    )
                    return

                if path == "/v1/finops/circuits":
                    ok, _ = self._authorize(required_role=TenantRole.AUDITOR)
                    if not ok:
                        return
                    self._send_json(200, _build_circuits_snapshot(gateway))
                    return

                if path == "/v1/finops/shadow-summary":
                    ok, _ = self._authorize(required_role=TenantRole.AUDITOR)
                    if not ok:
                        return
                    self._send_json(200, gateway.build_shadow_summary())
                    return

                if path == "/v1/finops/economics":
                    query_params = parse_qs(parsed.query, keep_blank_values=False)
                    org_id_values = query_params.get("org_id")
                    org_id = org_id_values[0] if org_id_values else None
                    ok, _ = self._authorize(
                        required_role=TenantRole.AUDITOR,
                        claimed_org_id=org_id,
                    )
                    if not ok:
                        return
                    try:
                        economics = gateway.summarize_economics(org_id=org_id)
                    except ValueError as exc:
                        self._send_json(400, {"error": {"message": str(exc)}})
                        return
                    self._send_json(200, economics)
                    return

                if path == "/metrics":
                    ok, _ = self._authorize(required_role=TenantRole.AUDITOR)
                    if not ok:
                        return
                    spans = gateway.span_exporter.get_spans()
                    summary = (
                        gateway._thermostat.summarize_economics()
                        if gateway._thermostat is not None
                        else None
                    )
                    circuits_snap = _build_circuits_snapshot(gateway)
                    metrics_text = render_prometheus_metrics(
                        spans,
                        summary=summary,
                        circuits=circuits_snap["circuits"] if circuits_snap["enabled"] else None,
                    )
                    self._send_text(
                        200,
                        "text/plain; version=0.0.4; charset=utf-8",
                        metrics_text,
                    )
                    return

                if path == "/dashboard":
                    ok, _ = self._authorize(required_role=TenantRole.AUDITOR)
                    if not ok:
                        return
                    economics_snapshot: dict[str, Any] | None = (
                        gateway.summarize_economics()
                        if gateway._thermostat is not None
                        else None
                    )
                    shadow_snapshot = gateway.build_shadow_summary()
                    recent_spans = [
                        span.model_dump(mode="json")
                        for span in gateway.span_exporter.get_spans()[-50:]
                    ]
                    dashboard_html = render_live_finops_dashboard_html(
                        economics=economics_snapshot,
                        shadow_summary=shadow_snapshot,
                        recent_spans=recent_spans,
                        circuits=_build_circuits_snapshot(gateway),
                    )
                    self._send_text(200, "text/html; charset=utf-8", dashboard_html)
                    return

                self._send_json(404, {"error": {"message": f"Unsupported route GET {path}"}})
            except Exception:
                self._send_json(500, {"error": {"message": "Internal server error"}})

        def _read_json_object_body(self) -> dict[str, Any] | None:
            raw_content_length = self.headers.get("Content-Length")
            if raw_content_length is None:
                self._send_json(400, {"error": {"message": "Missing Content-Length header"}})
                return None
            try:
                content_length = int(raw_content_length)
            except ValueError:
                self._send_json(400, {"error": {"message": "Invalid Content-Length header"}})
                return None

            if content_length < 0:
                self._send_json(400, {"error": {"message": "Invalid Content-Length header"}})
                return None

            if content_length > MAX_REQUEST_BODY_BYTES:
                self._send_json(
                    413,
                    {
                        "error": {
                            "message": (
                                f"Payload too large: {content_length} bytes exceeds "
                                f"maximum of {MAX_REQUEST_BODY_BYTES} bytes"
                            )
                        }
                    },
                )
                return None

            body_bytes = self.rfile.read(content_length)
            if len(body_bytes) != content_length:
                self._send_json(400, {"error": {"message": "Incomplete request body"}})
                return None

            try:
                raw_body = json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": {"message": f"Invalid JSON request body: {exc}"}})
                return None

            if not isinstance(raw_body, dict):
                self._send_json(400, {"error": {"message": "Request body must be a JSON object"}})
                return None

            return raw_body

        def do_POST(self) -> None:
            self._request_start = time.monotonic()
            self._request_id = _resolve_request_id(self.headers)
            try:
                parsed = urlsplit(self.path)
                path = parsed.path

                if path == "/v1/finops/circuits/reset":
                    ok, _ = self._authorize(required_role=TenantRole.ADMIN)
                    if not ok:
                        return

                    cb = getattr(gateway.adapter, "circuit_breaker", None)
                    if not isinstance(cb, ProviderCircuitBreaker):
                        self._send_json(
                            400,
                            {"error": {"message": "Circuit breaker is not enabled on this gateway"}},
                        )
                        return

                    raw_content_length = self.headers.get("Content-Length")
                    if raw_content_length is None or raw_content_length.strip() == "0":
                        body: dict[str, Any] = {}
                    else:
                        parsed_body = self._read_json_object_body()
                        if parsed_body is None:
                            return
                        body = parsed_body

                    raw_tier = body.get("tier")
                    target_tier: ModelTier | None = None
                    if raw_tier is not None:
                        try:
                            target_tier = ModelTier(str(raw_tier))
                        except ValueError:
                            self._send_json(
                                400, {"error": {"message": f"Invalid model tier: {raw_tier!r}"}}
                            )
                            return

                    cb.reset(target_tier)
                    self._send_json(
                        200,
                        {
                            "reset": True,
                            "tier": target_tier.value if target_tier is not None else "all",
                            **_build_circuits_snapshot(gateway),
                        },
                    )
                    return

                if path != "/v1/chat/completions":
                    self._send_json(404, {"error": {"message": f"Unsupported route POST {path}"}})
                    return

                raw_body = self._read_json_object_body()
                if raw_body is None:
                    return

                if gateway.auth_registry is not None:
                    raw_claimed_org = raw_body.get("x_org_id") or self.headers.get("x-org-id")
                    claimed_org_id = (
                        str(raw_claimed_org) if raw_claimed_org is not None else None
                    )
                    ok, authed_org_id = self._authorize(
                        required_role=TenantRole.ROUTER_CLIENT,
                        claimed_org_id=claimed_org_id,
                    )
                    if not ok:
                        return
                    if authed_org_id is not None:
                        raw_body["x_org_id"] = authed_org_id

                for header_name, field_name in _HEADER_TO_FIELD.items():
                    header_val = self.headers.get(header_name)
                    if header_val and field_name not in raw_body:
                        raw_body[field_name] = header_val

                try:
                    chat_req = ChatCompletionRequest.model_validate(raw_body)
                    server = self.server
                    if not isinstance(server, _RouterHTTPServer):
                        raise TypeError("Expected _RouterHTTPServer")
                    chat_resp = server.loop_runner.run(gateway.handle_chat_completion(chat_req))
                except ThermostatBlockedError as exc:
                    self._send_json(
                        429,
                        {
                            "error": {
                                "type": exc.decision.action.value,
                                "message": exc.decision.explanation,
                                "org_id": exc.decision.org_id,
                                "agent_id": exc.decision.agent_id,
                            }
                        },
                        extra_headers={"Retry-After": "1"},
                    )
                    return
                except (ValueError, KeyError, TypeError) as exc:
                    self._send_json(400, {"error": {"message": str(exc)}})
                    return

                self._send_json(200, chat_resp.model_dump(mode="json"))
            except Exception:
                self._send_json(500, {"error": {"message": "Internal server error"}})

    return _RouterHTTPServer((host, port), _RouterRequestHandler)
