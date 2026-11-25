# Cost-Aware Model Router & FinOps Control Plane

**Evidence-first 3-tier LLM routing, interactive 2D Pareto frontier optimization, autonomous Agent Budget Thermostat, shadow-mode gateway, and SHA-256 auditable FinOps savings receipts.**

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
| [adapters.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/adapters.py) | `ProviderAdapter` protocol, bundled 60-prompt workload generator, `DeterministicFixtureAdapter`, and hardened async `OpenAICompatibleAdapter` (`Semaphore(8)`, `--max-cost-usd` breaker, non-loopback `http://` auth block, `follow_redirects=False`). |
| [evaluators.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/evaluators.py) | Declarative evaluator engine (`json_schema`, ReDoS-guarded `regex_match`, `exact_match`, `json_field_predicate`, `latency_sla`) separating `content_passed` from `latency_passed`. |
| [policies.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/policies.py) | Complete-case `UNKNOWN` intersection, deployable policies (`static_tier`, `complexity_threshold`, `cascade_escalation`), `ear_oracle` hindsight benchmark, and exact 2D Pareto frontier solver (`compute_2d_pareto_frontier`, `select_policy_for_floor`). |
| [receipt.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/receipt.py) | Canonical JSON serializer (`allow_nan=False`, sorted UTF-8 keys, fixed-precision strings), SHA-256 `SavingsReceipt` builder, Markdown renderer, and raw-dict-first `verify_receipt_file()`. |
| [report.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/report.py) | Self-contained, XSS-hardened HTML generator (`pareto-explorer.html`) with interactive quality floor slider, `document.createElementNS` SVG chart, and filterable decision audit table. |
| [thermostat.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/thermostat.py) | Autonomous `AgentBudgetThermostat` with per-`(org_id, agent_id)` spend ledgers, progressive tier throttling (`NORMAL -> THROTTLED_TO_MID -> THROTTLED_TO_COMPACT -> CIRCUIT_OPEN`), and repeated-`prompt_sha256` loop breaker. |
| [gateway.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/gateway.py) | `OpenAIRouterGateway` supporting `/v1/chat/completions` (`ChatCompletionRequest` / `ChatCompletionResponse`), non-disruptive shadow-mode counterfactual evaluation (`ShadowComparisonRecord`), and `/v1/finops/shadow-summary`. |
| [telemetry.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/telemetry.py) | OpenTelemetry `gen_ai.*` and `cost_router.*` span instrumentation (`OTelSpanRecord`, `InMemorySpanExporter`), declarative policy loader (`load_policies_from_json`), and stratified holdout splitting (`split_workload_holdout`). |
| [cli.py](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/src/cost_aware_model_router/cli.py) | Command-line interface exposing `cost-router demo`, `cost-router replay`, and `cost-router verify-receipt`. |

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
- **STRIDE Threat Model & AI-Router Security Mitigations:** [docs/THREAT_MODEL.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/docs/THREAT_MODEL.md)
- **Sample Auditable Savings Receipt (Markdown):** [artifacts/demo/savings-receipt.md](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/artifacts/demo/savings-receipt.md)
- **Sample Auditable Savings Receipt (JSON):** [artifacts/demo/savings-receipt.json](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/artifacts/demo/savings-receipt.json)
- **Interactive Pareto Explorer (HTML):** [artifacts/demo/pareto-explorer.html](file:///Users/justinmafie/Documents/antigravity/cost-aware-model-router/artifacts/demo/pareto-explorer.html)
