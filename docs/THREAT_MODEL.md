# Threat Model — Cost-Aware Model Router & FinOps Control Plane

- **Version:** `2.0.0` (Milestone 1 + Milestone 2 Architecture)
- **Owner:** Justin Mafie
- **Scope:** CLI Replay Engine (`cli.py`), Provider Adapters (`adapters.py`), Declarative Evaluators (`evaluators.py`), Policy & Pareto Optimizer (`policies.py`), Auditable Savings Receipt (`receipt.py`), Interactive HTML Explorer (`report.py`), Agent Budget Thermostat (`thermostat.py`), OpenAI-Compatible Shadow/Active Gateway (`gateway.py`), and OpenTelemetry Instrumentation (`telemetry.py`).

---

## 1. Security Philosophy & Design Invariants

An LLM router sits on the critical path between application code, sensitive prompts, external model providers, and FinOps billing ledgers. Our security posture enforces five non-negotiable invariants:

1. **Zero Raw Prompt/Completion Persistence by Default:** Receipts (`savings-receipt.json`), interactive HTML reports (`pareto-explorer.html`), shadow logs, and OpenTelemetry spans emit only SHA-256 hex digests (`prompt_sha256`, `response_sha256`). Raw text never touches disk or telemetry sinks unless explicitly handled in-memory during evaluation.
2. **Zero `eval` / `exec` in Quality Evaluation & Receipt Verification:** All quality evaluators (`json_schema`, `regex_match`, `exact_match`, `json_field_predicate`, `latency_sla`) and receipt verifiers are strictly declarative. No untrusted code or dynamic expressions are ever executed.
3. **Fail-Closed Spend & Loop Protection ("Denial-of-Wallet" Defense):** Both live batch replay (`--max-cost-usd`) and runtime agent routing (`AgentBudgetThermostat`) enforce quantized 8-place `Decimal` spend caps, bounded concurrency, and repeated-prompt loop circuit breakers.
4. **Honest Provenance & Tamper Detection:** Every metric carries an explicit `ProvenanceTag` (`measured`, `modeled`, `unknown`). Any request with an `unknown` metric is excluded from savings claims via a complete-case intersection while its burned spend is tracked separately.
5. **Transport & Browser Hardening:** API keys are never sent over plaintext `http://` to non-loopback hosts, HTTP redirects are disabled (`follow_redirects=False`), and embedded HTML data is escaped against `<script>` breakout and rendered exclusively via `textContent` and `document.createElementNS`.

---

## 2. Protected Assets

| Asset ID | Asset | Sensitivity | Security Goal (CIA / FinOps) |
|---|---|---|---|
| **A1** | **Provider API Credentials** (`OPENAI_API_KEY`, custom `--api-key-env`) | Critical | **Confidentiality:** Never logged, never serialized to receipts/HTML/spans, never transmitted over non-loopback plaintext `http://` or across untrusted HTTP redirects. |
| **A2** | **Tenant & Agent Isolation State** (`org_id`, `agent_id`, `policy_id`) | High | **Integrity & Isolation:** Prevent cross-tenant budget bleed, attribution spoofing, or noisy-neighbor spend exhaustion in `AgentBudgetThermostat`. |
| **A3** | **Auditable Savings Receipt** (`savings-receipt.json`, `integrity.digest`) | High | **Integrity:** Detect any post-hoc modification of costs, token counts, evaluator verdicts, provenance tags, or policy pass rates via canonical SHA-256 verification. |
| **A4** | **Prompts & Model Completions** (Customer PII / PHI / Source Code) | Critical | **Confidentiality:** Keep raw prompts and responses out of persisted artifacts, HTML reports, and OTel attributes by default (SHA-256 digests only). |
| **A5** | **Live Inference Spend & Provider Quota** (USD Budget Envelopes) | High | **Availability & Financial Integrity:** Prevent runaway agent retry loops, unbounded concurrency, or unexpected frontier escalations from exhausting financial budgets. |
| **A6** | **Operator Browser & CLI Runtime** (`pareto-explorer.html`, evaluator loop) | High | **Integrity & Availability:** Prevent DOM XSS when inspecting untrusted workloads and prevent Regular Expression Denial of Service (ReDoS) during evaluation. |

---

## 3. System Architecture & Trust Boundaries

