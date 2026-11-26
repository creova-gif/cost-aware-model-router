# ADR-016: Security Headers, OpenAI SDK Interoperability, and OpenAPI 3.1.0 Specification
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-015`, three API surface, browser-security, and SDK-interoperability gaps remained for drop-in OpenAI client compatibility and production HTTP hardening:

1. **Browser & Feature-Policy Hardening (`server.py`):**
   - `_RouterRequestHandler._send_bytes` emitted `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`, but lacked a `Permissions-Policy` header disabling unused browser capabilities (`geolocation`, `microphone`, `camera`) across all responses.
   - `GET /dashboard` (`text/html; charset=utf-8`) lacked a `Content-Security-Policy` (CSP) header restricting script/style/connect/frame/form/base-URI origins for the zero-CDN single-file HTML dashboard.
2. **OpenAI SDK Optional Parameter Interoperability (`gateway.py`):**
   - `ChatCompletionRequest` enforces `model_config = ConfigDict(extra="forbid")` to reject unknown or misspelled fields fail-closed. However, standard OpenAI Python/TypeScript SDK callers frequently pass optional parameters (`top_p`, `stop`, `user`, `seed`), causing `422` / `400` validation rejections when `extra="forbid"` is active.
3. **Machine-Readable API Discovery & Single-Model Lookup (`server.py`):**
   - `GET /v1/models` listed configured tier models and `"auto"`, but standard OpenAI SDK clients and tooling also query `GET /v1/models/{model_id}` for single-model metadata lookup, and API gateways/integrators expect a machine-readable OpenAPI `3.1.0` schema at `GET /openapi.json`.

## Decision
1. **HTTP `Permissions-Policy` & Dashboard `Content-Security-Policy` (`server.py`):**
   - Emit `Permissions-Policy: geolocation=(), microphone=(), camera=()` on every response in `_RouterRequestHandler._send_bytes`.
   - Extend `_RouterRequestHandler._send_text` with an optional `extra_headers: dict[str, str] | None = None` keyword argument and attach a strict CSP header on `GET /dashboard`:
     `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
2. **OpenAI SDK Optional Parameters on `ChatCompletionRequest` (`gateway.py`):**
   - Add explicit optional fields to `ChatCompletionRequest` while preserving `extra="forbid"`:
     - `top_p: float | None = Field(default=None, ge=0.0, le=1.0)`
     - `stop: str | list[str] | None = None`
     - `user: str | None = None`
     - `seed: int | None = None`
3. **OpenAPI `3.1.0` Specification & Single-Model Lookup (`server.py`):**
   - Expose `GET /openapi.json` returning an OpenAPI `3.1.0` JSON document describing the gateway's HTTP routes, request/response schemas, and bearer authentication scheme.
   - Expose `GET /v1/models/{model_id}` (guarded by `TenantRole.ROUTER_CLIENT` RBAC) returning the matching OpenAI-compatible model object (`id`, `object: "model"`, `owned_by`, `tier`) for `"auto"` or configured tier models, or `404` when `model_id` is not registered.

## Alternatives Considered
- **Relaxing `extra="forbid"` to `extra="ignore"` on `ChatCompletionRequest` (Rejected):** Silently ignoring unknown fields hides client typos and weakens schema validation guarantees. Explicitly whitelisting `top_p`, `stop`, `user`, and `seed` preserves fail-closed validation for unsupported fields.
- **Using `'unsafe-eval'` or External Nonces/CDNs in Dashboard CSP (Rejected):** The dashboard is a zero-CDN single-file HTML response (`THREAT_MODEL.md`) with inline `<script>` and `<style>` blocks; `default-src 'none'` with `connect-src 'self'`, `base-uri 'none'`, `form-action 'none'`, and `frame-ancestors 'none'` blocks exfiltration, clickjacking, and external asset loading without breaking inline rendering.
- **Pulling in FastAPI/OpenAPI Codegen Dependencies (Rejected):** The HTTP server is intentionally zero-dependency (`ThreadingHTTPServer` in `server.py`, `ADR-004`); serving a deterministic OpenAPI `3.1.0` JSON document directly keeps runtime dependencies minimal.

## Consequences & Failure Modes
- **Positive:**
  - All HTTP responses enforce `Permissions-Policy`, and `/dashboard` enforces a strict CSP that prevents framing, form submission, and third-party network calls.
  - Standard OpenAI SDK calls passing `top_p`, `stop`, `user`, or `seed` succeed without relaxing `extra="forbid"`.
  - API consumers and OpenAI-compatible clients can introspect `GET /openapi.json` (`3.1.0`) and query `GET /v1/models/{model_id}` directly.
- **Failure Modes & Mitigations:**
  - **Dashboard Inline Script/Style Constraints:** `script-src 'unsafe-inline'` and `style-src 'unsafe-inline'` are required while `dashboard.py` embeds inline blocks; any future external script/style or third-party fetch in `dashboard.py` will be blocked by `default-src 'none'` / `connect-src 'self'` until explicitly reviewed.
  - **OpenAPI Schema Drift:** Because `GET /openapi.json` is served without a code-generating web framework, adding new routes or changing request/response schemas in `server.py` or `gateway.py` requires updating the OpenAPI document and its tests in lockstep.
