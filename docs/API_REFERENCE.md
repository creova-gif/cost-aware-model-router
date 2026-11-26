# Enterprise API Reference

This document specifies the HTTP control-plane and inference gateway API exposed by `cost-router serve` (`create_router_http_server` in `src/cost_aware_model_router/server.py`).

## Base URL & Transport

- **Default bind address:** `http://127.0.0.1:8080` (configurable via `--host` and `--port`)
- **Server implementation:** Zero-dependency Python `ThreadingHTTPServer` (`_RouterHTTPServer`) with `daemon_threads = True`, `request_queue_size = 128`, and a shared background `asyncio` event loop (`_AsyncLoopRunner`).
- **Maximum request body size:** `1,048,576` bytes (`MAX_REQUEST_BODY_BYTES = 1 MiB`). Requests exceeding this limit receive `413 Payload Too Large`.
- **`Content-Length` requirement:** JSON `POST` requests to `/v1/chat/completions` (and non-empty `POST` bodies to `/v1/finops/circuits/reset`) require a valid non-negative integer `Content-Length` header; missing or malformed values return `400 Bad Request`.

---

## Authentication, RBAC & Rate Limiting

When `--auth-config` is supplied to `cost-router serve`, all protected routes enforce Bearer token authentication (`Authorization: Bearer <token>`) via `TenantAuthRegistry`.

### `TenantRole` Permissions

| Role (`TenantRole`) | Value | Capabilities |
| :--- | :--- | :--- |
| `ROUTER_CLIENT` | `router_client` | `/v1/models`, `/v1/models/{model_id}`, `/v1/chat/completions` |
| `AUDITOR` | `auditor` | `/v1/finops/economics`, `/v1/finops/shadow-summary`, `/v1/finops/circuits`, `/metrics`, `/dashboard` |
| `ADMIN` | `admin` | All `ROUTER_CLIENT` and `AUDITOR` endpoints, plus `POST /v1/finops/circuits/reset` |

### Tenant Isolation

- **Cross-tenant spoofing protection:** If a `ROUTER_CLIENT` supplies `x_org_id` in the JSON body or `X-Org-Id` in the request headers (or an `AUDITOR` supplies `?org_id=` on `/v1/finops/economics`) that differs from the authenticated key's `org_id`, the server rejects the request with `403 Forbidden` (`"Cross-tenant org_id spoofing rejected"`).
- **Authoritative org binding:** On authenticated `POST /v1/chat/completions` requests, `x_org_id` is bound to the authenticated `org_id`.

### Rate Limiting & `429` Behavior

- Each `TenantKeyRecord` configures a per-`org_id` token bucket (`rate_limit_rps`, default `20.0`; `burst_capacity`, default `40`).
- **Rate-limit exhaustion:** Returns `429` with header `Retry-After: 1` and body:
  ```json
  {"error":{"message":"Tenant rate limit exceeded"}}
  ```
- **Thermostat blocking (`ThermostatBlockedError`):** When `AgentBudgetThermostat` triggers `loop_circuit_breaker` or `budget_circuit_breaker` on `POST /v1/chat/completions`, the server also returns `429` with header `Retry-After: 1` and body:
  ```json
  {
    "error": {
      "type": "budget_circuit_breaker",
      "message": "...",
      "org_id": "org_acme",
      "agent_id": "planner"
    }
  }
  ```

---

## Security & Correlation Headers

Every HTTP response emitted by the server includes the following headers:

| Header | Value / Behavior |
| :--- | :--- |
| `X-Request-Id` | Echoes the incoming `X-Request-Id` header if it matches `[A-Za-z0-9._-]+` and length `1..128`; otherwise generates a fresh UUID4 hex string. |
| `Cache-Control` | `no-store` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `no-referrer` |
| `Permissions-Policy` | `geolocation=(), microphone=(), camera=()` |
| `Content-Security-Policy` | Applied on `GET /dashboard`: `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'` |

---

## Endpoint Summary

| Method(s) | Path | Minimum `TenantRole` | Content-Type |
| :--- | :--- | :--- | :--- |
| `GET`, `HEAD` | `/healthz` | Public | `application/json; charset=utf-8` |
| `GET`, `HEAD` | `/readyz` | Public | `application/json; charset=utf-8` |
| `GET`, `HEAD` | `/openapi.json` | Public | `application/json; charset=utf-8` |
| `GET`, `HEAD` | `/v1/models` | `ROUTER_CLIENT` | `application/json; charset=utf-8` |
| `GET`, `HEAD` | `/v1/models/{model_id}` | `ROUTER_CLIENT` | `application/json; charset=utf-8` |
| `POST` | `/v1/chat/completions` | `ROUTER_CLIENT` | `application/json; charset=utf-8` or `text/event-stream; charset=utf-8` |
| `GET`, `HEAD` | `/v1/finops/economics` | `AUDITOR` | `application/json; charset=utf-8` |
| `GET`, `HEAD` | `/v1/finops/shadow-summary` | `AUDITOR` | `application/json; charset=utf-8` |
| `GET`, `HEAD` | `/v1/finops/circuits` | `AUDITOR` | `application/json; charset=utf-8` |
| `POST` | `/v1/finops/circuits/reset` | `ADMIN` | `application/json; charset=utf-8` |
| `GET`, `HEAD` | `/metrics` | `AUDITOR` | `text/plain; version=0.0.4; charset=utf-8` |
| `GET`, `HEAD` | `/dashboard` | `AUDITOR` | `text/html; charset=utf-8` |

