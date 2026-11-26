# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Structured Access Log, Burst Socket Hardening, Multi-Tenant Budget Config & `429 Retry-After` (`server.py`, `cli.py`):**
  - Hardened `_RouterHTTPServer` for burst traffic (`allow_reuse_address = True`, `daemon_threads = True`, `request_queue_size = 128`) and added a privacy-safe `access_log` callback (`--access-log` on `cost-router serve`) emitting `request_id`, `method`, `path`, `status_code`, and `duration_ms` without logging credentials or request bodies.
  - Added `--budget-config` JSON loading on `cost-router serve` for multi-tenant `AgentBudgetThermostat` budget envelopes.
  - Added the standard `Retry-After: 1` header on `429` rate-limit responses.
  - Documented these decisions in `docs/adr/012-structured-access-log-and-multi-tenant-budget-config.md` (`ADR-012`).
- **HTTP `HEAD` Probes & Atomic Tenant Key Rotation (`server.py`, `governance.py`, `cli.py`):**
  - Added `HEAD` probe support on `/healthz`, `/readyz`, and `GET` routes in `_RouterRequestHandler` (`HEAD /healthz` and `HEAD /readyz` return status codes and headers without a response body for load balancers).
  - Added atomic `TenantAuthRegistry.rotate_key()` and `cost-router rotate-key` / `cost-router list-keys` CLI subcommands for key rotation and non-secret key inventory inspection.
  - Documented these decisions in `docs/adr/013-head-probes-and-key-rotation.md` (`ADR-013`).
- **OpenAI-Compatible SSE Streaming & Atomic `0o600` Auth-Config Writes (`gateway.py`, `server.py`, `cli.py`):**
  - Added OpenAI-compatible `stream=True` Server-Sent Events (`text/event-stream; charset=utf-8`) framing on `POST /v1/chat/completions`, emitting `chat.completion.chunk` events followed by `data: [DONE]\n\n` while preserving thermostat, shadow-mode, and OTel accounting.
  - Added `_write_auth_config_atomic` in `cli.py` to write `auth-config.json` atomically with `0o600` permissions for `cost-router issue-key`, `cost-router revoke-key`, and `cost-router rotate-key`.
  - Documented these decisions in `docs/adr/014-openai-sse-streaming-and-atomic-auth-writes.md` (`ADR-014`).
- **WCAG 2.1 AA Accessibility, SQLite WAL Lifecycle, Enterprise Governance & Performance Regression Suite (`dashboard.py`, `report.py`, `store.py`):**
  - Upgraded `render_live_finops_dashboard_html` (`dashboard.py`) and `render_pareto_explorer_html` (`report.py`) to WCAG 2.1 AA with semantic `<header>` / `<main id="main-content">` landmarks, `.skip-link` navigation, `:focus-visible` outlines, `@media (prefers-reduced-motion: reduce)`, `aria-live="polite"` status regions, `scope="col"` table headers, `aria-label` on `#quality-slider` / `#pareto-svg`, and synchronized `aria-pressed` filter tabs.
  - Fixed the `. btn-active:hover` CSS selector typo to `.btn-active:hover` in `report.py`.
  - Guarded `SQLiteEconomicsStore.__enter__` with `_ensure_open()` to reject re-entering a closed store, and executed `PRAGMA wal_checkpoint(TRUNCATE)` on `SQLiteEconomicsStore.close()`.
  - Added enterprise repository governance (`SECURITY.md`, `CODEOWNERS`, `.env.example`, `.github/dependabot.yml`) and performance regression tests.
  - Documented these decisions in `docs/adr/015-wcag-accessibility-and-wal-lifecycle.md` (`ADR-015`).
- **Security Headers, OpenAI SDK Interoperability, OpenAPI `3.1.0` Spec & CI Smoke Checks (`server.py`, `gateway.py`, `.github/workflows/ci.yml`):**
  - Added `Permissions-Policy` (`geolocation=(), microphone=(), camera=()`) to all HTTP responses and a strict `Content-Security-Policy` header to `GET /dashboard`.
  - Added OpenAI SDK optional interoperability fields (`top_p`, `stop`, `user`, `seed`) on `ChatCompletionRequest` (`POST /v1/chat/completions`) while preserving `extra="forbid"`.
  - Added the OpenAPI `3.1.0` specification endpoint (`GET /openapi.json`) and single-model lookup endpoint (`GET /v1/models/{model_id}`).
  - Expanded `.github/workflows/ci.yml` smoke checks to run `cost-router --version` and `cost-router train-bandit --epochs 2 --out ./artifacts/ci-demo/bandit`.
  - Documented these decisions in `docs/adr/016-csp-openai-interop-and-openapi-spec.md` (`ADR-016`).

