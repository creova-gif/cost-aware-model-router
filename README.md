# Cost-Aware Model Router & FinOps Control Plane

**Evidence-first 3-tier LLM routing, interactive 2D Pareto frontier optimization, autonomous Agent Budget Thermostat, shadow-mode gateway, and SHA-256 auditable FinOps savings receipts.**

[![CI](https://github.com/creova-gif/cost-aware-model-router/actions/workflows/ci.yml/badge.svg)](https://github.com/creova-gif/cost-aware-model-router/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-v0.4.0-blue.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![Pydantic v2](https://img.shields.io/badge/pydantic-v2-brightgreen.svg)](src/cost_aware_model_router/schemas.py)
[![Typed: mypy --strict](https://img.shields.io/badge/mypy-strict-checked.svg)](pyproject.toml)
[![Security: Zero-Eval & Digest-Only](https://img.shields.io/badge/security-STRIDE%20hardened-success.svg)](docs/THREAT_MODEL.md)

---

## Overview

Engineering teams running AI applications and autonomous agents face an **inference economics trust gap**:
- **ML complexity routers** optimize routing on generic public leaderboards with no cost provenance or task-specific quality guarantees.
- **LLM gateway proxies** track raw token spend with no deterministic quality evaluation or counterfactual replay proof.

As a result, teams either overpay by sending every prompt to a frontier model (`all-frontier`) or hesitate to enable automated routing because they cannot prove whether cost savings silently broke JSON schemas, SQL safety invariants, or multi-step reasoning on their own workload.

**Cost-Aware Model Router** closes this gap by combining an offline **3-Tier Replay & 2D Pareto Optimizer** with a runtime **Agent Budget Thermostat**, **OpenAI-Compatible Shadow/Active Gateway**, and **OpenTelemetry `gen_ai.*` FinOps Instrumentation**:

1. **Zero-API-Key Reproducibility:** Run `cost-router demo` in under 2 seconds with zero API keys, zero databases, and zero background servers to generate an interactive single-file HTML Pareto Explorer (`pareto-explorer.html`) and a tamper-evident savings receipt (`savings-receipt.json` + `savings-receipt.md`).
2. **Goodput (Cost per Successful Outcome), Not Blind Token Cheapness:** Policies are evaluated on `cost_per_successful_outcome_usd` under explicit quality floors ($q_{\min}$) and benchmarked against a clearly separated offline hindsight-oracle upper bound (`ear-oracle`, $\tau$-equivalence).
3. **Strict Per-Metric Provenance (`measured`, `modeled`, `unknown`):** Every execution trace tags `tokens_provenance`, `latency_provenance`, `verdict_provenance`, and `cost_provenance` independently. Any request with an `unknown` metric on any tier is excluded from cross-policy savings percentages via a **complete-case intersection** while its burned spend is tracked separately.

---

## The 6 Core Capabilities

| # | Capability | Key Modules | What It Delivers |
|---|---|---|---|
| **1** | **Intelligent Model Routing** | [policies.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/policies.py), [schemas.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/schemas.py) | Evaluates deployable policies (`static_tier`, `complexity_threshold` with transparent weighted feature scoring, and `cascade_escalation` with cumulative cost/latency accounting and `latency_sla` short-circuiting) alongside the non-deployable `ear_oracle` theoretical ceiling. |
| **2** | **Live Inference Economics** | [receipt.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/receipt.py), [schemas.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/schemas.py) | 8-place quantized `Decimal` USD accounting (`ROUND_HALF_UP`), per-metric provenance (`measured` vs `modeled` vs `unknown`), goodput (`cost_per_successful_outcome_usd`), complete-case intersection, and canonical SHA-256 tamper-evident `SavingsReceipt` generation & verification. |
| **3** | **Shadow Mode & Traffic Replay** | [gateway.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/gateway.py), [adapters.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/adapters.py) | Deterministic offline 3-tier replay (`DeterministicFixtureAdapter`), live async OpenAI/Ollama replay (`OpenAICompatibleAdapter` with `--max-cost-usd` circuit breaker), and runtime `OpenAIRouterGateway` shadow mode (`shadow_mode=True`) that serves baseline responses while recording counterfactual tier savings. |
| **4** | **Agent Budget Thermostat** | [thermostat.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/thermostat.py) | Per-tenant (`org_id`) and per-agent (`agent_id`) budget envelopes that progressively step down allowed model tiers (`NORMAL -> THROTTLED_TO_MID -> THROTTLED_TO_COMPACT -> CIRCUIT_OPEN`) as utilization crosses soft/hard thresholds, plus sliding-window `prompt_sha256` streak detection to halt runaway agent retry loops. |
| **5** | **Developer Experience & OpenTelemetry** | [report.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/report.py), [telemetry.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/telemetry.py), [cli.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/cli.py) | Self-contained, zero-CDN interactive `pareto-explorer.html` with a live quality-floor slider ($q_{\min}$), collision-free SVG Pareto chart, per-request explanation filter tabs, and OpenTelemetry `gen_ai.*` + `cost_router.*` span attribute instrumentation. |
| **6** | **Quality-Aware Optimization** | [evaluators.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/evaluators.py), [policies.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/policies.py) | Strictly declarative, zero-`eval` evaluators (`json_schema`, ReDoS-guarded `regex_match`, `exact_match`, `json_field_predicate`, `latency_sla`) paired with an exact 2D Pareto frontier solver where every frontier policy is reachable via the 1D quality floor slider. |

---

## Architecture & Module Map

```text
                       +-----------------------------------------+
                       |     Workload JSONL / SDK Chat Call      |
                       |   (org_id, agent_id, EvalRules, OTel)   |
                       +--------------------+--------------------+
                                            |
                  +-------------------------+-------------------------+
                  |                                                   |
                  v (Offline Batch Replay)                            v (Runtime Gateway)
+-----------------------------------+               +-----------------------------------+
|      3-Tier Replay Pipeline       |               |       OpenAIRouterGateway         |
|           (cli.py)                |               |          (gateway.py)             |
+-----------------+-----------------+               +-----------------+-----------------+
                  |                                                   |
                  |                                                   v
                  |                                 +-----------------------------------+
                  |                                 |      AgentBudgetThermostat        |
                  |                                 |         (thermostat.py)           |
                  |                                 | NORMAL -> MID -> COMPACT -> OPEN  |
                  |                                 +-----------------+-----------------+
                  |                                                   |
                  +-------------------------+-------------------------+
                                            |
                                            v
                  +---------------------------------------------------+
                  |             ProviderAdapter Protocol              |
                  |                   (adapters.py)                   |
                  |  - DeterministicFixtureAdapter (zero API keys)    |
                  |  - OpenAICompatibleAdapter (async httpx + guards) |
                  +-------------------------+-------------------------+
                                            |
                                            v
                  +---------------------------------------------------+
                  |      Declarative Quality Evaluators (Zero-Eval)   |
                  |                  (evaluators.py)                  |
                  |  json_schema | regex_match | exact_match |        |
                  |  json_field_predicate | latency_sla               |
                  +-------------------------+-------------------------+
                                            |
                                            v
                  +---------------------------------------------------+
                  |    Policy Suite & Exact 2D Pareto Optimizer       |
                  |                   (policies.py)                   |
                  |  - Deployable: static, threshold, cascade         |
                  |  - Benchmark: ear-oracle (tau-equivalence)        |
                  +------------+-------------------------+------------+
                               |                         |
                               v                         v
             +---------------------------------+ +---------------------------------+
             |   Auditable Savings Receipt     | |   Observability & Explorer UI   |
             |          (receipt.py)           | |   (report.py / telemetry.py)    |
             | - savings-receipt.json (SHA256) | | - pareto-explorer.html (0 CDN)  |
             | - savings-receipt.md            | | - OTel gen_ai.* + cost_router.* |
             +---------------------------------+ +---------------------------------+
```

### Repository Structure (`src/cost_aware_model_router/`)

| Module | Responsibility |
|---|---|
| [schemas.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/schemas.py) | Pydantic v2 domain models (`WorkloadItem`, `ExecutionTrace`, `EvalVerdict`, `MetricProvenance`, `PolicySpec`, `PolicyResult`, `SavingsReceipt`) with 8-place `UsdDecimal` serialization and `extra="forbid"`. |
| [adapters.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/adapters.py) | `ProviderAdapter` protocol, bundled 60-prompt workload generator, `DeterministicFixtureAdapter`, and hardened async `OpenAICompatibleAdapter` + `AnthropicAdapter` (`Semaphore(8)`, `--max-cost-usd` breaker, non-loopback `http://` auth block, `follow_redirects=False`). |
| [registry.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/registry.py) | `MultiProviderAdapter` for concurrent heterogeneous 3-tier routing (`tier_adapters` overrides, `TierModelSpec.provider` mapping, `fallback_adapter`, and `ProvenanceTag.UNKNOWN` fallback for unconfigured tiers). |
| [resilience.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/resilience.py) | `ProviderCircuitBreaker` (`CLOSED -> OPEN -> HALF_OPEN -> CLOSED`, `reset()`), `FallbackChainAdapter` for provenance-safe cross-tier fallback, and `BoundedTTLCache` for O(1) LRU + TTL semantic caching. |
| [evaluators.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/evaluators.py) | Declarative evaluator engine (`json_schema`, ReDoS-guarded `regex_match`, `exact_match`, `json_field_predicate`, `latency_sla`) separating `content_passed` from `latency_passed`. |
| [policies.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/policies.py) | Complete-case `UNKNOWN` intersection, deployable policies (`static_tier`, `complexity_threshold`, `cascade_escalation`), `ear_oracle` hindsight benchmark, and exact 2D Pareto frontier solver (`compute_2d_pareto_frontier`, `select_policy_for_floor`). |
| [receipt.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/receipt.py) | Canonical JSON serializer (`allow_nan=False`, sorted UTF-8 keys, fixed-precision strings), SHA-256 `SavingsReceipt` builder, Markdown renderer, and raw-dict-first `verify_receipt_file()`. |
| [report.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/report.py) | Self-contained, XSS-hardened HTML generator (`pareto-explorer.html`) with interactive quality floor slider, `document.createElementNS` SVG chart, and filterable decision audit table. |
| [dashboard.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/dashboard.py) | Zero-CDN, XSS-hardened HTML generator (`render_live_finops_dashboard_html`) for the live FinOps control-plane `/dashboard` (unit economics KPIs, spend breakdowns, shadow-mode comparisons, circuit-breaker health panel, and recent OTel `gen_ai.*` spans). |
| [thermostat.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/thermostat.py) | Autonomous `AgentBudgetThermostat` with per-`(org_id, agent_id)` spend ledgers, progressive tier throttling (`NORMAL -> THROTTLED_TO_MID -> THROTTLED_TO_COMPACT -> CIRCUIT_OPEN`), and repeated-`prompt_sha256` loop breaker. |
| [learner.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/learner.py) | Quality-aware UCB1 `ContextualBanditRouter` (`ComplexityBucket`, `ArmStats`, `ContextualBanditConfig`) optimizing goodput across `(TaskCategory, ComplexityBucket)` contexts with `min_quality_floor` gating and deterministic JSON snapshots. |
| [governance.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/governance.py) | Multi-tenant `TenantAuthRegistry` and thread-safe `TokenBucketRateLimiter` enforcing SHA-256 virtual API keys (`hmac.compare_digest`), RBAC (`admin`/`router_client`/`auditor`), cross-tenant `org_id` anti-spoofing, and per-tenant rate limits. |
| [gateway.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/gateway.py) | `OpenAIRouterGateway` supporting `/v1/chat/completions` (`ChatCompletionRequest` / `ChatCompletionResponse`), non-disruptive shadow-mode counterfactual evaluation (`ShadowComparisonRecord`), and `/v1/finops/shadow-summary`. |
| [telemetry.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/telemetry.py) | OpenTelemetry `gen_ai.*` and `cost_router.*` span instrumentation (`OTelSpanRecord`, `InMemorySpanExporter`), declarative policy loader (`load_policies_from_json`), and stratified holdout splitting (`split_workload_holdout`). |
| [metrics.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/metrics.py) | Zero-dependency Prometheus `0.0.4` / OpenMetrics text exporter (`render_prometheus_metrics`) with label-value escaping (`escape_prometheus_label_value`) for `GET /metrics`. |
| [judge.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/judge.py) | `LLMJudge` protocol, `DeterministicRubricJudge`, and `OutOfSamplePolicyAuditor` for train/holdout policy generalization audits (`HoldoutGeneralizationReport`). |
| [server.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/server.py) | Zero-dependency `_RouterHTTPServer` (`ThreadingHTTPServer` + shared `_AsyncLoopRunner` background event loop via `create_router_http_server`) exposing `GET`/`HEAD` `/healthz`, `GET`/`HEAD` `/readyz`, `/v1/models` (`ROUTER_CLIENT`), `/v1/chat/completions` (JSON + `stream=True` SSE `chat.completion.chunk` with 1 MiB `413` payload guard), `/v1/finops/shadow-summary`, `/v1/finops/economics`, `/v1/finops/circuits` (`AUDITOR`), `POST /v1/finops/circuits/reset` (`ADMIN`), `/metrics`, and `/dashboard`, with `X-Request-Id` correlation and security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Cache-Control`). |
| [store.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/store.py) | Thread-safe WAL-mode `SQLiteEconomicsStore` for durable persistence of `SpendLedgerEntry`, `ShadowComparisonRecord`, and `OTelSpanRecord` with 8-place USD string precision, `SpendSummary` aggregation, `is_healthy()` readiness probe, and `prune_older_than()` telemetry retention pruning. |
| [cli.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/cli.py) | Command-line interface exposing `cost-router demo`, `cost-router replay`, `cost-router verify-receipt`, `cost-router simulate-thermostat`, `cost-router audit-holdout`, `cost-router train-bandit`, `cost-router issue-key`, `cost-router list-keys`, `cost-router rotate-key`, `cost-router revoke-key`, `cost-router prune-db`, and `cost-router serve`. |

---

## Quickstart

### 1. Installation

Requires **Python 3.12+**:

```bash
pip install -e ".[dev]"
```

### 2. Run the Zero-API-Key 60-Prompt Pareto Demo

```bash
cost-router demo --out ./artifacts/demo --quality-floor 0.92
```

Expected output:

```text
Workload: support-and-code-v1.jsonl (60 complete, 0 excluded unknown)
Baseline (all-frontier): $0.58276000 | pass=96.7%
Recommended (threshold-aggressive): $0.22235100 (+61.9% savings) | pass=96.7%
2D Pareto Frontier Policies: all-compact, all-mid, threshold-aggressive
Integrity SHA-256: 0824aa5c1aa4346efa668ba393dbc032dc8ec311fd62245fa98c87c637eeebd7
Artifacts written to: artifacts/demo
```

Open `./artifacts/demo/pareto-explorer.html` in any browser to drag the **Quality Floor ($q_{\min}$)** slider and inspect per-request routing explanations.

### 3. Verify Canonical Receipt Integrity

```bash
cost-router verify-receipt ./artifacts/demo/savings-receipt.json
# VERIFIED: OK (SHA-256: 0824aa5c1aa4346efa668ba393dbc032dc8ec311fd62245fa98c87c637eeebd7)
```

If any cost, pass rate, verdict, or provenance tag in `savings-receipt.json` is modified, `verify-receipt` exits non-zero with an explicit SHA-256 digest mismatch or invariant violation error.

### 4. Replay a Custom Workload (Offline or Live OpenAI / Ollama)

```bash
# Offline deterministic replay against custom JSONL workload
cost-router replay \
  --workload ./my-workload.jsonl \
  --out ./artifacts/custom-replay \
  --quality-floor 0.90

# Live replay against an OpenAI-compatible or local Ollama endpoint with a $5.00 spend breaker
export OPENAI_API_KEY="sk-..."
cost-router replay \
  --workload ./my-workload.jsonl \
  --out ./artifacts/live-replay \
  --live \
  --base-url https://api.openai.com/v1 \
  --api-key-env OPENAI_API_KEY \
  --concurrency 8 \
  --max-cost-usd 5.00000000
```

### 5. Python SDK: `OpenAIRouterGateway`, Shadow Mode & `AgentBudgetThermostat`

```python
import asyncio
from decimal import Decimal

from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    OpenAIRouterGateway,
)
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.telemetry import InMemorySpanExporter
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy


async def main() -> None:
    policies = {p.policy_id: p for p in default_policy_suite()}
    exporter = InMemorySpanExporter()

    # 1. Initialize OpenAI-compatible gateway with primary baseline + shadow candidate policy
    gateway = OpenAIRouterGateway(
        primary_policy=policies["all-frontier"],
        shadow_policy=policies["threshold-aggressive"],
        span_exporter=exporter,
    )

    resp = await gateway.handle_chat_completion(
        ChatCompletionRequest(
            model="auto",
            messages=[ChatMessage(role="user", content="Classify ticket #18 into BILLING or ACCOUNT.")],
            x_org_id="org_demo",
            x_agent_id="triage_router",
        )
    )
    print("Served tier:", resp.router_metadata.selected_tier.value, "| Cost USD:", resp.usage.cost_usd)
    print("Shadow summary:", gateway.build_shadow_summary())
    print("OTel span attributes:", exporter.get_spans()[0].attributes)

    # 2. Enforce autonomous per-(org_id, agent_id) spend envelopes with AgentBudgetThermostat
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_demo",
                agent_id="triage_router",
                hourly_budget_usd=Decimal("0.05000000"),
            )
        ]
    )
    print("Thermostat summary:", thermostat.summarize_economics(org_id="org_demo"))


asyncio.run(main())
```

You can also compose heterogeneous providers per tier with `MultiProviderAdapter` (`tier_adapters` overrides `provider_adapters[spec.provider]`, falling back to `fallback_adapter` or emitting `ProvenanceTag.UNKNOWN` with `error_code="missing_provider_adapter"`):

```python
from cost_aware_model_router import (
    AnthropicAdapter,
    DeterministicFixtureAdapter,
    ModelTier,
    MultiProviderAdapter,
    OpenAICompatibleAdapter,
)

adapter = MultiProviderAdapter(
    provider_adapters={
        "openai": OpenAICompatibleAdapter(base_url="https://api.openai.com/v1", api_key="..."),
        "anthropic": AnthropicAdapter(api_key="..."),
    },
    tier_adapters={ModelTier.COMPACT: DeterministicFixtureAdapter()},
)
```

### 6. Simulate Agent Budget Thermostat & Audit Out-of-Sample Generalization

```bash
# Multi-agent gateway + thermostat + OTel simulation
cost-router simulate-thermostat --out ./artifacts/thermostat-sim
# Writes thermostat-economics.json, shadow-summary.json, and otel-spans.jsonl

# Train/holdout split audit to detect in-sample threshold overfitting
cost-router audit-holdout --out ./artifacts/holdout-audit --holdout-fraction 0.30 --quality-floor 0.90
# Writes holdout-generalization.json (e.g., 95.3% train vs 82.4% holdout on support-and-code-v1.jsonl)
```

### 7. Scrape Prometheus / OpenMetrics Endpoint (`GET /metrics`)

The gateway ASGI transport (`create_gateway_asgi_transport` in [gateway.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/gateway.py)) and local HTTP server (`create_router_http_server` in [server.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/server.py)) expose `GET /metrics` (`text/plain; version=0.0.4; charset=utf-8`), powered by [metrics.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/metrics.py):

- `cost_router_requests_total{org_id,tier,provenance,shadow_mode}` (counter)
- `cost_router_spend_usd_total{org_id,tier,provenance}` (counter, 8-place USD `Decimal`)
- `cost_router_latency_ms_sum{org_id,tier}` / `cost_router_latency_ms_count{org_id,tier}` (summary)
- `cost_router_cache_hits_total` (counter) and `cost_router_goodput_usd` (gauge) when a thermostat is attached
- `cost_router_circuit_state` (gauge) for per-tier circuit breaker state when circuit breakers are configured

### 8. Serve the Local OpenAI-Compatible HTTP Gateway (`cost-router serve`)

Start the zero-dependency `ThreadingHTTPServer` locally or in a non-root container (`Dockerfile`). Optionally provision a hashed `TenantAuthRegistry` JSON config via `cost-router issue-key` (`issue-key`, `rotate-key`, and `revoke-key` all persist `auth-config.json` atomically with `0o600` permissions via `_write_auth_config_atomic`) and load it with `--auth-config`, pass multi-tenant thermostat envelopes with `--budget-config`, and enable privacy-safe structured JSON access logs (`request_id`, `method`, `path`, `status_code`, `duration_ms`) with `--access-log`:

```bash
# Issue a virtual API key (prints raw token once; stores only SHA-256 digest in auth-config.json)
cost-router issue-key \
  --org-id org_demo \
  --role router_client \
  --rate-limit-rps 20.0 \
  --burst-capacity 40 \
  --out ./artifacts/auth-config.json

# List registered tenant virtual API keys (non-secret metadata only)
cost-router list-keys --auth-config ./artifacts/auth-config.json

# Atomically rotate a tenant virtual API key (issues replacement key and disables old key by default)
cost-router rotate-key \
  --key-id <key_id> \
  --auth-config ./artifacts/auth-config.json

# Revoke (disable) a tenant virtual API key in auth-config.json
cost-router revoke-key \
  --key-id <key_id> \
  --auth-config ./artifacts/auth-config.json

# Start local HTTP router with shadow policy, fallback circuit breaker, bandit policy, auth config, multi-tenant budget config, structured access log, and $1.00/hr thermostat fallback budget
cost-router serve \
  --host 127.0.0.1 \
  --port 8080 \
  --policy threshold-balanced \
  --shadow-policy threshold-aggressive \
  --hourly-budget-usd 1.00000000 \
  --budget-config ./artifacts/budget-config.json \
  --enable-fallback \
  --circuit-failure-threshold 3 \
  --bandit-policy-file ./artifacts/bandit/bandit-policy.json \
  --auth-config ./artifacts/auth-config.json \
  --access-log

# Or serve against live OpenAI / Anthropic providers (shares _build_live_adapter() with replay; handles SIGTERM gracefully)
cost-router serve \
  --live \
  --provider openai \
  --api-key-env OPENAI_API_KEY \
  --max-cost-usd 5.00000000
```

All HTTP responses include `X-Request-Id` correlation (propagating a valid incoming `X-Request-Id` or generating a UUID4 hex ID) and baseline security headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`). Rate-limited `429` responses include the standard `Retry-After: 1` header.

Query the running server with `curl`:

```bash
# Liveness / container HEALTHCHECK probe (supports GET and HEAD)
curl -s http://127.0.0.1:8080/healthz
curl -s -I http://127.0.0.1:8080/healthz

# Readiness probe (supports GET and HEAD; returns 200 {"status":"ready"}, or 503 when SQLiteEconomicsStore.is_healthy() is false or all tier circuits are open)
curl -s -i http://127.0.0.1:8080/readyz
curl -s -I http://127.0.0.1:8080/readyz

# OpenAI-compatible model discovery (ROUTER_CLIENT role)
curl -s http://127.0.0.1:8080/v1/models

# OpenAI-compatible chat completion (1 MiB max body size; returns 413 if exceeded)
curl -s -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-org-id: org_demo" \
  -H "x-agent-id: triage_router" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Classify ticket #18 into BILLING or ACCOUNT."}]}'

# OpenAI-compatible streaming chat completion (returns text/event-stream chat.completion.chunk + data: [DONE])
curl -sN -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-org-id: org_demo" \
  -H "x-agent-id: triage_router" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"Classify ticket #18 into BILLING or ACCOUNT."}]}'

# FinOps thermostat economics summary (optional ?org_id=... filter)
curl -s "http://127.0.0.1:8080/v1/finops/economics?org_id=org_demo"

# Per-tier circuit breaker health snapshot (AUDITOR role)
curl -s http://127.0.0.1:8080/v1/finops/circuits

# Reset one or all tier circuit breakers to CLOSED (ADMIN role)
curl -s -X POST http://127.0.0.1:8080/v1/finops/circuits/reset \
  -H "Content-Type: application/json" \
  -d '{"tier":"frontier"}'

# Prometheus / OpenMetrics scrape endpoint
curl -s http://127.0.0.1:8080/metrics

# Live FinOps control-plane dashboard (zero-CDN HTML with Circuit Breaker Health panel; open in browser)
curl -s http://127.0.0.1:8080/dashboard
```

Or build and run the non-root Docker image (`appuser`, port `8080`):

```bash
docker build -t cost-aware-model-router .
docker run --rm -p 8080:8080 cost-aware-model-router
```

Or start the production deployment with Docker Compose ([docker-compose.yml](docker-compose.yml)), which persists SQLite state in the `cost_router_data` volume (`/data/economics.db`), enables fallback routing (`--enable-fallback`), and probes `http://127.0.0.1:8080/readyz`:

```bash
docker compose up -d
```

### 9. Calibrate Contextual Bandit Policy (`cost-router train-bandit`)

Train the quality-aware UCB1 `ContextualBanditRouter` ([learner.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/learner.py)) over the bundled 60-prompt workload and export learned `(TaskCategory, ComplexityBucket) -> ModelTier` mappings:

```bash
cost-router train-bandit --out ./artifacts/bandit --epochs 4 --quality-floor 0.85
# Writes ./artifacts/bandit/bandit-policy.json and prints final epoch pass rate + learned context mappings
```

### CLI Command Reference

```bash
# 0. Print installed CLI version
cost-router --version

# 1. Zero-API-key 60-prompt Pareto demo + SHA-256 receipt
cost-router demo --out ./artifacts/demo --quality-floor 0.92

# 2. Calibrate UCB1 contextual bandit and export bandit-policy.json
cost-router train-bandit --out ./artifacts/bandit --epochs 4 --quality-floor 0.85

# 3. Issue, list, rotate, or revoke hashed tenant API keys (ROUTER_CLIENT, AUDITOR, or ADMIN)
cost-router issue-key --org-id org_demo --role router_client --out ./artifacts/auth-config.json
cost-router list-keys --auth-config ./artifacts/auth-config.json
cost-router rotate-key --key-id <key_id> --auth-config ./artifacts/auth-config.json
cost-router revoke-key --key-id <key_id> --auth-config ./artifacts/auth-config.json

# 4. Serve OpenAI-compatible gateway (offline fixture or --live --provider openai|anthropic) with fallback circuit breakers, bandit policy, tenant auth/budget configs, and structured access logging
cost-router serve \
  --enable-fallback \
  --circuit-failure-threshold 3 \
  --bandit-policy-file ./artifacts/bandit/bandit-policy.json \
  --auth-config ./artifacts/auth-config.json \
  --budget-config ./artifacts/budget-config.json \
  --access-log
cost-router serve --live --provider anthropic --api-key-env ANTHROPIC_API_KEY --max-cost-usd 5.00000000

# 5. Prune expired shadow_comparisons and otel_spans from SQLite while preserving spend_ledger totals
cost-router prune-db --db ./artifacts/router-economics.db --max-age-seconds 604800
```

---

## Verified Milestone 1 Benchmark (`support-and-code-v1.jsonl`)

> **Provenance & Methodology Disclosure:** The table below is reproduced directly from the verified artifact [savings-receipt.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/artifacts/demo/savings-receipt.md) (`Workload SHA-256: 2b1f108010e9...`, `Receipt Integrity SHA-256: 0824aa5c1aa4...`). All `60 / 60` requests across 5 task categories (`json_extraction`, `classification`, `summarization`, `code_sql`, `reasoning`) use the deterministic `DeterministicFixtureAdapter` (`ProvenanceTag.MODELED`, `0` `measured`, `0` `unknown` excluded) priced against `catalog.json` (`2026-11-m1`: Compact `$0.15/$0.60`, Mid `$1.20/$4.80`, Frontier `$10.00/$30.00` per 1M input/output tokens). Policies whose thresholds were selected on this workload carry `In-Sample Fit = YES (grid)`.

| Policy | Type | Pareto 2D | In-Sample Fit | Pass Rate | Total Cost (USD) | Cost / Success (Goodput) | Savings vs Baseline | P95 Latency | Tier Mix (C / M / F) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `all-compact` | Deployable | YES | no | 61.7% (37/60) | \$0.00996090 | \$0.00026921 | +98.3% | 594 ms | 60 / 0 / 0 |
| `threshold-aggressive` | Deployable | YES | YES (grid) | 96.7% (58/60) | \$0.22235100 | \$0.00383364 | +61.9% | 3312 ms | 37 / 14 / 9 |
| `threshold-balanced` | Deployable | no | YES (grid) | 96.7% (58/60) | \$0.22235100 | \$0.00383364 | +61.9% | 3312 ms | 37 / 14 / 9 |
| `cascade-escalation` | Deployable | no | no | 88.3% (53/60) | \$0.25455930 | \$0.00480301 | +56.3% | 5136 ms | 37 / 14 / 9 |
| `threshold-conservative` | Deployable | no | YES (grid) | 96.7% (58/60) | \$0.40303040 | \$0.00694880 | +30.8% | 3312 ms | 31 / 6 / 23 |
| `all-mid` | Deployable | YES | no | 85.0% (51/60) | \$0.07968720 | \$0.00156249 | +86.3% | 1252 ms | 0 / 60 / 0 |
| `all-frontier` | Deployable | no | no | 96.7% (58/60) | \$0.58276000 | \$0.01004759 | +0.0% | 3312 ms | 0 / 0 / 60 |
| `ear-oracle-tau-0.02` | Oracle (Non-Deployable) | no | no | 96.7% (58/60) | \$0.22235100 | \$0.00383364 | +61.9% | 3312 ms | 37 / 14 / 9 |

### Key Takeaways from the Benchmark Matrix
- **Exact Quality Parity at `-61.85%` Cost (`threshold-aggressive`):** Routing the 30 low-complexity extraction/classification prompts and 7 straightforward incident summaries to `compact`, 3 harder summaries and 11 PostgreSQL lock-safety patches to `mid`, and only the 9 formal consensus proofs to `frontier` matches `all-frontier`'s `96.7%` (58/60) pass rate while cutting total cost from `\$0.58276000` to `\$0.22235100` (goodput improves from `\$0.01004759` to `\$0.00383364` per passing outcome).
- **Why `cascade-escalation` Trails on Tight Latency SLAs (`88.3%` pass rate):** Although `cascade-escalation` requires zero in-sample threshold tuning, sequential `compact -> mid -> frontier` retries accumulate both token spend (`\$0.25455930`) and wall-clock latency (`P95 = 5136 ms`), causing 5 frontier-dependent reasoning prompts (`req-052..req-056`) to breach their `5000 ms` `latency_sla` and trigger cascade short-circuiting.
- **All-Fail Ceiling (`req-059`, `req-060`):** Two adversarial distributed-consensus prompts fail even on `frontier`, establishing the `96.7%` ceiling and exercising the locked D2 `ear-oracle` fallback (`oracle_all_failed = True`).

---

## Development, Static Analysis & Testing

All code adheres to [AGENTS.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/AGENTS.md) (`Python 3.12+`, `mypy --strict`, `ruff check`, `Pydantic v2`, zero bare `except Exception: pass`):

```bash
ruff check src tests
mypy --strict src tests
pytest -q
```

---

## Architecture, Design & Security Documentation

- **System Design & Engineering Review Record:** [docs/designs/cost-aware-model-router-m1.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/designs/cost-aware-model-router-m1.md)
- **ADR-000 (Architecture Decision Record Template):** [docs/adr/000-template.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/000-template.md)
- **ADR-001 (Milestone 1 — Hexagonal Core, Per-Metric Provenance & 2D Pareto Savings Receipt):** [docs/adr/001-milestone-1-pareto-replay-and-provenance.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/001-milestone-1-pareto-replay-and-provenance.md)
- **ADR-002 (Milestone 2 — Agent Budget Thermostat, Shadow Gateway & OpenTelemetry Instrumentation):** [docs/adr/002-milestone-2-budget-thermostat-and-shadow-gateway.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/002-milestone-2-budget-thermostat-and-shadow-gateway.md)
- **ADR-003 (Zero-Dependency Prometheus/OpenMetrics Exporter & Out-of-Sample Holdout Audit):** [docs/adr/003-prometheus-openmetrics-and-holdout-audit.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/003-prometheus-openmetrics-and-holdout-audit.md)
- **ADR-004 (Zero-Dependency Local HTTP Server & Non-Root Container Packaging):** [docs/adr/004-local-http-server-and-container-packaging.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/004-local-http-server-and-container-packaging.md)
- **ADR-005 (Multi-Provider Registry & Live FinOps Control-Plane Dashboard):** [docs/adr/005-multi-provider-registry-and-live-dashboard.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/005-multi-provider-registry-and-live-dashboard.md)
- **ADR-006 (Contextual Bandit Routing & Multi-Tenant API Governance):** [docs/adr/006-contextual-bandit-and-tenant-governance.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/006-contextual-bandit-and-tenant-governance.md)
- **ADR-007 (Provider Circuit Breakers, Cross-Tier Fallback, Bounded TTL Cache & Bandit Calibration CLI):** [docs/adr/007-provider-circuit-breakers-fallback-and-ttl-cache.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/007-provider-circuit-breakers-fallback-and-ttl-cache.md)
- **ADR-008 (OpenAI `/v1/models` Discovery, Circuit Observability & `serve` Resilience/Bandit Wiring):** [docs/adr/008-openai-models-discovery-and-circuit-observability.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/008-openai-models-discovery-and-circuit-observability.md)
- **ADR-009 (Enterprise Auth Config, Readiness Probes, Correlation Headers & Dashboard Circuit Health):** [docs/adr/009-enterprise-auth-config-readiness-and-correlation.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/009-enterprise-auth-config-readiness-and-correlation.md)
- **ADR-010 (Live Serve Provider Wiring, Key Revocation, Graceful Shutdown & Store Readiness):** [docs/adr/010-live-serve-provider-and-store-readiness.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/010-live-serve-provider-and-store-readiness.md)
- **ADR-011 (Shared Event Loop, Bounded Store Retention Pruning & Circuit Breaker Reset):** [docs/adr/011-shared-event-loop-retention-and-circuit-reset.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/011-shared-event-loop-retention-and-circuit-reset.md)
- **ADR-012 (Structured Access Log, Burst Socket Hardening, Multi-Tenant Budget Config & `429 Retry-After`):** [docs/adr/012-structured-access-log-and-multi-tenant-budget-config.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/012-structured-access-log-and-multi-tenant-budget-config.md)
- **ADR-013 (HTTP `HEAD` Probe Support & Atomic Tenant Key Rotation):** [docs/adr/013-head-probes-and-key-rotation.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/013-head-probes-and-key-rotation.md)
- **ADR-014 (OpenAI-Compatible SSE Streaming & Atomic `0o600` Auth-Config Writes):** [docs/adr/014-openai-sse-streaming-and-atomic-auth-writes.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/adr/014-openai-sse-streaming-and-atomic-auth-writes.md)
- **ADR-015 (WCAG 2.1 AA Accessibility, SQLite WAL Lifecycle & Enterprise Governance):** [docs/adr/015-wcag-accessibility-and-wal-lifecycle.md](docs/adr/015-wcag-accessibility-and-wal-lifecycle.md)
- **Contributing Guide (Setup, Static Checks, ADR Process & Security Rules):** [CONTRIBUTING.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/CONTRIBUTING.md)
- **Security Policy (Vulnerability Disclosure & Response SLA):** [SECURITY.md](SECURITY.md)
- **Environment Configuration Template (Zero-Secret Reference):** [.env.example](.env.example)
- **Changelog (Release History):** [CHANGELOG.md](CHANGELOG.md)
- **STRIDE Threat Model & AI-Router Security Mitigations:** [docs/THREAT_MODEL.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/THREAT_MODEL.md)
- **Sample Auditable Savings Receipt (Markdown):** [artifacts/demo/savings-receipt.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/artifacts/demo/savings-receipt.md)
- **Sample Auditable Savings Receipt (JSON):** [artifacts/demo/savings-receipt.json](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/artifacts/demo/savings-receipt.json)
- **Interactive Pareto Explorer (HTML):** [artifacts/demo/pareto-explorer.html](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/artifacts/demo/pareto-explorer.html)

---

## Contributing

Contributions are welcome! Please review [CONTRIBUTING.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/CONTRIBUTING.md) for local setup, static analysis (`ruff check`, `mypy --strict`), and ADR requirements, use the bug/feature/PR templates in `.github/`, and note that this project is licensed under the MIT License ([LICENSE](LICENSE)).
