# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
