# Security Policy

## Supported Versions

Only the current minor release series receives security updates and patches.

| Version | Supported |
|---|---|
| `0.4.x` | :white_check_mark: Supported |
| `< 0.4.0` | :x: Unsupported |

## Reporting a Vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

To report a security issue privately, use **GitHub Private Vulnerability Reporting / Security Advisories** on the [`creova-gif/cost-aware-model-router`](https://github.com/creova-gif/cost-aware-model-router/security/advisories/new) repository.

When submitting a report, please include:

- A clear description of the vulnerability and its potential impact.
- Step-by-step reproduction instructions or a minimal proof of concept.
- The affected version(s) and configuration.

## Core Security Invariants

Our security posture enforces five non-negotiable invariants (see [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the full STRIDE analysis and trust boundaries):

1. **Zero Raw Prompt/Completion Persistence by Default:** Receipts (`savings-receipt.json`), interactive HTML reports (`pareto-explorer.html`), shadow logs, and OpenTelemetry spans emit only SHA-256 hex digests (`prompt_sha256`, `response_sha256`). Raw text never touches disk or telemetry sinks unless explicitly handled in-memory during evaluation.
2. **Zero `eval` / `exec` in Quality Evaluation & Receipt Verification:** All quality evaluators (`json_schema`, `regex_match`, `exact_match`, `json_field_predicate`, `latency_sla`) and receipt verifiers are strictly declarative. No untrusted code or dynamic expressions are ever executed.
3. **Fail-Closed Spend & Loop Protection ("Denial-of-Wallet" Defense):** Both live batch replay (`--max-cost-usd`) and runtime agent routing (`AgentBudgetThermostat`) enforce quantized 8-place `Decimal` spend caps, bounded concurrency, and repeated-prompt loop circuit breakers.
4. **Honest Provenance & Tamper Detection:** Every metric carries an explicit `ProvenanceTag` (`measured`, `modeled`, `unknown`). Any request with an `unknown` metric is excluded from savings claims via a complete-case intersection while its burned spend is tracked separately.
5. **Transport & Browser Hardening:** API keys are never sent over plaintext `http://` to non-loopback hosts, HTTP redirects are disabled (`follow_redirects=False`), and embedded HTML data is escaped against `<script>` breakout and rendered exclusively via `textContent` and `document.createElementNS`.
