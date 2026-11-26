# Production Deployment Guide

This guide covers container packaging, Kubernetes probes and graceful shutdown, tenant API key lifecycle management, multi-tenant budget policies, structured access logging, SQLite maintenance, and Prometheus alerting for `cost-aware-model-router`.

---

## 1. Container Deployment

### `Dockerfile`

The repository ships a minimal `python:3.12-slim` image (`Dockerfile`) that:
- Sets `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`.
- Installs the package cleanly (`pip install --no-cache-dir .`).
- Creates and runs as non-root user `appuser` (UID `10001`).
- Exposes port `8080`.
- Configures a container `HEALTHCHECK` against `http://127.0.0.1:8080/healthz`.
- Defaults to `cost-router serve --host 0.0.0.0 --port 8080`.

Build and run locally:

```bash
docker build -t cost-router:latest .
docker run -d --name cost-router -p 8080:8080 cost-router:latest
```

### `docker-compose.yml`

For single-node deployments with durable SQLite persistence and provider circuit breakers:

```bash
docker compose up -d --build
```

`docker-compose.yml` mounts the named volume `cost_router_data` at `/data`, starts `cost-router serve` with `--db-path /data/economics.db --enable-fallback`, sets `restart: unless-stopped`, and configures a `healthcheck` against `/readyz`.

---

## 2. Kubernetes Probes & Graceful `SIGTERM` Shutdown

### Liveness & Readiness Probes

The HTTP server supports both `GET` and `HEAD` on `/healthz` and `/readyz`:

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 15
  timeoutSeconds: 3
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /readyz
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 3
```

- `/healthz` returns `200 OK` (`{"status":"ok"}`) whenever the HTTP server is responsive.
- `/readyz` returns `200 OK` (`{"status":"ready"}`) when ready, or `503 Service Unavailable` if `SQLiteEconomicsStore.is_healthy()` is `False` (`"economics_store_unhealthy"`) or if all `ProviderCircuitBreaker` tiers are `open` (`"all_tier_circuits_open"`).

### Graceful `SIGTERM` Shutdown

`cost-router serve` registers a `SIGTERM` signal handler (`src/cost_aware_model_router/cli.py`) that:
1. Spawns a daemon thread calling `server.shutdown()` so `serve_forever()` exits cleanly without deadlocking the signal handler.
2. Calls `server.server_close()`, which stops and joins the shared `_AsyncLoopRunner` background `asyncio` event loop.
3. Calls `store.close()`, which runs `PRAGMA wal_checkpoint(TRUNCATE)` before closing the SQLite connection.

Set `terminationGracePeriodSeconds: 30` on the Pod spec so Kubernetes allows in-flight requests and WAL checkpointing to complete before `SIGKILL`.

---

## 3. Tenant API Key Lifecycle

`cost-router` stores only non-secret key metadata and SHA-256 digests (`key_sha256`) in the auth registry JSON file. All mutating CLI commands (`issue-key`, `rotate-key`, `revoke-key`) use `_write_auth_config_atomic()`, which writes to a temporary file in the target directory, calls `os.fsync()`, sets `0o600` permissions, and atomically replaces the target with `os.replace()`.

### Issue a Key

```bash
cost-router issue-key \
  --org-id org_acme \
  --role router_client \
  --rate-limit-rps 20.0 \
  --burst-capacity 40 \
  --out /etc/cost-router/keys.json
