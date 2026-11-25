# ADR-004: Zero-Dependency Local HTTP Server and Non-Root Container Packaging
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Milestones 1 through 3 (`ADR-001`, `ADR-002`, `ADR-003`) delivered the offline 3-tier Pareto replay engine, `OpenAIRouterGateway`, `AgentBudgetThermostat`, OpenTelemetry span recording, and the Prometheus/OpenMetrics `/metrics` exporter. However, running the gateway still required importing `OpenAIRouterGateway` from Python or wiring an external ASGI server (`uvicorn` / `starlette`), and there was no standard container image for local or CI/CD deployment:

1. **Zero-Dependency Local & Container HTTP Serving:** Requiring FastAPI, Starlette, or Uvicorn as mandatory dependencies would bloat the package footprint and complicate air-gapped or minimal deployments when `OpenAIRouterGateway` only needs a small, predictable set of JSON and OpenMetrics endpoints.
2. **Request-Size & Header-Mapping Hardening:** Accepting unbounded `POST /v1/chat/completions` payloads or missing `Content-Length` headers exposes an HTTP server to memory-exhaustion DoS attacks.
3. **Non-Root Container Packaging:** Teams deploying `cost-router serve` in Docker or Kubernetes need a minimal `python:3.12-slim` image that runs as a non-root user (`appuser`), excludes development/build artifacts via `.dockerignore`, and provides a self-contained `HEALTHCHECK` without adding `curl` to the image.

## Decision
1. **Zero-Dependency `ThreadingHTTPServer` (`src/cost_aware_model_router/server.py`):**
   - Implement `create_router_http_server(gateway, *, host="127.0.0.1", port=0) -> ThreadingHTTPServer` using Python's standard-library `http.server.ThreadingHTTPServer` and `BaseHTTPRequestHandler`.
   - Expose:
     - `GET /healthz` -> `200 {"status":"ok"}`
     - `GET /v1/chat/completions` is rejected (`404`); `POST /v1/chat/completions` validates `ChatCompletionRequest` (forwarding `x-org-id`, `x-agent-id`, `x-policy-id`, and `x-shadow-candidate-policy-id` headers when not already present in the JSON body), runs `asyncio.run(gateway.handle_chat_completion(chat_req))`, returns `429` with structured error metadata on `ThermostatBlockedError`, `400` on validation/value errors, and `200` with serialized `ChatCompletionResponse`.
     - `GET /v1/finops/shadow-summary` -> `200` JSON from `gateway.build_shadow_summary()`.
     - `GET /v1/finops/economics` -> `200` JSON from `gateway.summarize_economics(org_id=...)` (supporting optional `?org_id=...` query parameter, returning `400` on `ValueError`).
     - `GET /metrics` -> `200` Prometheus `0.0.4` text (`text/plain; version=0.0.4; charset=utf-8`) via `render_prometheus_metrics()`.
   - Set `Cache-Control: no-store` on all responses and suppress default `BaseHTTPRequestHandler` stderr logging.

2. **1 MiB (`413`) Payload Guard & `Content-Length` Validation (`server.py`):**
   - Enforce `MAX_REQUEST_BODY_BYTES = 1_048_576` (1 MiB) on `POST /v1/chat/completions`.
   - Reject missing, non-integer, or negative `Content-Length` headers with `400`, reject payloads exceeding `MAX_REQUEST_BODY_BYTES` with `413`, and reject truncated bodies (`len(body_bytes) != content_length`) or non-object/invalid JSON with `400`.

3. **`cost-router serve` CLI Subcommand (`src/cost_aware_model_router/cli.py`):**
   - Add `cost-router serve` with `--host` (default `127.0.0.1`), `--port` (default `8080`), `--policy` (default `threshold-balanced`), `--shadow-policy` (optional), and `--hourly-budget-usd` (optional `Decimal` enabling `AgentBudgetThermostat` for `org_demo`).
   - Validate that both primary and shadow policies exist and have `deployable=True` via `_build_gateway_for_serve()`, then bind and run `server.serve_forever()` cleanly with `KeyboardInterrupt` shutdown.

4. **Non-Root Container Packaging (`Dockerfile`, `.dockerignore`):**
   - Base image: `python:3.12-slim` with `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`.
   - Copy `pyproject.toml`, `README.md`, and `src/`, install the package via `pip install --no-cache-dir .`, create non-root user `appuser` (`uid 10001`, `/usr/sbin/nologin`), and switch to `USER appuser`.
   - Expose port `8080`, configure `HEALTHCHECK` using stdlib `urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)`, and set `CMD ["cost-router", "serve", "--host", "0.0.0.0", "--port", "8080"]`.
   - Add `.dockerignore` excluding `.venv/`, `.git/`, `.agents/`, `artifacts/`, `__pycache__/`, `.mypy_cache/`, and `.pytest_cache/` to keep build contexts minimal and prevent leaking local artifacts.

## Alternatives Considered
- **Bundling FastAPI + Uvicorn as Core Dependencies (Rejected):** Adds ~15+ transitive packages and ASGI lifecycle complexity for a lightweight local/sidecar gateway that serves 5 deterministic routes. The existing `create_gateway_asgi_transport` in `gateway.py` remains available for callers who already run an ASGI stack.
- **Running Container as `root` or Installing `curl` for `HEALTHCHECK` (Rejected):** Running as `root` violates container least-privilege best practices, and installing `curl` increases image size and CVE surface when Python's standard library `urllib.request` already probes `/healthz`.

## Consequences & Failure Modes
- **Positive:**
  - `cost-router serve` starts an OpenAI-compatible + FinOps + Prometheus HTTP server out of the box with zero extra runtime dependencies.
  - Containerized deployments run as non-root `appuser` with a built-in `/healthz` healthcheck and bounded `1 MiB` request bodies.
- **Failure Modes & Mitigations:**
  - **Per-request `asyncio.run()` in `ThreadingHTTPServer`:** Each `POST /v1/chat/completions` thread creates a short-lived event loop via `asyncio.run()`, which is suitable for moderate concurrency and deterministic/sidecar workloads; high-throughput production deployments can mount `create_gateway_asgi_transport` onto an async ASGI server.
  - **Chunked Transfer-Encoding Without `Content-Length`:** `BaseHTTPRequestHandler` expects `Content-Length` on `POST` requests and returns `400 Missing Content-Length header` if omitted; callers/proxies must send `Content-Length`.
