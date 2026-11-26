---
name: Feature request
about: Propose a new routing policy, provider adapter, or FinOps capability
title: "[Feat]: "
labels: enhancement
assignees: ''
---

## Problem Statement

A clear and concise description of the problem or missing FinOps / routing capability.

## Proposed Solution

Describe the proposed routing policy, provider adapter, telemetry integration, or FinOps feature, including expected CLI/SDK ergonomics and receipt/provenance behavior.

## Security / Provenance Impact

Assess how this proposal interacts with the router's security and accounting invariants (`docs/THREAT_MODEL.md`):

- **Prompt/Completion Persistence:** Does it preserve zero raw prompt/completion persistence by default (SHA-256 digests only)?
- **Declarative Evaluation:** Does it avoid `eval`/`exec` or dynamic code execution?
- **Spend & Loop Protection:** How does it integrate with `--max-cost-usd` / `AgentBudgetThermostat` fail-closed caps?
- **Provenance & Complete-Case Accounting:** Does it tag metrics accurately (`measured`, `modeled`, `unknown`) without survivorship bias?
- **Transport / Browser Hardening:** Does it uphold HTTPS / loopback-only HTTP, `follow_redirects=False`, and XSS-safe reporting?