```text
+-----------------------------------------------------------------------------------+
| TB1: Caller / Application Boundary (Untrusted Workload Input, CLI Args, SDK Call) |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          | WorkloadItem / ChatCompletionRequest
                                          v
+-----------------------------------------------------------------------------------+
| TB2: OpenAI-Compatible Gateway & Shadow Recorder (gateway.py / cli.py)            |
|  - Validates Pydantic v2 schemas (ConfigDict(extra="forbid", frozen=True))        |
|  - Hashes prompt -> prompt_sha256 immediately                                     |
+--------------------+-----------------------------------------+--------------------+
                     |                                         |
                     | Allocate Budget & Check Loop Streak     | Emit Redacted Spans
                     v                                         v
+----------------------------------------+   +--------------------------------------+
| TB3: Thermostat, Policy & Evaluators   |   | TB5a: Observability & Artifact Sink  |
|  - thermostat.py (Spend + Loop Guard)  |   |  - savings-receipt.{json,md}         |
|  - policies.py (2D Pareto + Cascade)   |   |  - pareto-explorer.html (Zero CDN)   |
|  - evaluators.py (Zero-eval + ReDoS)   |   |  - telemetry.py (OTel gen_ai.*)      |
+--------------------+-------------------+   +--------------------------------------+
                     |
                     | Validated Tier Dispatch (Semaphore(8), max_cost_usd guard)
                     v
+-----------------------------------------------------------------------------------+
| TB4: ProviderAdapter Egress Boundary (adapters.py)                                |
|  - validate_endpoint_security() blocks plaintext http:// auth to remote hosts     |
|  - follow_redirects=False, timeout=30s, fail-fast on 401/403/404                  |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          | HTTPS (or loopback HTTP for local Ollama)
                                          v
+-----------------------------------------------------------------------------------+
| TB5b: External Model Providers (OpenAI API, vLLM, Ollama, Anthropic Gateways)     |
+-----------------------------------------------------------------------------------+
```

### Trust Boundary Definitions
- **TB1 -> TB2 (Caller to Router Gateway/CLI):** Workload JSONL files, prompts, regex rules, and CLI flags cross from user/caller space into the router. Inputs may contain malformed JSON, non-finite floats (`NaN`/`Infinity`), pathological regexes, or XSS payloads.
- **TB2 -> TB3 (Gateway to Thermostat & Policy/Evaluator Engine):** Evaluates model outputs (which are untrusted strings returned by external LLMs) and enforces per-`(org_id, agent_id)` budget state transitions.
- **TB3 -> TB4 (Router Core to HTTP Provider Egress):** Attaches `Authorization: Bearer` credentials and dispatches network requests to model endpoints.
- **TB4 -> TB5b (Egress to External LLM APIs):** External APIs may return HTTP `429`/`5xx`, malformed JSON, negative or missing `usage` token counts, or malicious completion strings.
- **TB2/TB3 -> TB5a (Router to Persisted Artifacts & Browser UI):** Serializes `SavingsReceipt` and renders `pareto-explorer.html` opened in an engineer's or auditor's browser.

---

## 4. STRIDE Threat Analysis & Implemented Mitigations

### S — Spoofing

| Threat | Attack Vector | Concrete Codebase Mitigation | Verification |
|---|---|---|---|
| **S1: Plaintext Credential Interception / Rogue Endpoint Spoofing** | Misconfigured `--base-url http://remote-host/v1` leaks `Authorization: Bearer <key>` over cleartext network. | `validate_endpoint_security()` in `src/cost_aware_model_router/adapters.py` parses `base_url`, permits only `http`/`https`, and raises `ValueError` if `scheme == "http"`, an `api_key` is present, and `hostname` is not in `LOOPBACK_HOSTS` (`localhost`, `127.0.0.1`, `::1`, `[::1]`). | `tests/test_adapters.py` |
| **S2: Open Redirect Credential Exfiltration** | A compromised or misconfigured proxy returns HTTP `301`/`302`/`307` redirecting an authenticated POST request to an external host, forwarding the `Authorization` header. | `OpenAICompatibleAdapter._call_single()` explicitly sets `follow_redirects=False` on all `httpx.AsyncClient.post()` calls and treats non-200 responses as `ProvenanceTag.UNKNOWN` errors. | `src/cost_aware_model_router/adapters.py` |
| **S3: Tenant / Agent Attribution Spoofing** | Requests omit or forge `org_id` / `agent_id` fields or inject extra fields to bypass policy validation. | All domain schemas (`WorkloadItem`, `SavingsReceipt`, `BudgetPolicy`) enforce `ConfigDict(frozen=True, extra="forbid")` and non-empty string constraints; thermostat ledgers key state strictly by `(org_id, agent_id)`. | `tests/test_schemas.py` |

### T — Tampering

