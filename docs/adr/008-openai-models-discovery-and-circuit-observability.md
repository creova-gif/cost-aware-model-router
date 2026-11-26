# ADR-008: OpenAI `/v1/models` Discovery, Circuit Observability, and `serve` Resilience/Bandit Wiring
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-006` (`ContextualBanditRouter`, `TenantAuthRegistry`) and `ADR-007` (`ProviderCircuitBreaker`, `FallbackChainAdapter`, `cost-router train-bandit`), two operational and interoperability gaps remained when running the HTTP gateway via `cost-router serve`:

1. **OpenAI SDK Model Discovery (`GET /v1/models`):** Standard OpenAI SDKs, LangChain/LlamaIndex integrations, and client health/discovery probes expect `GET /v1/models` returning an OpenAI-compatible model list. Without this route, drop-in OpenAI clients fail discovery checks before issuing `/v1/chat/completions` requests.
2. **Runtime Circuit Observability & Zero-Code CLI Wiring:** Operators running `FallbackChainAdapter` + `ContextualBanditRouter` via `cost-router serve` needed:
   - Live visibility into per-`ModelTier` circuit breaker state (`closed`, `open`, `half_open`, failure/success counters) at `GET /v1/finops/circuits`.
   - CLI flags (`--enable-fallback`, `--circuit-failure-threshold`, `--bandit-policy-file`) on `cost-router serve` to enable cross-tier fallback and load a calibrated `bandit-policy.json` snapshot without writing custom Python bootstrap code.

## Decision
1. **OpenAI-Compatible Model Discovery (`GET /v1/models` in `src/cost_aware_model_router/server.py`):**
   - Expose `GET /v1/models` gated by `TenantRole.ROUTER_CLIENT` (consistent with `POST /v1/chat/completions` RBAC in `ADR-006`).
   - Return an OpenAI-format list payload (`{"object": "list", "data": [...]}`) enumerating the virtual `"auto"` routing target alongside the configured tier models from the gateway's catalog.

2. **Circuit Breaker Snapshot & Observability (`ProviderCircuitBreaker.snapshot()` in `resilience.py`, `GET /v1/finops/circuits` in `server.py`):**
   - Add `ProviderCircuitBreaker.snapshot()` returning a thread-safe, JSON-serializable per-`ModelTier` view of `state` (`closed`, `open`, `half_open`), `consecutive_failures`, `consecutive_successes`, and breaker configuration thresholds.
   - Expose `GET /v1/finops/circuits` in `server.py` gated by `TenantRole.AUDITOR` (consistent with `/v1/finops/economics`, `/v1/finops/shadow-summary`, `/metrics`, and `/dashboard`), returning the circuit snapshot when a `FallbackChainAdapter` / `ProviderCircuitBreaker` is attached or an explicit disabled/empty status when fallback is not enabled.

3. **Resilience & Bandit Policy Wiring in `cost-router serve` (`src/cost_aware_model_router/cli.py`):**
   - `--enable-fallback`: Wrap the configured `ProviderAdapter` in `FallbackChainAdapter` with a `ProviderCircuitBreaker`.
   - `--circuit-failure-threshold`: Configure `CircuitBreakerConfig(failure_threshold=...)` on the `ProviderCircuitBreaker` used by `FallbackChainAdapter`.
   - `--bandit-policy-file`: Load a serialized `ContextualBanditRouter` snapshot via `ContextualBanditRouter.from_json(...)` at server startup and attach it to `OpenAIRouterGateway`.

## Alternatives Considered
- **Leaving `GET /v1/models` and `GET /v1/finops/circuits` Unauthenticated (Rejected):** Exposing model topology or live circuit-breaker failure state without RBAC would bypass `TenantAuthRegistry` governance established in `ADR-006`. Assigning `ROUTER_CLIENT` to `/v1/models` and `AUDITOR` to `/v1/finops/circuits` preserves least-privilege separation.
- **Reading `ProviderCircuitBreaker._entries` Directly from `server.py` (Rejected):** Direct access to private attributes bypasses `ProviderCircuitBreaker`'s `threading.Lock` and lazy `OPEN -> HALF_OPEN` timeout evaluation. A dedicated `snapshot()` method keeps locking and state transition logic encapsulated in `resilience.py`.
- **Requiring Custom Python Entrypoints for Fallback & Bandit Serving (Rejected):** Containerized deployments (`Dockerfile`) and local evaluations rely on `cost-router serve`; exposing `--enable-fallback`, `--circuit-failure-threshold`, and `--bandit-policy-file` makes resilience and learned routing immediately usable from the CLI.

## Consequences & Failure Modes
- **Positive:**
  - OpenAI SDKs and ecosystem tools can discover models via `GET /v1/models` with standard `ROUTER_CLIENT` credentials.
  - FinOps and SRE operators can monitor per-tier circuit health via `GET /v1/finops/circuits` with `AUDITOR` credentials.
  - `cost-router train-bandit` outputs (`bandit-policy.json`) can be promoted directly to `cost-router serve --bandit-policy-file ...` alongside `--enable-fallback`.
- **Failure Modes & Mitigations:**
  - **Missing or Invalid `--bandit-policy-file`:** `cost-router serve` validates and deserializes the JSON snapshot at startup before binding the HTTP socket, failing fast with a clear error rather than silently falling back to an uncalibrated policy.
  - **Fallback Disabled at Runtime:** When `--enable-fallback` is not passed, `GET /v1/finops/circuits` returns a well-formed JSON payload indicating fallback/circuit tracking is disabled rather than raising `500` or `AttributeError`.
  - **Stale `OPEN` State Reporting:** `ProviderCircuitBreaker.snapshot()` evaluates elapsed `recovery_timeout_seconds` under the lock so `HALF_OPEN` readiness is accurately reflected even before the next inference request arrives.
