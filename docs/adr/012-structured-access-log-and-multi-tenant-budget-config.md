# ADR-012: Structured Access Log, Burst Socket Hardening, Multi-Tenant Budget Config, and 429 Retry-After
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-011`, the local HTTP gateway (`cost-router serve`) shared a single background `asyncio` event loop via `_RouterHTTPServer`, but three production-readiness gaps remained for multi-tenant, high-concurrency deployments:

1. **TCP Burst Queue Drops & Missing Request Telemetry (`server.py`, `cli.py`):** `ThreadingHTTPServer` defaults to `request_queue_size = 5`, `allow_reuse_address = False` (or platform-dependent socket reuse), and non-daemon handler threads, which can drop connections under concurrent bursts and stall process exit. At the same time, `_RouterRequestHandler.log_message` suppressed stderr logging without offering a structured, privacy-safe access log stream for operators.
2. **Single-Tenant Serve Budget Configuration (`cli.py`):** `cost-router serve` supported `--hourly-budget-usd` for a single default envelope, but operators running multi-tenant gateways needed a declarative `--budget-config` JSON file to configure per-`org_id` / per-`agent_id` `AgentBudgetThermostat` policies at startup.
3. **Standard Rate-Limit Backoff Signaling (`server.py`):** Rate-limited (`429`) responses returned JSON error payloads without a standard `Retry-After` HTTP header, forcing clients and OpenAI-compatible SDKs to guess retry backoff intervals.

## Decision
1. **`_RouterHTTPServer` Burst Socket Hardening & Privacy-Safe `access_log` (`server.py`, `cli.py`):**
   - Configure `_RouterHTTPServer` with `allow_reuse_address = True`, `daemon_threads = True`, and `request_queue_size = 128` to absorb concurrent TCP connection bursts, allow immediate port rebinding on restart, and avoid blocking shutdown on hung worker threads.
   - Add an optional `access_log: Callable[[dict[str, Any]], None] | None = None` callback to `create_router_http_server(...)`. When provided, `_RouterRequestHandler._send_bytes` emits a structured record containing `request_id`, `method`, `path` (`urlsplit(self.path).path`, excluding query strings), `status_code`, and `duration_ms`. Sensitive fields (`Authorization` headers, API keys, prompt bodies, and query parameters) are never logged.
   - Expose `--access-log` on `cost-router serve` to enable structured JSON access logging.
2. **Multi-Tenant `--budget-config` JSON Loading on `cost-router serve` (`cli.py`):**
   - Add `--budget-config <path>` to `cost-router serve` to load multi-tenant `AgentBudgetThermostat` budget policies from a JSON configuration file at startup.
3. **Standard `Retry-After: 1` Header on `429` Responses (`server.py`):**
   - Emit `Retry-After: 1` on `429` rate-limit responses from `_RouterHTTPServer` so conforming HTTP clients and SDKs can back off deterministically.

## Alternatives Considered
- **Logging Full Request URLs or Headers in `access_log` (Rejected):** Query parameters and `Authorization` headers can leak bearer tokens or tenant identifiers into log sinks; restricting `path` to `urlsplit(self.path).path` and omitting headers keeps access logs safe by default.
- **Per-Tenant Budgets Only via Code/SDK (Rejected):** Requiring Python code changes to add tenant budget envelopes makes containerized `cost-router serve` deployments rigid; `--budget-config` keeps multi-tenant FinOps configuration declarative alongside `--auth-config`.
- **Custom JSON-Only Retry Hints Without `Retry-After` (Rejected):** Standard HTTP clients, proxies, and OpenAI SDKs inspect the `Retry-After` header directly on `429` responses.

## Consequences & Failure Modes
- **Positive:**
  - Burst traffic up to 128 queued TCP connections is accepted cleanly without premature `ECONNREFUSED` drops, and restarts bind cleanly with `allow_reuse_address = True`.
  - Operators gain structured, correlation-ID-linked (`request_id`) JSON access logs via `--access-log` without risking credential or prompt leakage.
  - Multi-tenant deployments can declaratively load per-tenant budget envelopes via `--budget-config`, and rate-limited clients receive `Retry-After: 1` on `429` responses.
- **Failure Modes & Mitigations:**
  - **Slow or Failing `access_log` Sink:** `access_log` runs synchronously at the end of `_send_bytes`; callers should keep the callback non-blocking (e.g., structured stdout/JSON line writes).
  - **Malformed `--budget-config` JSON:** Invalid JSON or schema mismatches fail fast at `cost-router serve` startup before binding the listening socket.
