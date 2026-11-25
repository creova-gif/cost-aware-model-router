# ADR-001: Milestone 1 Hexagonal Core, Per-Metric Provenance & 2D Pareto Savings Receipt
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Open-source LLM routing and FinOps tooling is split between opaque ML complexity classifiers (optimized on generic benchmarks without cost provenance) and gateway proxies (tracking raw token spend without quality evaluation or replay proof). Before introducing a live OpenAI-compatible HTTP proxy or autonomous Agent Budget Thermostat, Milestone 1 must prove the core optimization loop with a zero-API-key reproducible 3-tier replay, an interactive Pareto frontier explorer, and an integrity-verified savings receipt.

## Decision
1. **Flat 7-Module Package (`src/cost_aware_model_router/`):** Implement `schemas.py`, `adapters.py`, `evaluators.py`, `policies.py`, `receipt.py`, `report.py`, and `cli.py` plus bundled `fixtures/`. Carry `org_id`, `agent_id`, `policy_id`, and OpenTelemetry attribute hooks in `schemas.py` now so Milestone 2's live proxy mounts without rewriting core models.
2. **Per-Metric Provenance (`measured`, `modeled`, `unknown`):** Every `ExecutionTrace` tags `tokens_provenance`, `latency_provenance`, `verdict_provenance`, and `cost_provenance` independently. Live API calls tag tokens/latency/verdict `measured` and USD cost `measured` only when billed USD is returned by the provider (`modeled` when computed from `PricingCatalog * measured_tokens`). Fixture replays tag all metrics `modeled`. Missing evaluators or errors emit `unknown` and are excluded via a **complete-case intersection** across all 3 tiers.
3. **Deployable Policies vs. Hindsight Oracle (`ear-oracle`):** Separate deployable policies (`static-tier`, `complexity-threshold` with `in_sample_fit` disclosure, and `cascade-escalation` accumulating cost/latency across attempted tiers and re-evaluating `latency_sla` on cumulative latency) from the non-deployable `ear-oracle` upper bound (cheapest passing tier within $\tau$ of max score; on all-fail, highest-scoring tier tie-broken by lowest cost -> lowest latency -> `compact < mid < frontier` with `oracle_all_failed=true`).
4. **2D Pareto Frontier & 1D Slider Parity:** Compute the exact 2D Pareto frontier `(total_cost_usd, pass_rate)` over deployable policies so every frontier point is reachable by the 1D quality-floor slider (`pass_rate >= q_min`, min cost, tie-break `p95_latency_ms` -> `pass_rate` -> `policy_id`). Python pre-computes all policy results and frontier points; client-side JS in `pareto-explorer.html` performs only the 10-line floor filter.
5. **Canonical SHA-256 Receipt Integrity & Zero-Eval Security:** Serialize all USD values as 8-place `Decimal` strings and floats with fixed precision (`allow_nan=False`) in sorted-key UTF-8 compact JSON. `verify-receipt` hashes the parsed raw JSON dict (minus `integrity`) to detect payload drift or modification. Evaluators are strictly declarative (`json_schema`, ReDoS-guarded `regex_match`, `exact_match`, `json_field_predicate`, `latency_sla` — zero `eval`/`exec`). Raw prompt/response text is omitted by default (SHA-256 digests only).

## Alternatives Considered
- **7 Nested Subpackages (Rejected in D1):** Doubles file count with shallow `__init__.py` pass-through barrels for Milestone 1.
- **3D Pareto Frontier with 1D Slider (Rejected in D3):** Creates unreachable "ghost points" that are non-dominated only via P95 latency.
- **Local FastAPI Shadow Proxy + SQLite in Milestone 1 (Rejected in Office Hours):** Adds server/DB setup friction before proving the offline evaluation and Pareto optimization loop.

## Consequences & Failure Modes
- **Positive:** 100% deterministic zero-key demo (`cost-router demo`) in <5s; tamper-evident canonical receipts; zero XSS (`<script type="application/json">` with `\u003c` escape + `textContent`/`createElementNS`), zero `eval`, and non-loopback `http://` auth blocking.
- **Failure Modes & Mitigations:** Live provider 429/5xx/timeouts or missing `usage` fields degrade gracefully to `ProvenanceTag.UNKNOWN` and are excluded from savings claims via complete-case intersection; zero-pass policies emit `cost_per_successful_outcome_usd = null` (`∞`) without division by zero.
