# ADR-005: Multi-Provider Registry and Live FinOps Control-Plane Dashboard
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-004`, the router could serve OpenAI-compatible chat completions, FinOps summaries, and Prometheus metrics over a zero-dependency local HTTP server. Two operational gaps remained for heterogeneous multi-vendor deployments and live FinOps observability:

1. **Heterogeneous 3-Tier Provider Routing:** Prior to `MultiProviderAdapter`, replay and workload execution bound a single `ProviderAdapter` instance to all three tiers (`compact`, `mid`, `frontier`). Production teams routinely mix providers across tiers (for example, local Ollama or a fast compact model on `compact`, Anthropic on `mid`, and OpenAI on `frontier`) while still needing concurrent execution, shared client/concurrency-cap pooling when multiple tiers share a backend, and explicit `ProvenanceTag.UNKNOWN` accounting if a tier's adapter is unconfigured.
2. **Live FinOps Control-Plane UI:** `pareto-explorer.html` (`report.py`) provides offline 2D Pareto exploration, and `/v1/finops/economics`, `/v1/finops/shadow-summary`, and `/metrics` expose machine-readable telemetry. Operators running `cost-router serve` also need a zero-CDN, XSS-hardened browser control plane at `GET /dashboard` to inspect live thermostat unit economics, shadow-mode counterfactual deltas, and recent OpenTelemetry `gen_ai.*` spans without deploying Grafana or external frontend toolchains.

## Decision
1. **Heterogeneous Multi-Provider Registry (`src/cost_aware_model_router/registry.py`):**
   - Implement `MultiProviderAdapter(tier_adapters=None, provider_adapters=None, fallback_adapter=None)` implementing `execute_workload(workload, catalog)`.
   - Resolve the backend adapter for each `ModelTier` in `TIER_ORDER` using deterministic precedence:
     1. `tier_adapters[tier]` (explicit per-tier override)
     2. `provider_adapters[catalog.models[tier].provider]` (`TierModelSpec.provider` routing)
     3. `fallback_adapter` (optional default adapter)
   - Group tiers that resolve to the same adapter identity (`id(adapter)`) into a single `PricingCatalog` subset so shared adapters execute once with their own pooled HTTP clients and concurrency semaphores, then execute all adapter groups concurrently via `asyncio.gather`.
   - When a tier's `TierModelSpec` is missing from `PricingCatalog`, no adapter resolves (`None`), or a grouped adapter omits a `(request_id, tier)` trace, emit a typed `ExecutionTrace` with `ProvenanceTag.UNKNOWN` for `tokens_provenance`, `latency_provenance`, and `cost_provenance`, `Decimal("0")` cost, and `error_code="missing_provider_adapter"` so downstream complete-case intersection excludes incomplete tiers cleanly.
   - Re-export `MultiProviderAdapter` from `cost_aware_model_router.__init__`.

2. **Zero-CDN, XSS-Hardened Live FinOps Dashboard (`src/cost_aware_model_router/dashboard.py` + `src/cost_aware_model_router/server.py`):**
   - Implement `render_live_finops_dashboard_html(economics=None, shadow_summary=None, recent_spans=None, *, title=...) -> str` in `dashboard.py` and re-export it from `cost_aware_model_router.__init__`.
   - Embed the initial snapshot (`economics`, `shadow_summary`, `recent_spans`) inside `<script id="finops-snapshot" type="application/json">` using `escape_json_for_script_tag` from `report.py`, and escape `title` with `html.escape(title, quote=True)`.
   - Construct all dynamic DOM nodes via `document.createElement`, `clearNode`, and `td.textContent` (zero `innerHTML` and zero `eval`), with inline CSS and zero external CDN assets.
   - Wire `Refresh Live Metrics` to fetch `/v1/finops/economics` and `/v1/finops/shadow-summary` with `cache: "no-store"`, gracefully handling `400` when no `AgentBudgetThermostat` is attached.
   - Expose `GET /dashboard` in `create_router_http_server` (`server.py`), returning `200 OK` with `text/html; charset=utf-8`, `Cache-Control: no-store`, and the most recent 50 spans from `gateway.span_exporter.get_spans()[-50:]`.

## Alternatives Considered
- **Separate Adapter Wrapper Per Tier Without Identity Grouping (Rejected):** Calling `adapter.execute_workload(...)` independently for each tier when two tiers share an `OpenAICompatibleAdapter` or `AnthropicAdapter` would bypass shared semaphore limits and duplicate client setup. Grouping by `id(adapter)` preserves per-adapter pooling while supporting distinct adapters per tier.
- **Bundling a React/Vite Frontend or External Charting CDN for `/dashboard` (Rejected):** Adds Node build tooling, supply-chain surface, and external network dependencies that break air-gapped or single-container (`python:3.12-slim`) deployments.

## Consequences & Failure Modes
- **Positive:**
  - Teams can mix `DeterministicFixtureAdapter`, `OpenAICompatibleAdapter`, `AnthropicAdapter`, or custom `ProviderAdapter` implementations across `compact`, `mid`, and `frontier` tiers in a single concurrent replay.
  - `cost-router serve` provides an out-of-the-box live FinOps control plane at `GET /dashboard` with zero external assets and strict XSS escaping.
- **Failure Modes & Mitigations:**
  - **Unconfigured Tier or Provider:** If `TierModelSpec.provider` is not mapped in `provider_adapters` and no tier override or `fallback_adapter` is provided, `MultiProviderAdapter` emits `ProvenanceTag.UNKNOWN` traces with `error_code="missing_provider_adapter"` rather than silently substituting another model or crashing the batch.
  - **Live Refresh Scope on `/dashboard`:** Client-side refresh polls `/v1/finops/economics` and `/v1/finops/shadow-summary`; `recent_spans` reflects the server-rendered snapshot (latest 50 spans) on page load/reload.