> **Note:** `HEAD` requests execute the same routing, status codes, and response headers (including `Content-Length`) as `GET`, omitting the response body.

---

## Endpoint Reference

### `GET` / `HEAD /healthz`

Liveness probe indicating the HTTP server process is responsive.

- **Auth:** None
- **Responses:**
  - `200 OK`: `{"status":"ok"}`

### `GET` / `HEAD /readyz`

Readiness probe verifying the economics store (if configured) and provider circuit breakers.

- **Auth:** None
- **Responses:**
  - `200 OK`: `{"status":"ready"}`
  - `503 Service Unavailable`:
    - `{"status":"not_ready","reason":"economics_store_unhealthy"}` when `SQLiteEconomicsStore.is_healthy()` returns `False`.
    - `{"status":"not_ready","reason":"all_tier_circuits_open"}` when `ProviderCircuitBreaker` is enabled and every tier (`compact`, `mid`, `frontier`) is in `open` state.

### `GET` / `HEAD /openapi.json`

Returns the OpenAPI `3.1.0` specification document generated by `build_openapi_spec()`.

- **Auth:** None
- **Responses:**
  - `200 OK`: OpenAPI `3.1.0` JSON document describing `BearerAuth` and all server paths.

### `GET` / `HEAD /v1/models`

Lists all models in the active `PricingCatalog`.

- **Auth:** `TenantRole.ROUTER_CLIENT`
- **Responses:**
  - `200 OK`:
    ```json
    {
      "object": "list",
      "data": [
        {
          "id": "gpt-4o-mini",
          "object": "model",
          "owned_by": "openai",
          "tier": "compact"
        }
      ]
    }
    ```
  - `401` / `403` / `429`: `{"error":{"message":"..."}}`

### `GET` / `HEAD /v1/models/{model_id}`

Retrieves metadata for a single catalog model by `model_id` (URL-decoded).

- **Auth:** `TenantRole.ROUTER_CLIENT`
- **Responses:**
  - `200 OK`: `{"id":"...","object":"model","owned_by":"...","tier":"compact|mid|frontier"}`
  - `404 Not Found`: `{"error":{"message":"Model '...' not found"}}`

### `POST /v1/chat/completions`

OpenAI-compatible routed chat completion endpoint supporting both synchronous JSON and Server-Sent Events (`stream=True`).

- **Auth:** `TenantRole.ROUTER_CLIENT`
- **Optional Routing Headers** (used when corresponding body fields are omitted):
  - `X-Org-Id` → `x_org_id`
  - `X-Agent-Id` → `x_agent_id`
  - `X-Policy-Id` → `x_policy_id`
  - `X-Shadow-Candidate-Policy-Id` → `x_shadow_candidate_policy_id`
- **Request Schema (`ChatCompletionRequest`, `extra="forbid"`):**
  - `model` (`string`, default `"auto"`)
  - `messages` (`list[ChatMessage]`, required, min 1 item; each with `role`: `"system" | "user" | "assistant" | "developer" | "tool"` and non-empty `content`)
  - `temperature` (`float`, `0.0..2.0`, default `0.0`)
  - `top_p` (`float | null`, `0.0..1.0`)
  - `max_tokens` (`int | null`, `>= 1`)
  - `stop` (`string | list[string] | null`)
  - `user` (`string | null`)
  - `seed` (`int | null`)
  - `stream` (`bool`, default `false`)
  - `request_id` (`string | null`)
  - `category` (`TaskCategory | null`)
  - `x_org_id`, `x_agent_id`, `x_policy_id`, `x_shadow_candidate_policy_id` (`string | null`)
- **Response (`stream=false`, `200 OK`, `application/json; charset=utf-8`):**
  Returns `ChatCompletionResponse`:
  ```json
  {
    "id": "chatcmpl-...",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gpt-4o-mini",
    "choices": [
      {
        "index": 0,
        "message": {"role": "assistant", "content": "..."},
        "finish_reason": "stop"
      }
    ],
    "usage": {
      "prompt_tokens": 24,
      "completion_tokens": 12,
      "total_tokens": 36,
      "cost_usd": "0.00001080",
      "cost_provenance": "modeled"
    },
    "router_metadata": {
      "request_id": "req-001",
      "org_id": "org_demo",
      "agent_id": "default_agent",
      "policy_id": "threshold-balanced",
      "selected_tier": "compact",
      "routed_model": "gpt-4o-mini",
      "latency_ms": 18.4,
      "trace_id": "...",
      "span_id": "...",
      "shadow_evaluated": false,
      "shadow_policy_id": null,
      "thermostat_action": "allow_requested_tier"
    }
  }
  ```