| Threat | Attack Vector | Concrete Codebase Mitigation | Verification |
|---|---|---|---|
| **T1: Post-Hoc Savings Receipt Manipulation** | An operator edits `total_cost_usd`, `pass_rate`, or `provenance` in `savings-receipt.json` to inflate savings claims in a PR or audit. | `receipt.py` embeds a `ReceiptIntegrity` envelope (`algorithm: "sha256"`, `canonicalization: "sorted-keys-utf8-compact-json-decimal-strings"`). `verify_receipt_dict()` hashes the **parsed raw JSON dict** (minus `integrity`) *before* Pydantic validation so default field population cannot mask tampering, and verifies `complete_case_requests + excluded_unknown_requests == total_workload_requests`. | `tests/test_receipt.py`, `tests/test_cli_e2e.py` |
| **T2: Floating-Point Drift & `NaN`/`Infinity` Injection** | Injecting `NaN` or `Infinity` into JSON payloads or relying on IEEE-754 float addition for USD totals causes cross-platform hash mismatches or bypassed comparisons. | All monetary fields use `UsdDecimal` (`quantize_usd()` to 8 decimal places with `ROUND_HALF_UP` and `dec.is_finite()`). `canonical_json_bytes()` uses `allow_nan=False` and formats floats to fixed 6-decimal strings; `verify_receipt_file()` rejects `NaN`/`Infinity` at `json.loads(parse_constant=...)`. | `src/cost_aware_model_router/receipt.py`, `src/cost_aware_model_router/schemas.py` |
| **T3: Misrepresenting Unsigned Digest as Non-Repudiation** | Claiming a plain SHA-256 hash prevents an attacker with write access to the JSON file from recomputing the hash. | `ReceiptIntegrity.note` explicitly states in both JSON and Markdown receipts: *"Unsigned SHA-256 digest detects accidental corruption or payload drift; not cryptographic non-repudiation."* | `src/cost_aware_model_router/schemas.py` |

### R — Repudiation & Audit Opacity

| Threat | Attack Vector | Concrete Codebase Mitigation | Verification |
|---|---|---|---|
| **R1: Blending Simulated Estimates with Live Measured Spend** | Presenting synthetic fixture costs or catalog estimates as actual provider-billed savings. | Every `ExecutionTrace` and `CellEvaluation` carries independent `MetricProvenance` tags (`tokens_provenance`, `latency_provenance`, `verdict_provenance`, `cost_provenance`). Live calls mark `cost_provenance = MEASURED` *only* when the provider response returns billed USD (`usage.cost_usd`), otherwise `MODELED`. | `src/cost_aware_model_router/adapters.py`, `tests/test_adapters.py` |
| **R2: Silent Omission of Failed Requests (" survivorship bias")** | Dropping failed or timed-out API calls on cheaper tiers to make `compact` look cheaper or more accurate than `frontier`. | `build_evaluation_matrix()` computes a **complete-case intersection** across all 3 tiers (`complete_case_ids`). If *any* tier is `UNKNOWN` for a request, the request is excluded from all policies' savings percentages, `excluded_unknown_requests` is incremented, and `unknown_burned_cost_usd` is reported separately. | `src/cost_aware_model_router/policies.py`, `tests/test_policies.py` |
| **R3: Undisclosed In-Sample Threshold Overfitting** | Tuning complexity thresholds on a 60-prompt workload and claiming out-of-sample generalization. | Threshold policies tuned on the evaluation grid set `in_sample_fit=True`, which is surfaced in `savings-receipt.json`, the Markdown table (`YES (grid)`), and the HTML Pareto chart (`[grid]` / `[in-sample]`). | `src/cost_aware_model_router/policies.py`, `src/cost_aware_model_router/report.py` |

### I — Information Disclosure

| Threat | Attack Vector | Concrete Codebase Mitigation | Verification |
|---|---|---|---|
| **I1: PII / PHI / Proprietary Prompt Leakage in Artifacts** | Sharing `savings-receipt.json`, `pareto-explorer.html`, or OTel traces leaks customer prompts or LLM completions. | `CellEvaluation` omits `prompt` and `response_text`, storing only `prompt_sha256` and `response_sha256`. `telemetry.py` and `gateway.py` shadow logs likewise record only SHA-256 digests. | `src/cost_aware_model_router/schemas.py`, `tests/test_report.py` |
| **I2: Stored / Reflected XSS in `pareto-explorer.html`** | A workload name, request ID, or failure reason contains `</script><script>alert(1)</script>` or HTML markup that executes when an auditor opens `pareto-explorer.html`. | 1. `escape_json_for_script_tag()` in `report.py` escapes `&`, `<`, `>`, `\u2028`, and `\u2029` to `\u0026`, `\u003c`, `\u003e`, etc. inside `<script type="application/json">`.<br>2. All HTML header interpolations use `html.escape(..., quote=True)`.<br>3. Client-side JS builds the SVG chart exclusively via `document.createElementNS` / `setAttribute` and populates all DOM nodes via `textContent` (zero `innerHTML` or `outerHTML`). | `src/cost_aware_model_router/report.py`, `tests/test_report.py` |
| **I3: Secret Leakage in Source or CI Logs** | Hardcoding API keys in fixtures, CLI flags, or git commits. | Live adapter reads API keys via `--api-key-env` (environment variable lookup in `cli.py`); `.github/workflows/ci.yml` runs `gitleaks` on repository history. | `.github/workflows/ci.yml` |