## [0.4.0] - 2026-11-26

### Added

- **OpenAI Model Discovery & Circuit Observability (`server.py`, `resilience.py`):**
  - `GET /v1/models` (`TenantRole.ROUTER_CLIENT` role) returning OpenAI-compatible model discovery metadata for `"auto"` and configured tier models.
  - `GET /v1/finops/circuits` (`TenantRole.AUDITOR` role) exposing per-`ModelTier` circuit breaker state and counters via `ProviderCircuitBreaker.snapshot()`.
- **`cost-router serve` Resilience & Bandit Flags (`cli.py`):**
  - `--enable-fallback` and `--circuit-failure-threshold` to wrap the serving adapter in `FallbackChainAdapter` + `ProviderCircuitBreaker`.
  - `--bandit-policy-file` to load a calibrated `ContextualBanditRouter` JSON snapshot at gateway startup.
- **Repository Hygiene & Documentation:**
  - Added MIT `LICENSE`, `.github/` issue & pull request templates, and `README.md` CI/version/license badges, CLI command reference, and endpoint docs.
  - Documented the discovery, circuit observability, and `serve` wiring decisions in `docs/adr/008-openai-models-discovery-and-circuit-observability.md` (`ADR-008`).
- **Enterprise Auth Config, Readiness Probe, Correlation & Dashboard Circuit Health (`server.py`, `cli.py`, `governance.py`, `dashboard.py`):**
  - Added `GET /readyz` readiness probe returning `200` (`{"status":"ready"}`) or `503` (`{"status":"not_ready","reason":"all_tier_circuits_open"}`) when all tier circuits in `ProviderCircuitBreaker` are open.
  - Added `X-Request-Id` request correlation propagation/generation and security headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`) to HTTP responses.
  - Added `TenantAuthRegistry` JSON persistence (`to_json` / `from_json`), `cost-router issue-key` CLI subcommand, and `cost-router serve --auth-config` startup loading.
  - Added a **Circuit Breaker Health** panel to `GET /dashboard` (`render_live_finops_dashboard_html`) with live `/v1/finops/circuits` polling.
  - Documented these decisions in `docs/adr/009-enterprise-auth-config-readiness-and-correlation.md` (`ADR-009`).
- **Live Serve Provider Wiring, Key Revocation, Graceful Shutdown & Store Readiness (`cli.py`, `store.py`, `server.py`):**
  - Added `cost-router revoke-key` CLI subcommand (`--key-id`, `--auth-config`) to disable tenant virtual API keys (`enabled=false`) while preserving audit history.
  - Added `SIGTERM` graceful shutdown handling to `cost-router serve` so `server.shutdown()`, `server.server_close()`, and `SQLiteEconomicsStore.close()` run cleanly on container stop.
  - Shared `_build_live_adapter()` across `cost-router replay --live` and `cost-router serve --live --provider openai|anthropic`.
  - Added `SQLiteEconomicsStore.is_healthy()` and wired it into `GET /readyz` (returning `503` when the SQLite store is unhealthy or all tier circuits are open).
  - Documented these decisions in `docs/adr/010-live-serve-provider-and-store-readiness.md` (`ADR-010`).
- **Shared Event Loop, Store Retention Pruning & Circuit Breaker Reset (`server.py`, `store.py`, `resilience.py`, `cli.py`):**
  - Added `_AsyncLoopRunner` and `_RouterHTTPServer` in `server.py` to run `POST /v1/chat/completions` coroutines on a single shared background `asyncio` event loop instead of per-request `asyncio.run(...)`.
  - Added `SQLiteEconomicsStore.prune_older_than()` and `cost-router prune-db` to prune old `shadow_comparisons` and `otel_spans` rows while preserving `spend_ledger` totals.
  - Added `ProviderCircuitBreaker.reset()` and `POST /v1/finops/circuits/reset` (`TenantRole.ADMIN` RBAC) to reset one or all tier circuits to `CLOSED`.
  - Documented these decisions in `docs/adr/011-shared-event-loop-retention-and-circuit-reset.md` (`ADR-011`).
- **Prometheus Circuit Gauge, CLI Version Flag & Docker Compose (`metrics.py`, `cli.py`, `docker-compose.yml`):**
  - Added `cost_router_circuit_state{tier="..."}` Prometheus gauge (`0=closed`, `1=half_open`, `2=open`) to `render_prometheus_metrics` and `GET /metrics`.
  - Added `cost-router --version` / `-V` CLI flag reporting the installed package version.
  - Added `docker-compose.yml` and container operations documentation in `README.md`.

## [0.3.0] - 2026-11-26

### Added

- **Quality-Aware Contextual Bandit Router (`learner.py`):**
  - `ContextualBanditRouter`, `ContextualBanditConfig`, `ArmStats`, and `ComplexityBucket` (`low`, `medium`, `high` via `bucket_for_score`) implementing zero-dependency UCB1 exploration + quality-floor-aware goodput optimization across `(TaskCategory, ComplexityBucket)` contexts.
  - Deterministic `export_learned_thresholds()`, `to_json()`, and `from_json()` snapshot persistence.
- **Multi-Tenant API Governance & Rate Limiting (`governance.py`):**
  - `TenantAuthRegistry`, `TenantKeyRecord`, `TenantRole` (`admin`, `router_client`, `auditor`), `AuthDecision`, and `TokenBucketRateLimiter` providing SHA-256 virtual API keys verified with `hmac.compare_digest`, RBAC enforcement, cross-tenant `org_id` anti-spoofing, and thread-safe per-tenant token-bucket rate limiting.
- **Provider Circuit Breakers, Fallback Chains & Bounded TTL Cache (`resilience.py`):**
  - `ProviderCircuitBreaker` (`CircuitState`: `CLOSED -> OPEN -> HALF_OPEN -> CLOSED`, `CircuitBreakerConfig`) providing thread-safe per-`ModelTier` failure isolation with configurable failure/success thresholds and recovery timeout.
  - `FallbackChainAdapter` wrapping any `ProviderAdapter` with circuit-breaker gating, `DEFAULT_FALLBACK_ORDER` cross-tier fallback, authentic trace provenance preservation, and `ProvenanceTag.UNKNOWN` (`error_code="all_fallback_tiers_unavailable"`) emission when all candidate tiers are unavailable.
  - `BoundedTTLCache` providing thread-safe `OrderedDict`-backed O(1) LRU + TTL semantic cache eviction keyed by `(org_id, prompt_sha256)`.
- **Contextual Bandit Calibration CLI (`cli.py`):**
  - `cost-router train-bandit` subcommand (`--out`, `--epochs`, `--quality-floor`) calibrating `ContextualBanditRouter` over the bundled 60-prompt workload and exporting `bandit-policy.json`.
- **Top-Level Package Exports & Architecture Record:**
  - Exported `ArmStats`, `AuthDecision`, `ComplexityBucket`, `ContextualBanditConfig`, `ContextualBanditRouter`, `TenantAuthRegistry`, `TenantKeyRecord`, `TenantRole`, and `TokenBucketRateLimiter` in `cost_aware_model_router.__init__`.
  - Documented the design and failure modes in `docs/adr/006-contextual-bandit-and-tenant-governance.md` (`ADR-006`) and `docs/adr/007-provider-circuit-breakers-fallback-and-ttl-cache.md` (`ADR-007`).

## [0.2.0] - 2026-11-26

### Added

- **Heterogeneous Multi-Provider Routing (`registry.py`):**
  - `MultiProviderAdapter` routing `compact`, `mid`, and `frontier` tiers across heterogeneous `ProviderAdapter` backends via explicit `tier_adapters` overrides, `TierModelSpec.provider` (`provider_adapters`), and `fallback_adapter`.
  - Identity-based adapter grouping (`id(adapter)`) with concurrent `asyncio.gather` execution and typed `ProvenanceTag.UNKNOWN` (`error_code="missing_provider_adapter"`) fallback traces for unconfigured tiers.
- **Live FinOps Control-Plane Dashboard (`dashboard.py`, `server.py`):**
  - `render_live_finops_dashboard_html` generating a zero-CDN, XSS-hardened HTML control plane with thermostat unit-economics KPIs, spend breakdowns (by org, agent, category, and tier), shadow-mode counterfactual comparisons, recent OpenTelemetry `gen_ai.*` spans, and live client-side refresh.
  - `GET /dashboard` route on `create_router_http_server` (`text/html; charset=utf-8`, `Cache-Control: no-store`).
- **Durable SQLite Economics Persistence (`store.py`):**
  - `SQLiteEconomicsStore` providing thread-safe, WAL-mode SQLite persistence for `SpendLedgerEntry`, `ShadowComparisonRecord`, and `OTelSpanRecord` with 8-place USD string precision and `SpendSummary` aggregation.
- **Top-Level Package Exports & Architecture Record:**
  - Exported `MultiProviderAdapter`, `SQLiteEconomicsStore`, and `render_live_finops_dashboard_html` in `cost_aware_model_router.__init__`.
  - Documented the design and failure modes in `docs/adr/005-multi-provider-registry-and-live-dashboard.md` (`ADR-005`).

## [0.1.0] - 2026-11-25

### Added

- **Offline 3-Tier Replay & 2D Pareto Optimization (`cost-router demo`, `cost-router replay`):**
  - `DeterministicFixtureAdapter` for zero-API-key deterministic 3-tier replay (`compact`, `mid`, `frontier`) over the bundled 60-prompt `support-and-code-v1.jsonl` workload or custom JSONL workloads.
  - Deployable routing policies (`static_tier`, `complexity_threshold`, `cascade_escalation` with `latency_sla` short-circuiting), complete-case `UNKNOWN` intersection, and the non-deployable `ear-oracle` ($\tau$-equivalence) hindsight benchmark.
  - Exact 2D Pareto frontier solver (`compute_2d_pareto_frontier`, `select_policy_for_floor`) optimizing goodput (`cost_per_successful_outcome_usd`) under explicit quality floors ($q_{\min}$).
  - Self-contained, zero-CDN, XSS-hardened interactive `pareto-explorer.html` with a live quality-floor slider, SVG Pareto chart, and filterable per-request decision audit table.
- **Tamper-Evident FinOps Receipts (`cost-router verify-receipt`):**
  - 8-place quantized `Decimal` USD accounting (`UsdDecimal`) with independent per-metric provenance tags (`measured`, `modeled`, `unknown`).
  - Canonical JSON serialization (`allow_nan=False`, sorted UTF-8 keys) and SHA-256 `SavingsReceipt` generation (`savings-receipt.json` + `savings-receipt.md`) with raw-dict-first `verify_receipt_file()` verification.
- **Hardened Async Provider Adapters:**
  - `OpenAICompatibleAdapter` (`/v1/chat/completions`) and `AnthropicAdapter` (`/v1/messages`) sharing hardened async HTTP transport (`Semaphore(8)`, `--max-cost-usd` spend circuit breaker, non-loopback `http://` auth block, `follow_redirects=False`).
