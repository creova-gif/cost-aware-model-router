# ADR-009: Enterprise Auth Config, Readiness Probes, Correlation Headers, and Dashboard Circuit Health
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-006` (`TenantAuthRegistry`, RBAC), `ADR-007` (`ProviderCircuitBreaker`, `FallbackChainAdapter`), and `ADR-008` (`GET /v1/models`, `GET /v1/finops/circuits`, `serve` resilience flags), three operational gaps remained for production and containerized deployments of `cost-router serve`:

1. **Persistent Tenant Auth Configuration & Key Provisioning:** `TenantAuthRegistry` previously existed only in memory, meaning API keys issued for `ROUTER_CLIENT` and `AUDITOR` roles could not be persisted across restarts or provisioned declaratively via CLI tooling and `--auth-config`.
2. **Production HTTP Server Hardening:** Container orchestrators (Kubernetes, Cloud Run, ECS) require an unauthenticated readiness probe (`/readyz`) distinct from `/healthz`, distributed tracing requires `X-Request-Id` correlation propagation, and browser/API responses need standard security headers (`X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `X-Frame-Options: DENY`).
3. **Live Dashboard Circuit-Breaker Visibility:** Although `GET /v1/finops/circuits` exposed per-`ModelTier` circuit states (`closed`, `half_open`, `open`), the self-contained FinOps dashboard (`render_live_finops_dashboard_html`) did not embed or refresh circuit-breaker health status.

## Decision
1. **Persistent Hashed `TenantAuthRegistry` JSON Config & `cost-router issue-key` CLI:**
   - Serialize `TenantAuthRegistry` entries to a JSON config file storing only cryptographic hashes (never plaintext bearer tokens), `org_id`, and `role` (`ROUTER_CLIENT` / `AUDITOR`).
   - Add `cost-router issue-key` CLI subcommand to generate a new API key, write/update the hashed JSON config file, and print the raw token once to stdout.
   - Support `--auth-config` on `cost-router serve` to load the persisted hashed registry at startup.

2. **HTTP Server `/readyz` Probe, `X-Request-Id` Correlation, and Security Headers:**
   - Expose `GET /readyz` returning `200 OK` when the gateway and configured dependencies (e.g., SQLite store, provider registry) are ready to serve traffic, or `503 Service Unavailable` otherwise.
   - Propagate or generate an `X-Request-Id` header on every HTTP response for end-to-end log and trace correlation.
   - Attach baseline security response headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Cache-Control: no-store`) to HTTP responses.

3. **Live Dashboard Circuit-Breaker Health Visibility (`src/cost_aware_model_router/dashboard.py`):**
   - Extend `render_live_finops_dashboard_html(..., circuits: dict[str, Any] | None = None)` to embed `"circuits"` (defaulting to `{"enabled": False, "circuits": {}}`) into the XSS-escaped `#finops-snapshot` JSON payload via `escape_json_for_script_tag`.
   - Render a **Circuit Breaker Health** panel showing each tier's state (`closed`, `half_open`, `open`) or `"Circuit breaker disabled"` when `enabled` is `false`.
   - Update `refreshLiveMetrics()` in the dashboard script to fetch `/v1/finops/circuits` alongside `/v1/finops/economics` and `/v1/finops/shadow-summary`, gracefully tolerating `401`/`403`/`404` responses when unauthenticated or when the endpoint is unavailable.

## Alternatives Considered
- **Storing Plaintext API Keys in `--auth-config` (Rejected):** Storing raw bearer tokens on disk creates unnecessary credential-leakage risk; storing salted/hashed digests matches standard API key management and limits blast radius if the config file is exposed.
- **Reusing `/healthz` for Dependency Readiness (Rejected):** Liveness and readiness have different semantics in orchestrators; separating `/readyz` prevents premature traffic routing before stores or adapters initialize while keeping `/healthz` lightweight.
- **Polling `/v1/finops/circuits` Without Embedded Snapshot (Rejected):** Embedding initial circuit state in `#finops-snapshot` ensures zero-flicker initial rendering and offline HTML export parity with economics, shadow summary, and spans.

## Consequences & Failure Modes
- **Positive:**
  - Operators can provision, persist, and rotate tenant API keys without code changes via `cost-router issue-key` and `cost-router serve --auth-config`.
  - Orchestrators and reverse proxies gain standard `/readyz` readiness checks, `X-Request-Id` correlation, and hardened response headers.
  - FinOps and SRE teams can inspect per-tier circuit breaker health directly in `/dashboard` with zero external CDN dependencies.
- **Failure Modes & Mitigations:**
  - **Lost Plaintext Key After `issue-key`:** Because only the hash is persisted, a lost token cannot be recovered; operators simply issue a replacement key via `cost-router issue-key`.
  - **Unauthenticated Dashboard Refresh on `/v1/finops/circuits`:** If a static or unauthenticated viewer triggers refresh and receives `401`/`403`/`404`, the dashboard preserves its last known circuit snapshot without failing the entire refresh cycle.
  - **XSS Breakout in Circuit Tier Names/States:** All embedded snapshot data passes through `escape_json_for_script_tag`, and DOM rendering uses `textContent` exclusively without `innerHTML`.
