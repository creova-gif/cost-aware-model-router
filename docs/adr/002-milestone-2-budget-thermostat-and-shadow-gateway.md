# ADR-002: Milestone 2 Agent Budget Thermostat, Shadow Gateway & OpenTelemetry Instrumentation
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Milestone 1 (`ADR-001`) established the offline hexagonal core: a 3-tier execution matrix (`compact`, `mid`, `frontier`), per-metric provenance (`measured`, `modeled`, `unknown`), declarative quality evaluators, exact 2D Pareto frontier optimization on `(total_cost_usd, pass_rate)`, and canonical SHA-256 `SavingsReceipt` verification.

To bridge offline Pareto replay with runtime production control, engineering teams need three runtime capabilities without sacrificing Milestone 1's zero-server reproducibility or strict `Decimal` accounting:
1. **Autonomous Spend & Loop Protection (`thermostat.py`):** Multi-step AI agents frequently enter retry or reasoning loops that silently burn frontier token budgets. Static hard caps abruptly break user workflows at 100% spend instead of gracefully stepping down model tiers (`frontier -> mid -> compact`) as budget utilization rises.
2. **OpenAI-Compatible Routing Gateway & Shadow Mode (`gateway.py`):** Teams adopting a model router need an SDK/gateway interface compatible with `/v1/chat/completions` that can run in **active routing mode** (enforcing the selected policy and budget thermostat) or **shadow mode** (serving the baseline tier to the caller while asynchronously evaluating candidate tiers and recording digest-only audit traces).
3. **Standardized FinOps & GenAI Observability (`telemetry.py`):** Production traces must join standard OpenTelemetry `gen_ai.*` semantic conventions with router-specific unit economics (`cost_router.*` attributes for tier selection, USD cost, provenance tag, thermostat state, and evaluator pass/fail verdicts).

## Decision
1. **Agent Budget Thermostat (`src/cost_aware_model_router/thermostat.py`):**
   - Implement `AgentBudgetThermostat` and immutable Pydantic v2 configuration/state models (`BudgetPolicy`, `ThermostatState`, `BudgetLedgerSnapshot`, `ThermostatDecision`).
   - Track real-time cumulative spend in 8-place quantized `Decimal` (`USD_QUANT = 0.00000001`) scoped by `(org_id, agent_id)`, partitioned into `measured_cost_usd`, `modeled_cost_usd`, and `unknown_burned_cost_usd`.
   - Enforce a 4-stage progressive state machine based on utilization ratio $u = \text{spent\_usd} / \text{budget\_limit\_usd}$:
     - `NORMAL` ($u < \text{soft\_threshold}$, default `0.70`): Policy-selected tier (`compact`, `mid`, or `frontier`) executes unmodified.
     - `THROTTLED_TO_MID` ($\text{soft\_threshold} \le u < \text{hard\_threshold}$, default `[0.70, 0.90)`): Requests targeting `frontier` are stepped down to `mid` unless the request carries an explicit `allow_frontier_override=True` with a high-complexity justification.
     - `THROTTLED_TO_COMPACT` ($\text{hard\_threshold} \le u < 1.00$, default `[0.90, 1.00)`): All routable requests are clamped to `compact` to preserve remaining budget runway.
     - `CIRCUIT_OPEN` ($u \ge 1.00$ or runaway loop tripped): Requests are rejected before provider egress with a deterministic `circuit_breaker_reason`.
   - **Runaway Loop Detection:** Maintain a bounded per-`(org_id, agent_id)` sliding window of recent `prompt_sha256` digests. If identical `prompt_sha256` digests repeat $\ge \text{max\_identical\_prompt\_streak}$ times (default `3`) within the window, immediately trip `CIRCUIT_OPEN` (`runaway_prompt_loop_detected`) to stop autonomous retry storms.

2. **Live Inference Economics Ledger:**
   - Maintain per-tenant (`org_id`) and per-agent (`agent_id`) unit economics in the thermostat and gateway ledger: total requests, passed outcomes (when inline declarative evaluators are attached), `total_cost_usd`, `cost_savings_vs_frontier_usd`, and live goodput (`cost_per_successful_outcome_usd`, emitting `None` when `passed_count == 0`).
   - Preserve strict per-metric provenance: live provider responses with explicit billed USD record `cost_provenance = ProvenanceTag.MEASURED`, catalog-computed token costs record `ProvenanceTag.MODELED`, and failed/unmetered calls record `ProvenanceTag.UNKNOWN` while still accruing known burned spend against the thermostat safety cap.

