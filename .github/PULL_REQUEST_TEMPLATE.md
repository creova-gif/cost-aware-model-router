## Summary

<!-- Provide a concise summary of the changes and the motivation behind them. -->

## Type of Change

- [ ] Bug fix (non-breaking change fixing an issue)
- [ ] New feature (non-breaking change adding functionality)
- [ ] Breaking change (fix or feature causing existing behavior to change)
- [ ] Documentation / ADR / Repository hygiene

## Verification Checklist

- [ ] `ruff check src tests` passes cleanly
- [ ] `mypy --strict src tests` passes cleanly
- [ ] `pytest -v` passes with deterministic unit tests
- [ ] Non-trivial design choices recorded in `docs/adr/` (using `docs/adr/000-template.md`)
- [ ] Checked against the 5 security invariants in `docs/THREAT_MODEL.md`:
  1. **Zero Raw Prompt/Completion Persistence by Default** (SHA-256 digests only in receipts, HTML reports, shadow logs, and OTel spans)
  2. **Zero `eval` / `exec` in Quality Evaluation & Receipt Verification** (strictly declarative evaluators and verifiers)
  3. **Fail-Closed Spend & Loop Protection ("Denial-of-Wallet" Defense)** (quantized 8-place `Decimal` caps, bounded concurrency, loop circuit breakers)
  4. **Honest Provenance & Tamper Detection** (`measured` / `modeled` / `unknown` tags, complete-case intersection, canonical SHA-256 receipt integrity)
  5. **Transport & Browser Hardening** (no plaintext `http://` auth to non-loopback hosts, `follow_redirects=False`, XSS-safe HTML/SVG rendering)
