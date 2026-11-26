# ADR-013: HTTP HEAD Probe Support and Atomic Tenant Key Rotation
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-012`, `_RouterHTTPServer` (`server.py`) and `TenantAuthRegistry` (`governance.py`) + `cli.py` still had two operational gaps for production load-balancer integration and multi-tenant credential lifecycle management:

1. **Load-Balancer & Readiness `HEAD` Probes (`server.py`):** Container orchestrators, reverse proxies, and cloud load balancers (as well as `curl -I`) commonly issue HTTP `HEAD` requests to `/healthz`, `/readyz`, and `GET` endpoints. Because `BaseHTTPRequestHandler` does not implement `do_HEAD` by default, `HEAD` probes previously returned `501 Unsupported method ('HEAD')`.
2. **Atomic Key Rotation & Key Inventory Inspection (`governance.py`, `cli.py`):** `TenantAuthRegistry` supported `issue_key()` (`cost-router issue-key`) and `disable_key()` (`cost-router revoke-key`), but lacked an atomic `rotate_key()` operation and a read-only `cost-router list-keys` CLI subcommand to inspect registered key metadata without exposing raw tokens or SHA-256 digests.

## Decision
1. **HTTP `HEAD` Probe Support on `/healthz`, `/readyz`, and `GET` Routes (`server.py`):**
   - Implement `do_HEAD` on `_RouterRequestHandler` by delegating to the `GET` route handler while suppressing the body write (`self.wfile.write(...)` skipped when `self.command == "HEAD"`).
   - Preserve exact status codes (`200`, `503`, `401`, `403`, `404`), `Content-Type`, `Content-Length`, `X-Request-Id`, security headers, and `access_log` telemetry (`method: "HEAD"`).
2. **Atomic `TenantAuthRegistry.rotate_key()` (`governance.py`):**
   - Add `TenantAuthRegistry.rotate_key(key_id, ...)` to atomically issue a replacement `TenantKeyRecord` inheriting `org_id`, `role`, `rate_limit_rps`, and `burst_capacity` under `self._lock`, optionally disabling the prior key in the same critical section.
3. **`cost-router rotate-key` and `cost-router list-keys` CLI Subcommands (`cli.py`):**
   - Add `cost-router rotate-key --key-id <id> --auth-config <path> [--keep-old]` to rotate a virtual API key in-place in `auth-config.json` and print the new raw token once.
   - Add `cost-router list-keys --auth-config <path> [--org-id <org_id>]` to list non-secret key metadata (`key_id`, `org_id`, `role`, `key_prefix`, `enabled`, `rate_limit_rps`, `burst_capacity`) without printing `key_sha256` or raw secrets.

## Alternatives Considered
- **Separate `HEAD`-Only Handlers for `/healthz` and `/readyz` (Rejected):** Duplicating readiness and auth checks in `do_HEAD` risks status-code or header drift relative to `do_GET`.
- **Manual Two-Step `issue-key` + `revoke-key` Rotation (Rejected):** Requiring operators to copy `org_id`, `role`, and rate-limit settings manually between two commands is error-prone and non-atomic.

## Consequences & Failure Modes
- **Positive:**
  - `HEAD /healthz`, `HEAD /readyz`, and `HEAD` requests on `GET` routes return accurate status codes and headers with zero body bytes for load balancers and `curl -I` probes.
  - Operators can audit tenant keys with `cost-router list-keys` and rotate keys atomically with `cost-router rotate-key`.
- **Failure Modes & Mitigations:**
  - **Immediate Invalidation During Rollout:** Rotating a key with default `disable_old=True` invalidates the old token immediately; `--keep-old` supports zero-downtime overlapping rollouts followed by `cost-router revoke-key`.
  - **Unknown `key_id`:** `rotate_key()` raises `KeyError` and the CLI exits non-zero without modifying `auth-config.json`.