3. **OpenAI-Compatible Gateway & Shadow Mode Recorder (`src/cost_aware_model_router/gateway.py`):**
   - Implement `OpenAIRouterGateway` accepting OpenAI-shaped chat completion requests (`ChatCompletionRequest` / `WorkloadItem`) and returning typed `GatewayCompletionResponse` objects with OpenAI-compatible `choices`, `usage`, and a `router_metadata` envelope.
   - **Active Routing Mode (`shadow_mode=False`):** Evaluates the configured `PolicySpec` (`static_tier`, `complexity_threshold`, or `cascade_escalation`), consults `AgentBudgetThermostat.allocate()`, dispatches only the permitted tier(s), records spend, and returns the completion + routing explanation.
   - **Shadow Mode (`shadow_mode=True`):** Serves the baseline tier (`frontier` by default) to the caller while evaluating the candidate routing policy and alternative tiers (`compact`, `mid`) in shadow, emitting a redacted `ShadowComparisonRecord` (`prompt_sha256`, `response_sha256`, baseline vs. shadow tier, cost delta, latency delta, and counterfactual evaluator verdict) without altering caller-visible outputs.

4. **OpenTelemetry `gen_ai.*` & `cost_router.*` Instrumentation (`src/cost_aware_model_router/telemetry.py`):**
   - Provide a lightweight, zero-mandatory-collector span builder and exporter hook (`RouterSpanRecorder`, `build_otel_span_attributes()`) that emits standard OpenTelemetry GenAI semantic conventions alongside FinOps routing telemetry:
     - Standard attributes: `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.operation.name` (`"chat"`).
     - FinOps & router attributes: `cost_router.org_id`, `cost_router.agent_id`, `cost_router.policy_id`, `cost_router.request_id`, `cost_router.selected_tier`, `cost_router.attempted_tiers`, `cost_router.cost_usd`, `cost_router.baseline_cost_usd`, `cost_router.savings_usd`, `cost_router.tokens_provenance`, `cost_router.cost_provenance`, `cost_router.verdict_passed`, `cost_router.verdict_score`, `cost_router.thermostat_state`, and `cost_router.shadow_mode`.
   - Never attach raw prompt or completion strings to span attributes by default; attach only `cost_router.prompt_sha256` and `cost_router.response_sha256` to uphold PII/PHI zero-leakage guarantees.

## Alternatives Considered
- **External Redis / Postgres Requirement for Milestone 2 Thermostat (Rejected):** Requiring an external database breaks single-command local execution (`pytest` and SDK quickstarts). Instead, `thermostat.py` uses an in-memory concurrency-safe ledger with clean snapshot/export serialization so external state backends can wrap it in Milestone 3.
- **Hard-Stop-Only Budget Limiter (Rejected):** Abruptly failing requests at 100% budget without intermediate tier throttling (`frontier -> mid -> compact`) wastes the core value of a multi-tier router, which can extend budget runway by 5x–40x on routine tasks before opening the circuit breaker.
- **Full OpenTelemetry SDK Dependency Lock-In (Rejected):** Pinning heavy OTel gRPC exporter dependencies into core `pyproject.toml` bloats lightweight CLI installs. `telemetry.py` constructs standards-compliant attribute dictionaries and supports an optional `SpanSink` protocol (in-memory ring buffer + optional `opentelemetry-api` bridge).

## Consequences & Failure Modes
- **Positive:**
  - Autonomous agents gain predictable spend envelopes and automatic protection against infinite retry loops (`prompt_sha256` streak detection).
  - Engineering teams can validate routing policies in production via Shadow Mode (`shadow_mode=True`) before switching to Active Routing Mode.
  - Every routed completion emits deterministic `gen_ai.*` + `cost_router.*` telemetry attributes ready for Datadog, Grafana Tempo, Honeycomb, or Arize.
- **Failure Modes & Mitigations:**
  - **Downgraded tier fails strict schema evaluator under `THROTTLED_TO_COMPACT`:** When a request is forced onto `compact` due to budget exhaustion and fails `json_schema` under `cascade_escalation`, the thermostat blocks escalation above the permitted ceiling (`max_allowed_tier`) and records `thermostat_clamped=True` in the explanation rather than silently exceeding the hard budget cap.
  - **Shadow execution errors or timeouts:** Shadow evaluations trap adapter errors into `ProvenanceTag.UNKNOWN` so shadow failures never raise unhandled exceptions or fail the primary caller response.
