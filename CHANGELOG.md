# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