- **Response (`stream=true`, `200 OK`, `text/event-stream; charset=utf-8`):**
  Emits three SSE frames:
  1. `data: {"id":"...","object":"chat.completion.chunk","created":...,"model":"...","choices":[{"index":0,"delta":{"role":"assistant","content":"..."},"finish_reason":null}]}\n\n`
  2. `data: {"id":"...","object":"chat.completion.chunk","created":...,"model":"...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{...},"x_router_metadata":{...}}\n\n`
  3. `data: [DONE]\n\n`
- **Error Responses:** `400` (validation/JSON error), `401` (unauthenticated), `403` (RBAC/tenant spoofing), `413` (body > 1 MiB), `429` (`Retry-After: 1` for rate limit or thermostat block).

### `GET` / `HEAD /v1/finops/economics`

Returns a `SpendSummary` JSON snapshot from `AgentBudgetThermostat` or `SQLiteEconomicsStore`.

- **Auth:** `TenantRole.AUDITOR`
- **Query Parameters:** `org_id` (optional; subject to cross-tenant isolation check)
- **Responses:**
  - `200 OK`: `SpendSummary` (`total_requests`, `passed_requests`, `failed_requests`, `cache_hits`, `total_cost_usd`, `measured_cost_usd`, `modeled_cost_usd`, `unknown_cost_usd`, `cost_per_request_usd`, `cost_per_successful_outcome_usd`, `spend_by_org`, `spend_by_agent`, `spend_by_category`, `spend_by_tier`).
  - `400 Bad Request`: Returned if neither thermostat nor `SQLiteEconomicsStore` is configured.

### `GET` / `HEAD /v1/finops/shadow-summary`

Summarizes shadow policy evaluations vs. the primary routing policy.

- **Auth:** `TenantRole.AUDITOR`
- **Responses:**
  - `200 OK`:
    ```json
    {
      "total_shadow_requests": 10,
      "downgrade_count": 4,
      "primary_total_cost_usd": "0.01200000",
      "shadow_total_cost_usd": "0.00450000",
      "shadow_cost_delta_usd": "-0.00750000",
      "records": []
    }
    ```

### `GET` / `HEAD /v1/finops/circuits`

Returns current `ProviderCircuitBreaker` states per tier.

- **Auth:** `TenantRole.AUDITOR`
- **Responses:**
  - `200 OK` (when `--enable-fallback` is active):
    ```json
    {"enabled":true,"circuits":{"compact":"closed","mid":"closed","frontier":"closed"}}
    ```
  - `200 OK` (when disabled): `{"enabled":false,"circuits":{}}`

### `POST /v1/finops/circuits/reset`

Resets one or all tier circuit breakers to `closed`.

- **Auth:** `TenantRole.ADMIN`
- **Request Body:** Optional JSON object `{"tier": "compact" | "mid" | "frontier"}`. An empty body (or omitted `tier`) resets all tiers.
- **Responses:**
  - `200 OK`:
    ```json
    {
      "reset": true,
      "tier": "all",
      "enabled": true,
      "circuits": {"compact":"closed","mid":"closed","frontier":"closed"}
    }
    ```
  - `400 Bad Request`: Circuit breaker is not enabled on the gateway, or invalid `tier`.

### `GET` / `HEAD /metrics`

Exports Prometheus / OpenMetrics `0.0.4` text format metrics (`text/plain; version=0.0.4; charset=utf-8`).

- **Auth:** `TenantRole.AUDITOR`
- **Exposed Metrics:**
  - `cost_router_requests_total{org_id,tier,provenance,shadow_mode}` (`counter`)
  - `cost_router_spend_usd_total{org_id,tier,provenance}` (`counter`)
  - `cost_router_latency_ms_sum{org_id,tier}` & `cost_router_latency_ms_count{org_id,tier}` (`summary`)
  - `cost_router_cache_hits_total` (`counter`, when thermostat is enabled)
  - `cost_router_goodput_usd` (`gauge`, when thermostat is enabled and `passed_requests > 0`)
  - `cost_router_circuit_state{tier}` (`gauge`: `0=closed`, `1=half_open`, `2=open`, when circuit breaker is enabled)

### `GET` / `HEAD /dashboard`

Serves the server-rendered Live FinOps HTML dashboard (`text/html; charset=utf-8`) with the strict `Content-Security-Policy` header listed above.

- **Auth:** `TenantRole.AUDITOR`
- **Responses:** `200 OK` HTML page containing economics totals, shadow policy savings, circuit-breaker states, and the 50 most recent OTel spans.
