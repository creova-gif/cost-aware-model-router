from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    OpenAIRouterGateway,
    ThermostatBlockedError,
)
from cost_aware_model_router.metrics import render_prometheus_metrics

MAX_REQUEST_BODY_BYTES = 1_048_576

_HEADER_TO_FIELD: dict[str, str] = {
    "x-org-id": "x_org_id",
    "x-agent-id": "x_agent_id",
    "x-policy-id": "x_policy_id",
    "x-shadow-candidate-policy-id": "x_shadow_candidate_policy_id",
}


def create_router_http_server(
    gateway: OpenAIRouterGateway,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Create a zero-dependency local ThreadingHTTPServer backed by OpenAIRouterGateway."""

    class _RouterRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Suppress default stderr request logging; callers/CLI control output.
            return

        def _send_bytes(self, status_code: int, content_type: str, body: bytes) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._send_bytes(status_code, "application/json; charset=utf-8", encoded)

        def _send_text(self, status_code: int, content_type: str, text: str) -> None:
            self._send_bytes(status_code, content_type, text.encode("utf-8"))

        def do_GET(self) -> None:
            try:
                parsed = urlsplit(self.path)
                path = parsed.path

                if path == "/healthz":
                    self._send_json(200, {"status": "ok"})
                    return

                if path == "/v1/finops/shadow-summary":
                    self._send_json(200, gateway.build_shadow_summary())
                    return

                if path == "/v1/finops/economics":
                    query_params = parse_qs(parsed.query, keep_blank_values=False)
                    org_id_values = query_params.get("org_id")
                    org_id = org_id_values[0] if org_id_values else None
                    try:
                        economics = gateway.summarize_economics(org_id=org_id)
                    except ValueError as exc:
                        self._send_json(400, {"error": {"message": str(exc)}})
                        return
                    self._send_json(200, economics)
                    return

                if path == "/metrics":
                    spans = gateway.span_exporter.get_spans()
                    summary = (
                        gateway._thermostat.summarize_economics()
                        if gateway._thermostat is not None
                        else None
                    )
                    metrics_text = render_prometheus_metrics(spans, summary=summary)
                    self._send_text(
                        200,
                        "text/plain; version=0.0.4; charset=utf-8",
                        metrics_text,
                    )
                    return

                self._send_json(404, {"error": {"message": f"Unsupported route GET {path}"}})
            except Exception:
                self._send_json(500, {"error": {"message": "Internal server error"}})

        def do_POST(self) -> None:
            try:
                parsed = urlsplit(self.path)
                path = parsed.path

                if path != "/v1/chat/completions":
                    self._send_json(404, {"error": {"message": f"Unsupported route POST {path}"}})
                    return

                raw_content_length = self.headers.get("Content-Length")
                if raw_content_length is None:
                    self._send_json(400, {"error": {"message": "Missing Content-Length header"}})
                    return
                try:
                    content_length = int(raw_content_length)
                except ValueError:
                    self._send_json(400, {"error": {"message": "Invalid Content-Length header"}})
                    return

                if content_length < 0:
                    self._send_json(400, {"error": {"message": "Invalid Content-Length header"}})
                    return

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
                    return

                body_bytes = self.rfile.read(content_length)
                if len(body_bytes) != content_length:
                    self._send_json(400, {"error": {"message": "Incomplete request body"}})
                    return

                try:
                    raw_body = json.loads(body_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._send_json(
                        400, {"error": {"message": f"Invalid JSON request body: {exc}"}}
                    )
                    return

                if not isinstance(raw_body, dict):
                    self._send_json(
                        400, {"error": {"message": "Request body must be a JSON object"}}
                    )
                    return

                for header_name, field_name in _HEADER_TO_FIELD.items():
                    header_val = self.headers.get(header_name)
                    if header_val and field_name not in raw_body:
                        raw_body[field_name] = header_val

                try:
                    chat_req = ChatCompletionRequest.model_validate(raw_body)
                    chat_resp = asyncio.run(gateway.handle_chat_completion(chat_req))
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
                    )
                    return
                except (ValueError, KeyError, TypeError) as exc:
                    self._send_json(400, {"error": {"message": str(exc)}})
                    return

                self._send_json(200, chat_resp.model_dump(mode="json"))
            except Exception:
                self._send_json(500, {"error": {"message": "Internal server error"}})

    return ThreadingHTTPServer((host, port), _RouterRequestHandler)