- **Agent Budget Thermostat (`cost-router simulate-thermostat`):**
  - `AgentBudgetThermostat` enforcing per-`(org_id, agent_id)` hourly/daily spend envelopes with progressive tier step-down (`NORMAL -> THROTTLED_TO_MID -> THROTTLED_TO_COMPACT -> CIRCUIT_OPEN`) and sliding-window `prompt_sha256` retry-loop detection.
  - `SpendSummary` live economics reporting (`summarize_economics`) and multi-agent simulation CLI (`simulate-thermostat`).
- **Shadow/Active Gateway, OpenTelemetry & Prometheus Observability:**
  - `OpenAIRouterGateway` supporting `/v1/chat/completions`, non-disruptive `shadow_mode` counterfactual evaluation (`ShadowComparisonRecord`), and `/v1/finops/shadow-summary`.
  - OpenTelemetry `gen_ai.*` and `cost_router.*` span instrumentation (`OTelSpanRecord`, `InMemorySpanExporter`).
  - Zero-dependency Prometheus `0.0.4` / OpenMetrics text exporter (`render_prometheus_metrics`, `escape_prometheus_label_value`) exposed at `GET /metrics`.
- **Zero-Dependency HTTP Server & Container Packaging (`cost-router serve`):**
  - `ThreadingHTTPServer` (`create_router_http_server`) exposing `/healthz`, `/v1/chat/completions` (with 1 MiB `413` payload guard), `/v1/finops/shadow-summary`, `/v1/finops/economics`, and `/metrics`.
  - Non-root `Dockerfile` (`appuser`, port `8080`, `HEALTHCHECK` against `/healthz`).
- **Out-of-Sample Policy Auditing (`cost-router audit-holdout`):**
  - `LLMJudge` protocol, `DeterministicRubricJudge`, stratified `split_workload_holdout`, and `OutOfSamplePolicyAuditor` (`HoldoutGeneralizationReport`) to detect in-sample threshold overfitting.
