# ADR-011: Shared Event Loop, Bounded Store Retention Pruning, and Circuit Breaker Reset
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-010`, the local HTTP gateway (`cost-router serve`), durable SQLite ledger (`SQLiteEconomicsStore`), and `ProviderCircuitBreaker` were production-hardened for live provider routing and readiness probing, but three operational bottlenecks remained in long-running deployments:

1. **Per-Request `asyncio.run(...)` Overhead & Connection Pool Churn (`server.py`):** Each `POST /v1/chat/completions` request in `ThreadingHTTPServer` previously spawned and tore down a fresh `asyncio` event loop via `asyncio.run(...)`, preventing `httpx.AsyncClient` connection/transport reuse across requests and adding unnecessary event-loop initialization overhead under concurrent traffic.
2. **Unbounded Telemetry Table Growth (`store.py`, `cli.py`):** While `spend_ledger` rows are small and required for cumulative `summarize_economics()` totals, `shadow_comparisons` and `otel_spans` grow monotonically in long-lived SQLite WAL databases without a bounded retention-pruning mechanism.
3. **Manual Circuit Breaker Recovery (`resilience.py`, `server.py`):** Once `ProviderCircuitBreaker` tripped a `ModelTier` to `OPEN`, operators had to wait for `recovery_timeout_seconds` or restart the server process because there was neither a programmatic `reset()` method nor an admin-authenticated HTTP endpoint to clear tripped circuits after an upstream incident resolved.

## Decision
1. **Shared Background Event Loop via `_AsyncLoopRunner` & `_RouterHTTPServer` (`server.py`):**
   - Introduce `_AsyncLoopRunner`, which owns a single `asyncio.new_event_loop()` running `run_forever()` on a dedicated daemon thread (`router-async-loop`).
   - Subclass `ThreadingHTTPServer` as `_RouterHTTPServer` to hold `self.loop_runner = _AsyncLoopRunner()` and dispatch `POST /v1/chat/completions` coroutines via `loop_runner.run(coro)` (`asyncio.run_coroutine_threadsafe(coro, loop).result()`).
   - Shut down async generators (`shutdown_asyncgens()`) and close the loop cleanly inside `_RouterHTTPServer.server_close()` (calling `loop_runner.close()` before `super().server_close()`), and close unawaited coroutines if `run()` is called after shutdown.
2. **Bounded Telemetry Retention Pruning via `SQLiteEconomicsStore.prune_older_than()` & `cost-router prune-db` (`store.py`, `cli.py`):**
   - Add `SQLiteEconomicsStore.prune_older_than(max_age_seconds: float, *, now_epoch: float | None = None) -> dict[str, int]`, validating that `max_age_seconds` is finite and `> 0` (and `now_epoch`, if provided, is finite), computing `cutoff_unix_ns = int((effective_now - max_age_seconds) * 1_000_000_000)`, and deleting rows with `created_at_unix_ns < cutoff_unix_ns` from `shadow_comparisons` and `otel_spans` in a single transaction while leaving `spend_ledger` untouched so `summarize_economics()` totals remain intact.
   - Expose `cost-router prune-db` in the CLI to run bounded retention pruning against an on-disk SQLite store for scheduled maintenance.
3. **Admin-Gated Circuit Breaker Reset via `ProviderCircuitBreaker.reset()` & `POST /v1/finops/circuits/reset` (`resilience.py`, `server.py`):**
   - Add `ProviderCircuitBreaker.reset(tier: ModelTier | None = None) -> None` to atomically reset either a single `ModelTier` or all tiers to `CircuitState.CLOSED` with `consecutive_failures = 0`, `consecutive_successes = 0`, and `opened_at = None`.
   - Expose `POST /v1/finops/circuits/reset` on `_RouterHTTPServer`, gated by `TenantRole.ADMIN` RBAC, allowing operators to clear tripped circuits on demand without restarting the gateway.

## Alternatives Considered
- **Per-Thread `threading.local()` Event Loops (Rejected):** Keeping one loop per `ThreadingHTTPServer` worker thread would still create unbounded loops under thread churn and prevent a single shared `httpx.AsyncClient` pool across worker threads.
- **Deleting Old `spend_ledger` Rows During Pruning (Rejected):** `summarize_economics()` aggregates total spend, request counts, and per-org/agent/tier breakdowns directly from `spend_ledger`; pruning `spend_ledger` would silently alter historical FinOps totals.
- **Allowing `AUDITOR` Role on `POST /v1/finops/circuits/reset` (Rejected):** `AUDITOR` is strictly read-only (`GET /v1/finops/economics`, `GET /v1/finops/circuits`, `GET /v1/finops/shadow-summary`). Mutating circuit state can route traffic back to a recently failing upstream provider, so `TenantRole.ADMIN` is required.

## Consequences & Failure Modes
- **Positive:**
  - Concurrent HTTP requests share a single long-lived `asyncio` event loop, eliminating per-request `asyncio.run(...)` teardown and enabling persistent async client/transport reuse.
  - Operators can bound SQLite database growth in production via `SQLiteEconomicsStore.prune_older_than()` / `cost-router prune-db` without losing durable `spend_ledger` economics.
  - Operators with `TenantRole.ADMIN` credentials can immediately restore `OPEN` provider circuits via `POST /v1/finops/circuits/reset` once upstream health recovers.
- **Failure Modes & Mitigations:**
  - **Calling `_AsyncLoopRunner.run()` After `close()`:** `run()` checks `self._closed` under `self._lock`, closes the passed coroutine to avoid unawaited-coroutine warnings, and raises `RuntimeError("Async loop runner is closed")`.
  - **Invalid Retention Window (`max_age_seconds <= 0`, `NaN`, or `inf`):** `prune_older_than()` raises `ValueError` before opening a SQLite transaction, preventing accidental full-table wipes.
  - **Unauthorized Circuit Reset Attempts:** Requests to `POST /v1/finops/circuits/reset` without `TenantRole.ADMIN` are rejected by `_authorize(required_role=TenantRole.ADMIN)` with `401`/`403` before touching `ProviderCircuitBreaker`.