### D — Denial of Service & Financial Exhaustion ("Denial of Wallet")

| Threat | Attack Vector | Concrete Codebase Mitigation | Verification |
|---|---|---|---|
| **D1: Regular Expression Denial of Service (ReDoS)** | A workload specifies a catastrophic backtracking regex such as `(a+)+$` in `EvalRule(eval_type="regex_match")`, freezing the evaluator thread on crafted LLM responses. | `validate_safe_regex()` in `evaluators.py` enforces `MAX_REGEX_PATTERN_LENGTH = 256` and rejects nested quantifiers matching `_NESTED_QUANTIFIER_RE` (`\([^()]*[+*?][^()]*\)[+*?{]`). | `src/cost_aware_model_router/evaluators.py`, `tests/test_evaluators.py` |
| **D2: Runaway Agent Loops & Unbounded Live Spend** | An autonomous agent enters a retry loop sending identical failing prompts to a frontier model, or a large JSONL replay drains an OpenAI billing account. | 1. **Live Replay Guard (`adapters.py`):** `OpenAICompatibleAdapter` enforces an async lock-guarded `--max-cost-usd` circuit breaker (`max_cost_usd_circuit_breaker_tripped`), `asyncio.Semaphore(8)` concurrency cap, `30.0s` request timeout, max 2 retries with `Retry-After` cap (`<= 2.0s`), and per-tier fail-fast abort on HTTP `401`/`403`/`404`.<br>2. **Runtime Agent Thermostat (`thermostat.py`):** `AgentBudgetThermostat` progressively steps down tiers (`NORMAL -> THROTTLED_TO_MID -> THROTTLED_TO_COMPACT -> CIRCUIT_OPEN`) as spend approaches budget limits and trips `CIRCUIT_OPEN` immediately when consecutive identical `prompt_sha256` requests exceed `max_identical_prompt_streak`. | `tests/test_adapters.py`, `tests/test_thermostat.py` |
| **D3: Cascade Latency Amplification** | `cascade-escalation` attempts `compact -> mid -> frontier` sequentially even after `compact` already breached the caller's `latency_sla`, wasting Mid/Frontier tokens on an already-late request. | `_evaluate_item_under_policy()` in `policies.py` re-evaluates `latency_sla` against cumulative latency at every step of the cascade and short-circuits immediately when `latency_sla` is violated. | `src/cost_aware_model_router/policies.py`, `tests/test_policies.py` |

### E — Elevation of Privilege / Arbitrary Code Execution

| Threat | Attack Vector | Concrete Codebase Mitigation | Verification |
|---|---|---|---|
| **E1: Arbitrary Code Execution via Custom Evaluators** | Allowing Python lambdas, `eval()`, or dynamic module imports in workload JSONL files lets an untrusted workload execute arbitrary shell commands on the runner. | `EvalType` is a closed `StrEnum` (`json_schema`, `regex_match`, `exact_match`, `json_field_predicate`, `latency_sla`) handled with an exhaustive `match` and `typing.assert_never`. Zero `eval`, `exec`, `compile`, or `pickle` calls exist anywhere in the codebase. | `src/cost_aware_model_router/evaluators.py` |

---

## 5. Residual Risks & Milestone 3+ Hardening Roadmap

1. **Cryptographic Receipt Signing (Asymmetric Non-Repudiation):** Milestone 1–2 receipts use canonical unsigned SHA-256 digests to detect payload drift and accidental modification. For untrusted third-party FinOps audits, Milestone 3 will add optional Ed25519 / Sigstore cosign detached signatures over `canonical_json_bytes`.
2. **Distributed Multi-Replica Thermostat State:** `AgentBudgetThermostat` maintains in-process state suitable for single-process gateways and SDKs. Multi-replica Kubernetes deployments in Milestone 3 will back the thermostat ledger with atomic Redis Lua token-bucket scripts.
3. **Semantic Prompt Similarity Loop Detection:** Loop detection currently matches exact normalized `prompt_sha256` streaks; Milestone 3 will add lightweight locality-sensitive hashing (MinHash / SimHash) to catch paraphrased agent retry loops where timestamps or UUIDs vary inside the prompt.
