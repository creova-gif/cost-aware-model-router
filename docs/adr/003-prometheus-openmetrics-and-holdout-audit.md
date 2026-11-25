# ADR-003: Zero-Dependency Prometheus/OpenMetrics Exporter and Out-of-Sample Holdout Audit
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Milestones 1 and 2 (`ADR-001`, `ADR-002`) established the offline 3-tier Pareto replay engine, SHA-256 `SavingsReceipt`, `AgentBudgetThermostat`, `OpenAIRouterGateway`, and OpenTelemetry (`gen_ai.*` / `cost_router.*`) span recording. Two operational and methodological gaps remained:

1. **Scrapeable FinOps & Gateway Metrics (`GET /metrics`):** Many production observability stacks (Prometheus, Grafana Agent, VictoriaMetrics) scrape HTTP `/metrics` endpoints in Prometheus text exposition format (`0.0.4`) rather than ingesting OTLP spans directly. Requiring `prometheus-client` as a mandatory runtime dependency would violate our lightweight, zero-bloat core philosophy, while unescaped label values (`org_id`, `tier`, `provenance`) could allow label injection or scrape parse failures.
2. **In-Sample Threshold Overfitting Detection:** Complexity-threshold policies (`threshold-aggressive`, `threshold-balanced`, `threshold-conservative`) tuned via grid search on a single workload (`support-and-code-v1.jsonl`, `In-Sample Fit = YES (grid)`) can overfit to in-sample prompt characteristics. Without an automated train/holdout split and out-of-sample audit, teams cannot quantify how much pass rate degrades on unseen prompts (e.g., on `support-and-code-v1.jsonl`, grid-selected thresholds achieved `95.3%` in-sample pass rate on the training split vs `82.4%` out-of-sample on the holdout split).

## Decision
1. **Zero-Dependency Prometheus/OpenMetrics `/metrics` Exporter (`src/cost_aware_model_router/metrics.py`, `src/cost_aware_model_router/gateway.py`):**
   - Implement `render_prometheus_metrics(spans: list[OTelSpanRecord], summary: SpendSummary | None = None) -> str` in `metrics.py` using only the Python standard library (`collections.defaultdict`, `decimal.Decimal`) and existing `format_usd_str` helpers.
   - Emit deterministic, sorted Prometheus `0.0.4` text format (`text/plain; version=0.0.4; charset=utf-8` on `GET /metrics` in `gateway.py`):
     - `cost_router_requests_total` (`counter`, labels: `org_id`, `tier`, `provenance`, `shadow_mode`)
     - `cost_router_spend_usd_total` (`counter`, labels: `org_id`, `tier`, `provenance`, 8-place `Decimal` USD string)
     - `cost_router_latency_ms` (`summary` with `_sum` and `_count`, labels: `org_id`, `tier`)
     - When a `SpendSummary` from `AgentBudgetThermostat` is provided: `cost_router_cache_hits_total` (`counter`) and `cost_router_goodput_usd` (`gauge`, omitted when `cost_per_successful_outcome_usd` is `None`).
   - Implement `escape_prometheus_label_value(value: str) -> str` to escape `\`, `"`, and `\n` (`\\`, `\"`, `\n`) on every label value in `_format_labels()`, preventing Prometheus label injection and malformed exposition lines.

2. **Out-of-Sample Holdout Policy Auditor & CLI Workflow (`src/cost_aware_model_router/judge.py`, `src/cost_aware_model_router/cli.py`):**
   - Implement `OutOfSamplePolicyAuditor` and `HoldoutGeneralizationReport` (frozen Pydantic v2 model with `extra="forbid"`) in `judge.py`:
     - Partition the workload into disjoint training and holdout splits using `split_workload_holdout()` (default `holdout_fraction=0.30`).
     - Grid-search `(compact_max_score, mid_max_score)` pairs across `DEFAULT_COMPACT_GRID` and `DEFAULT_MID_GRID` (`c_max <= m_max`) on the training split to minimize `total_cost_usd` subject to `quality_floor` (default `0.90`).
     - Freeze the selected thresholds (`in_sample_fit=False`) and evaluate them on the unseen holdout split alongside `all-compact` and `all-frontier` baselines.
     - Report `in_sample_pass_rate`, `out_of_sample_pass_rate`, `generalization_pass_delta`, `in_sample_savings_pct`, `out_of_sample_savings_pct`, and `holds_quality_floor_out_of_sample` (within `quality_tolerance=0.03`).
   - Expose `cost-router audit-holdout` in `cli.py` (`--out`, `--holdout-fraction`, `--quality-floor`), writing `holdout-generalization.json` and printing a concise generalization summary.
   - Observed baseline result on `support-and-code-v1.jsonl` (`holdout_fraction=0.30`, `quality_floor=0.90`): `95.3%` in-sample train pass rate vs `82.4%` out-of-sample holdout pass rate, explicitly surfacing threshold overfitting before production rollout.

## Alternatives Considered
- **Depending on `prometheus-client` (Rejected):** Adding an external metrics library introduces global default collector registries, process-wide state collisions in tests, and extra install footprint for a formatting task that takes ~110 lines of pure Python with deterministic output ordering.
- **Evaluating Thresholds Only via Full-Workload Cross-Validation Without a Frozen Holdout CLI (Rejected):** Reporting only in-sample grid scores or abstract CV averages makes it harder for operators to inspect exact train-vs-holdout pass-rate deltas. A dedicated `cost-router audit-holdout` command produces an auditable `holdout-generalization.json` artifact for PR review.

## Consequences & Failure Modes
- **Positive:**
  - Any Prometheus/OpenMetrics scraper can poll `GET /metrics` on the gateway with zero extra dependencies and guaranteed label-value escaping.
  - Teams can detect in-sample threshold overfitting (`95.3%` train vs `82.4%` holdout on `support-and-code-v1.jsonl`) prior to deploying complexity-threshold policies.
- **Failure Modes & Mitigations:**
  - **High-cardinality `org_id` label explosion in Prometheus:** Unbounded tenant IDs on `/metrics` can increase Prometheus series cardinality. Mitigate by bounding tenant/agent IDs at the caller boundary and aggregating spans per `org_id` + `tier` + `provenance`.
  - **Small holdout splits:** If `holdout_fraction` yields zero complete-case requests in either split, `OutOfSamplePolicyAuditor.audit()` fails fast with a `ValueError` rather than emitting misleading `0%` or `100%` generalization metrics.
