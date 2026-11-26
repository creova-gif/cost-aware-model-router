# ADR-006: Contextual Bandit Routing and Multi-Tenant API Governance
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-005`, the router supported heterogeneous multi-provider routing (`MultiProviderAdapter`), durable SQLite economics persistence (`SQLiteEconomicsStore`), and a live FinOps dashboard (`GET /dashboard`). Two production capabilities remained for adaptive routing and multi-tenant API control:

1. **Online Contextual Routing Beyond Static Thresholds:** Static complexity-threshold policies (`threshold-balanced`, `threshold-aggressive`, `threshold-conservative`) rely on offline grid-searched cutoffs that do not adapt when new workload distributions or model quality/cost profiles shift in production. Operators need a zero-dependency online learner that balances exploration across `(TaskCategory, ComplexityBucket)` contexts, respects explicit `min_quality_floor` guarantees, and optimizes goodput (`pass - cost_penalty_lambda * cost_usd`) with deterministic JSON checkpointing.
2. **Multi-Tenant Virtual Key Governance & Rate Limiting:** Exposing `/v1/chat/completions` and `/v1/finops/*` endpoints across multiple organizations (`org_id`) requires digest-only virtual API key management, constant-time token verification, role-based access control (RBAC), cross-tenant `org_id` anti-spoofing, and per-tenant burst/RPS rate limiting without external auth proxies or secret-leaking logs.

## Decision
1. **Quality-Aware UCB1 Contextual Bandit (`src/cost_aware_model_router/learner.py`):**
   - Implement `ComplexityBucket` (`low`, `medium`, `high` via `bucket_for_score` at `< 0.30` and `< 0.70` cutoffs), `ArmStats` (immutable Pydantic v2 statistics with 8-place `UsdDecimal` `total_cost_usd`, `pass_rate`, and `mean_cost_usd`), `ContextualBanditConfig` (`exploration_c`, `cost_penalty_lambda`, `min_quality_floor`, `seed`), and `ContextualBanditRouter`.
   - Derive discrete context keys `(TaskCategory, ComplexityBucket)` from `WorkloadItem.features.weighted_score()` and `WorkloadItem.category`.
   - In `select_tier(item)`, explore any unpulled tier in `TIER_ORDER` (`compact`, `mid`, `frontier`) first (`pulls == 0`), then score each arm with `mean_reward + exploration_c * sqrt(ln(total_pulls) / arm.pulls)` where `reward = (1.0 if passed else 0.0) - cost_penalty_lambda * float(quantize_usd(cost_usd))`.
   - Filter eligible arms to those meeting `pass_rate + 1e-9 >= min_quality_floor`, selecting the highest UCB1 score (breaking ties in favor of cheaper `TIER_ORDER`). When no arm meets `min_quality_floor`, fall back to the tier with the highest empirical `pass_rate`, then UCB1 score.
   - Provide deterministic `export_learned_thresholds()`, `to_json()`, and `from_json()` snapshot serialization.

2. **Multi-Tenant Auth Registry & Token-Bucket Rate Limiter (`src/cost_aware_model_router/governance.py`):**
   - Implement `TenantRole` (`admin`, `router_client`, `auditor`), `TenantKeyRecord` (storing only `key_prefix` and 64-char `key_sha256` digest—never raw secrets), `AuthDecision` (`authenticated`, `authorized`, `rate_limited`, `org_id`, `role`, `status_code` in `{200, 401, 403, 429}`, `reason`), `TokenBucketRateLimiter`, and `TenantAuthRegistry`.
   - Issue virtual keys (`crk_<key_id>.<secret_part>`) via `secrets.token_hex` / `secrets.token_urlsafe` and store only the SHA-256 hex digest.
   - In `authenticate_and_authorize(authorization_header, *, required_role, claimed_org_id=None)`:
     1. Parse Bearer token (`401` if missing or malformed).
     2. Hash presented token with SHA-256 and compare against all stored `TenantKeyRecord.key_sha256` digests using `hmac.compare_digest` (`401` if unmatched).
     3. Reject disabled keys (`403`).
     4. Reject cross-tenant `claimed_org_id != matched_record.org_id` spoofing attempts (`403`).
     5. Enforce RBAC capabilities (`admin` -> `{admin, router_client, auditor}`; `router_client` -> `{router_client}`; `auditor` -> `{auditor}`; `403` if insufficient).
     6. Enforce thread-safe per-`org_id` token-bucket rate limiting (`TokenBucketRateLimiter.allow(org_id, rps, burst)` returning `429` when exhausted).
   - Export `ArmStats`, `AuthDecision`, `ComplexityBucket`, `ContextualBanditConfig`, `ContextualBanditRouter`, `TenantAuthRegistry`, `TenantKeyRecord`, `TenantRole`, and `TokenBucketRateLimiter` from `cost_aware_model_router.__init__`.

## Alternatives Considered
- **External ML / Linear Bandit Libraries (`scikit-learn`, `vowpalwabbit`) (Rejected):** Introduces heavy native dependencies and non-deterministic serialization, violating the zero-dependency core design and reproducible JSON snapshot requirements.
- **Unconstrained Epsilon-Greedy or Pure Cost-Minimizing Bandit (Rejected):** Pure cost minimization without `min_quality_floor` gating degrades pass rates on complex `code_sql` and `reasoning` prompts; UCB1 with quality-floor filtering preserves SLA compliance while still preferring cheaper tiers.
- **Plaintext Key Lookup Dict (`dict[raw_key, record]`) or JWT-Only Auth (Rejected):** Storing raw keys in memory/snapshots risks secret exposure, and stateless JWTs cannot be immediately disabled via `disable_key(key_id)` without a revocation list. SHA-256 digest storage with `hmac.compare_digest` enables instant revocation and zero-plaintext persistence.

## Consequences & Failure Modes
- **Positive:**
  - `ContextualBanditRouter` adapts tier selection online per `(TaskCategory, ComplexityBucket)` context while bounding quality degradation via `min_quality_floor`.
  - `TenantAuthRegistry` and `TokenBucketRateLimiter` provide zero-dependency, thread-safe authentication, RBAC, tenant isolation, and burst-aware rate limiting.
- **Failure Modes & Mitigations:**
  - **Cold-Start Quality Dip:** Until all three tiers in a context have at least 1 pull, `select_tier` probes `compact` and `mid` once; callers requiring strict zero-probe SLAs can warm-start via `update(...)` or `from_json(...)`.
  - **All-Arm Floor Miss:** If no tier meets `min_quality_floor` in a difficult context, `select_tier` deterministically falls back to the highest empirical `pass_rate` tier rather than raising or returning an empty choice.
  - **Linear Digest Scan at Very High Key Counts:** `authenticate_and_authorize` scans `self._records_by_id.values()` with `hmac.compare_digest` for constant-time comparison across keys; for deployments with thousands of keys, indexing by `key_prefix` before `hmac.compare_digest` can be added without changing the public API.
