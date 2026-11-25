# Contributing to Cost-Aware Model Router

Thank you for contributing! This project follows the shared engineering standards in [AGENTS.md](AGENTS.md) and the security invariants in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## 1. Local Development Setup (Python 3.12+)

We recommend using `uv` to create an isolated virtual environment and install the package in editable mode with development dependencies:

```bash
uv venv --python 3.12
source .venv/bin/activate
pip install -e ".[dev]"
```

Verify the CLI is available:

```bash
cost-router --help
```

## 2. Required Static Analysis & Testing

Every pull request must pass all static checks and deterministic unit/E2E tests before review:

```bash
ruff check src tests
mypy --strict src tests
pytest -v
```

### Code Quality Standards
- **Python 3.12+ & Pydantic v2:** Use `ConfigDict(frozen=True, extra="forbid")` for domain models and `Decimal` (`UsdDecimal` / `quantize_usd`) for all USD monetary calculations.
- **Strict Typing:** All production and test code must pass `mypy --strict src tests`.
- **No Silent Exception Swallowing:** Never use bare `except Exception: pass`.
- **Deterministic Tests:** Every PR must include deterministic unit tests; never claim benchmark numbers without running them.

## 3. Architecture Decision Records (ADRs)

Record every non-trivial design choice in `docs/adr/` using [docs/adr/000-template.md](docs/adr/000-template.md).
- Number ADRs sequentially (`001-...`, `002-...`, `003-...`).
- Include **Context**, **Decision**, **Alternatives Considered**, and **Consequences & Failure Modes**.
- Link new ADRs from the **Architecture, Design & Security Documentation** section of [README.md](README.md).

## 4. Security Rules & Threat Model

All contributions must uphold the five security invariants documented in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md):

1. **Zero Raw Prompt/Completion Persistence by Default:** Persist and log only SHA-256 hex digests (`prompt_sha256`, `response_sha256`) in receipts, HTML reports, shadow logs, and OTel spans.
2. **Zero `eval` / `exec`:** Quality evaluators and receipt verifiers must remain strictly declarative.
3. **Fail-Closed Spend & Loop Protection:** Respect quantized `Decimal` spend caps (`--max-cost-usd`, `AgentBudgetThermostat`) and repeated-prompt loop circuit breakers.
4. **Honest Provenance:** Tag every metric with `measured`, `modeled`, or `unknown` and preserve complete-case intersection accounting.
5. **No Secrets or Sensitive Fixtures:** Never commit `.env` files, API keys, or PII/PHI fixtures. Keep HTTP redirects disabled (`follow_redirects=False`) and escape untrusted strings in HTML (`report.py`) and Prometheus labels (`metrics.py`).