```

Supported `--role` values: `router_client`, `auditor`, `admin`. Save the printed `token=crk_...` output immediately; raw tokens are never persisted to disk.

### List Keys

```bash
cost-router list-keys --auth-config /etc/cost-router/keys.json
```

### Rotate a Key

Atomically disables the old `key_id` and issues a replacement key with identical `org_id`, `role`, `rate_limit_rps`, and `burst_capacity`:

```bash
cost-router rotate-key --key-id <key_id> --auth-config /etc/cost-router/keys.json
```

### Revoke a Key

Disables an existing key (`enabled=false`) without deleting its audit record:

```bash
cost-router revoke-key --key-id <key_id> --auth-config /etc/cost-router/keys.json
```

Pass `--auth-config /etc/cost-router/keys.json` to `cost-router serve` to enforce authentication.

---

## 4. Multi-Tenant Budget Configuration (`--budget-config`)

Use `--budget-config` to enforce per-tenant and per-agent `AgentBudgetThermostat` policies across multiple organizations. The JSON file may be either a list of `BudgetPolicy` objects or `{"policies": [...]}`:

```json
{
  "policies": [
    {
      "org_id": "org_acme",
      "agent_id": "support_bot",
      "hourly_budget_usd": "2.50000000",
      "soft_downgrade_ratio": 0.80,
      "hard_cap_ratio": 1.00,
      "max_loop_repeats": 3,
      "min_quality_tier": "compact",
      "enable_semantic_cache": true,
      "loop_window_size": 32
    }
  ]
}
```

Start the server with:

```bash
cost-router serve \
  --host 0.0.0.0 \
  --port 8080 \
  --db-path /data/economics.db \
  --auth-config /etc/cost-router/keys.json \
  --budget-config /etc/cost-router/budgets.json \
  --enable-fallback \
  --access-log
```

---

## 5. Structured Access Logging (`--access-log`)

Passing `--access-log` to `cost-router serve` emits one compact JSON line to `stdout` per HTTP response:

```json
{"request_id":"a1b2c3d4...","method":"POST","path":"/v1/chat/completions","status_code":200,"duration_ms":14.821}
```

Default `BaseHTTPRequestHandler` stderr logging is suppressed so log collectors (Fluent Bit, Vector, Datadog) can ingest clean JSON lines from `stdout`.

---

## 6. SQLite Maintenance & Retention (`CronJob`)

`SQLiteEconomicsStore` opens the database in `WAL` mode (`PRAGMA journal_mode=WAL`) and stores `spend_ledger`, `shadow_comparisons`, and `otel_spans`.

### Pruning Old Telemetry (`prune-db`)

Run `cost-router prune-db` on a schedule to bound database growth. It deletes `shadow_comparisons` and `otel_spans` older than `--max-age-hours` (default `168.0` = 7 days) while preserving `spend_ledger` rows so `/v1/finops/economics` totals remain intact:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: cost-router-prune-db
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: prune-db
              image: cost-router:latest
              args:
                - "cost-router"
                - "prune-db"
                - "--db-path"
                - "/data/economics.db"
                - "--max-age-hours"
                - "168"
              volumeMounts:
                - name: data
                  mountPath: /data
```

### Online WAL Backup (`CronJob`)

Because `SQLiteEconomicsStore` operates in `WAL` mode, avoid raw `cp` of `/data/economics.db` while the server is running. Instead, use SQLite's online backup command in a `CronJob` sharing the volume:

```bash
sqlite3 /data/economics.db ".backup '/backups/economics-$(date -u +%Y%m%dT%H%M%SZ).db'"
```

---

## 7. Prometheus Scraping & Alerting

Configure Prometheus to scrape `/metrics` using a Bearer token issued with `--role auditor`:

```yaml
scrape_configs:
  - job_name: cost-router
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/cost_router_auditor_token
    static_configs:
      - targets: ["cost-router:8080"]
```

### Recommended Alerting Rules

```yaml
groups:
  - name: cost-router-alerts
    rules:
      - alert: CostRouterCircuitBreakerOpen
        expr: cost_router_circuit_state == 2
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Provider circuit breaker open for tier {{ $labels.tier }}"
          description: "cost_router_circuit_state{tier=\"{{ $labels.tier }}\"} has been open (2) for >2m."

      - alert: CostRouterHighHourlySpendRate
        expr: sum by (org_id) (increase(cost_router_spend_usd_total[1h])) > 10
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High hourly LLM spend for {{ $labels.org_id }}"
          description: "Accumulated spend over the last 1h exceeded $10 USD for org {{ $labels.org_id }}."
```
