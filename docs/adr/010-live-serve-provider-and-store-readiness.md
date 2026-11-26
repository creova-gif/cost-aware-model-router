# ADR-010: Live Serve Provider Wiring, Key Revocation, Graceful Shutdown, and Store Readiness
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-008` and `ADR-009`, `cost-router serve` supported offline `DeterministicFixtureAdapter` execution, circuit-breaker/fallback wiring, and `/readyz` circuit checks, but four operational gaps remained for production deployments:

1. **Duplicated / Missing Live Provider Wiring in `serve`:** `cost-router replay --live` already constructed hardened live adapters (`OpenAICompatibleAdapter` / `AnthropicAdapter` with `Semaphore(8)`, `--max-cost-usd` spend circuit breaker, non-loopback `http://` auth block, and `follow_redirects=False`), whereas `cost-router serve` only ran against `DeterministicFixtureAdapter` and lacked `--live` / `--provider openai|anthropic` flags.
2. **Tenant Key Revocation:** `cost-router issue-key` could provision hashed keys into a `TenantAuthRegistry` JSON file (`--auth-config`), but operators had no CLI subcommand to disable a compromised or retired key without manually editing JSON.
3. **Container Orchestrator Shutdown (`SIGTERM`):** `cost-router serve` handled `SIGINT` (`KeyboardInterrupt`), but container runtimes (Docker, Kubernetes, Cloud Run) send `SIGTERM` first, risking abrupt process termination without draining `ThreadingHTTPServer` or closing `SQLiteEconomicsStore`.
4. **Store Readiness in `GET /readyz`:** `/readyz` checked whether all tier circuits in `ProviderCircuitBreaker` were `OPEN`, but did not verify that an attached `SQLiteEconomicsStore` was still open and responsive (`SELECT 1`).

## Decision
1. **Shared `_build_live_adapter()` Helper Across `replay` and `serve --live --provider openai|anthropic` (`cli.py`):**
   - Extract live adapter construction into a single shared `_build_live_adapter()` helper used by both `cost-router replay --live` and `cost-router serve --live`.
   - Support `--provider openai|anthropic` (defaulting to `openai`), `--base-url`, `--api-key-env`, `--concurrency`, and `--max-cost-usd` consistently across both commands, instantiating `OpenAICompatibleAdapter` or `AnthropicAdapter` with identical security and spend-breaker guarantees.
2. **Key Revocation CLI & `SIGTERM` Graceful Shutdown (`cli.py`, `governance.py`):**
   - Add `cost-router revoke-key --key-id <id> --auth-config <path>` to load the persisted `TenantAuthRegistry` JSON, call `registry.disable_key(key_id)` (`enabled=false` while preserving the audit record), and atomically rewrite the JSON file.
   - Register a `SIGTERM` handler in `cost-router serve` that invokes `server.shutdown()` on a daemon thread, restores the previous signal handler in `finally`, and calls `server.server_close()` plus `store.close()` cleanly.
3. **Store Health Check in `GET /readyz` (`store.py`, `server.py`):**
   - Add `SQLiteEconomicsStore.is_healthy() -> bool`, returning `False` if the store is closed or if executing a lightweight probe (`SELECT 1`) raises `sqlite3.Error`, and `True` otherwise.
   - Update `GET /readyz` in `create_router_http_server` to return `503 Service Unavailable` when an attached `SQLiteEconomicsStore.is_healthy()` is `False` or when all tier circuits in `ProviderCircuitBreaker` are `OPEN`, and `200 OK` (`{"status":"ready"}`) otherwise.

## Alternatives Considered
- **Separate Live Adapter Builders for `replay` and `serve` (Rejected):** Duplicating `--base-url`, `--api-key-env`, `--max-cost-usd`, and provider selection logic would risk drift in HTTPS/SSRF guards or spend circuit-breaker enforcement between batch replay and live serving.
- **Deleting Revoked Key Records from `auth-config.json` (Rejected):** Hard-deleting entries removes the audit trail of `key_id` and `org_id`; flipping `enabled=false` via `disable_key()` preserves forensic traceability while immediately rejecting bearer tokens.
- **Calling `server.shutdown()` Directly Inside the `SIGTERM` Signal Handler (Rejected):** `HTTPServer.shutdown()` blocks until `serve_forever()` exits; invoking it synchronously on the main thread inside a signal handler would deadlock. Spawning a daemon thread to call `server.shutdown()` unblocks `serve_forever()` safely.
- **Checking Only Circuit Breakers in `/readyz` (Rejected):** If the SQLite WAL ledger connection is closed or corrupted, the gateway cannot durably record spend or telemetry; failing `/readyz` with `503` prevents orchestrators from routing traffic to a degraded replica.

## Consequences & Failure Modes
- **Positive:**
  - `cost-router serve --live --provider openai|anthropic` inherits the exact same hardened transport, spend breaker, and provenance guarantees as `cost-router replay --live`.
  - Operators can revoke compromised tenant keys with a single CLI command and rely on clean `SIGTERM` drain/close behavior in Kubernetes and Docker.
  - `/readyz` accurately reflects both downstream provider availability (circuit breakers) and local durable ledger availability (`SQLiteEconomicsStore.is_healthy()`).
- **Failure Modes & Mitigations:**
  - **Missing API Key Environment Variable at `serve --live` Startup:** `_build_live_adapter()` fails fast before binding the HTTP socket and exits non-zero, closing any opened `SQLiteEconomicsStore`.
  - **Revoking a Non-Existent `--key-id` or Missing `--auth-config`:** `revoke-key` catches the error, prints a diagnostic to `stderr`, and exits with status `1` without modifying the file on disk.
  - **Concurrent `auth-config.json` Reloads:** Because `serve` loads `--auth-config` at startup, revoking a key on disk requires restarting (or rolling) the server process to take effect in memory.
  - **Transient SQLite Lock or Closed Connection:** `SQLiteEconomicsStore.is_healthy()` catches `sqlite3.Error` and returns `False` rather than raising an unhandled `500` in `/readyz`, causing `/readyz` to return `503` cleanly.
