# ADR-007: Provider Circuit Breakers, Cross-Tier Fallback, Bounded TTL Cache, and Bandit Calibration CLI
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-006` (`ContextualBanditRouter` and `TenantAuthRegistry`), three operational gaps remained for production resilience and offline bandit calibration:

1. **Per-Tier Provider Outage Isolation & Provenance-Safe Fallback:** Live LLM backends (`OpenAICompatibleAdapter`, `AnthropicAdapter`, `MultiProviderAdapter`) can experience transient outages, rate limits, or degraded responses on a single `ModelTier` while sibling tiers remain healthy. Without a circuit breaker and fallback chain, repeated requests continue hitting an unhealthy tier, and naive fallback implementations risk masking which tier actually served the response—corrupting `ExecutionTrace` cost, latency, and provenance accounting.
2. **Bounded Semantic Cache Eviction:** Unbounded in-memory prompt caches grow without limit under long-running gateway processes, while naive dict caches lack both time-to-live (TTL) expiration and O(1) least-recently-used (LRU) eviction.
3. **Reproducible Bandit Calibration Workflow:** Operators using `ContextualBanditRouter` (`learner.py`) needed a zero-API-key CLI command to calibrate `(TaskCategory, ComplexityBucket) -> ModelTier` policies over the bundled 60-prompt workload and export deterministic JSON snapshots (`bandit-policy.json`) for inspection or warm-starting.

## Decision
1. **Thread-Safe Per-Tier Circuit Breaker (`ProviderCircuitBreaker` in `src/cost_aware_model_router/resilience.py`):**
   - Implement `CircuitState` (`closed`, `open`, `half_open`) and frozen `CircuitBreakerConfig` (`failure_threshold=3`, `recovery_timeout_seconds=30.0`, `success_threshold=2`).
   - Maintain a `threading.Lock`-guarded `_TierCircuitEntry` per `ModelTier` with an injectable `clock` (`time.monotonic` by default) for deterministic testing.
   - Enforce state transitions:
     - `CLOSED -> OPEN` when `consecutive_failures >= failure_threshold` (`opened_at = clock()`).
     - `OPEN -> HALF_OPEN` lazily once `clock() - opened_at >= recovery_timeout_seconds`.
     - `HALF_OPEN -> CLOSED` when `consecutive_successes >= success_threshold`, or `HALF_OPEN -> OPEN` immediately on any failure.

2. **Provenance-Safe Cross-Tier Fallback (`FallbackChainAdapter` in `src/cost_aware_model_router/resilience.py`):**
   - Wrap any `ProviderAdapter` with a `ProviderCircuitBreaker` and `fallback_order` (defaulting to `DEFAULT_FALLBACK_ORDER`: `compact -> (mid, frontier)`, `mid -> (frontier, compact)`, `frontier -> (mid, compact)`).
   - Before invoking `inner.execute_workload(...)`, filter `catalog.models` to tiers where `breaker.allow_request(tier)` is `True` so open circuits are not called.
   - For each `(item, tier)` in `TIER_ORDER`, walk `_candidate_tiers(tier)` (`[primary_tier, ...fallback_order[primary_tier]]`). A trace is healthy (`_is_trace_healthy`) only when it is non-`None`, `error_code is None`, and `ProvenanceTag.UNKNOWN` is absent from `tokens_provenance`, `latency_provenance`, and `cost_provenance`.
   - Record `record_success(candidate_tier)` on the first healthy candidate and return that `candidate_trace` verbatim (preserving its true `tier`, `model_id`, `cost_usd`, and provenance tags without silent provenance corruption). Record `record_failure(candidate_tier)` on unhealthy candidates.
   - If all candidate tiers are open or unhealthy, emit `_unavailable_trace(item, tier, catalog)` with `ProvenanceTag.UNKNOWN` across all three provenance fields and `error_code="all_fallback_tiers_unavailable"`.

3. **O(1) LRU + TTL Semantic Cache (`BoundedTTLCache` in `src/cost_aware_model_router/resilience.py`):**
   - Implement a `threading.Lock`-guarded `OrderedDict`-backed cache keyed by `(org_id, prompt_sha256)` with configurable `max_entries` (default `1024`, `>= 1`), `ttl_seconds` (default `3600.0`, `> 0.0`), and injectable `clock`.
   - `get(org_id, prompt_hash)` performs O(1) lookup, evicts the entry if `clock() >= expires_at`, and promotes hits via `OrderedDict.move_to_end(key)`.
   - `set(org_id, prompt_hash, response_text)` purges expired entries, updates existing keys in place (`move_to_end`), or evicts the least-recently-used entry via `popitem(last=False)` when at `max_entries`.

4. **Contextual Bandit Calibration CLI (`cost-router train-bandit` in `src/cost_aware_model_router/cli.py`, `src/cost_aware_model_router/learner.py`):**
   - Add the `train-bandit` subcommand accepting `--out` (default `./artifacts/bandit`), `--epochs` (default `4`, must be `> 0`), and `--quality-floor` (default `0.85`).
   - Execute `DeterministicFixtureAdapter` over the bundled 60-prompt workload (`generate_bundled_workload()`) and `load_bundled_catalog()`, then run `epochs` training passes calling `router.select_tier(item)`, `evaluate_trace(trace, item.evaluators)`, and `router.update(item, tier, passed=verdict.passed, cost_usd=trace.cost_usd)`.
   - Persist the calibrated state via `router.to_json()` to `<out>/bandit-policy.json` and print the final epoch pass rate alongside learned `(TaskCategory, ComplexityBucket) -> ModelTier` mappings from `router.export_learned_thresholds()`.

## Alternatives Considered
- **Rewriting Fallback `ExecutionTrace.tier` to Match the Requested Primary Tier (Rejected):** Mutating `candidate_trace.tier` or `model_id` to pretend the primary tier succeeded would silently corrupt cost/latency attribution and violate the strict provenance model established in `ADR-001`. Returning the authentic `candidate_trace` (or an explicit `ProvenanceTag.UNKNOWN` unavailable trace) keeps FinOps receipts auditable.
- **Third-Party Caching/Resilience Libraries (`cachetools`, `tenacity`, `pybreaker`) (Rejected):** Adds external runtime dependencies for functionality that is compactly and deterministically implemented with stdlib `OrderedDict`, `threading.Lock`, and `time.monotonic`.
- **Single Global Circuit Breaker Across All Tiers (Rejected):** A rate-limit or outage on `frontier` should not trip the circuit for `compact` or `mid`; per-`ModelTier` state machines allow graceful cross-tier degradation.

## Consequences & Failure Modes
- **Positive:**
  - `FallbackChainAdapter` isolates failing tiers automatically while preserving exact per-trace `tier`, `model_id`, `cost_usd`, and `ProvenanceTag` integrity.
  - `BoundedTTLCache` bounds memory usage at `max_entries` with O(1) LRU promotion/eviction and deterministic TTL expiry.
  - `cost-router train-bandit` provides a repeatable, zero-API-key calibration workflow producing inspectable `bandit-policy.json` artifacts.
- **Failure Modes & Mitigations:**
  - **All-Tier Outage:** When every candidate tier in `_candidate_tiers(tier)` is open or fails, `FallbackChainAdapter` emits `error_code="all_fallback_tiers_unavailable"` with `ProvenanceTag.UNKNOWN`, ensuring downstream `build_evaluation_matrix` excludes those requests from cross-policy savings percentages rather than counting $0 failures as successes.
  - **Half-Open Probe Contention:** Under concurrent callers when `recovery_timeout_seconds` elapses, multiple requests may observe `HALF_OPEN` before `success_threshold` or a failure transitions the state; this is bounded by `allow_request` and immediately re-opens on any failure.
  - **In-Sample Bandit Calibration:** `cost-router train-bandit` calibrates on the bundled 60-prompt fixture workload; operators should pair it with `cost-router audit-holdout` or live shadow evaluation before promoting learned policies to production traffic.
