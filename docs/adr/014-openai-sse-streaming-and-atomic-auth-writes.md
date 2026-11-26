# ADR-014: OpenAI-Compatible SSE Streaming and Atomic Auth-Config Writes
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-013`, two operational gaps remained for drop-in OpenAI SDK compatibility and credential-file durability on `cost-router`:

1. **OpenAI-Compatible `stream=True` SSE Framing (`gateway.py`, `server.py`):** OpenAI-compatible SDKs and streaming clients frequently send `"stream": true` to `POST /v1/chat/completions` and expect Server-Sent Events (`Content-Type: text/event-stream; charset=utf-8`) framed as `data: <json>\n\n` `chat.completion.chunk` events terminated by `data: [DONE]\n\n`. Previously, `ChatCompletionRequest` (`extra="forbid"`) rejected `stream`, and `_RouterRequestHandler.do_POST` only emitted `application/json` `chat.completion` responses.
2. **Non-Atomic / World-Readable Auth-Config Writes (`cli.py`):** `cost-router issue-key`, `cost-router revoke-key`, and `cost-router rotate-key` previously persisted `TenantAuthRegistry` JSON via `Path.write_text(...)`. A crash or concurrent read mid-write could leave a truncated `auth-config.json`, and default umask permissions could expose hashed key metadata and rate-limit configurations more broadly than `0o600`.

## Decision
1. **OpenAI-Compatible `stream=True` SSE Framing on `POST /v1/chat/completions` (`gateway.py`, `server.py`):**
   - Accept `stream: bool = False` on `ChatCompletionRequest` while preserving `extra="forbid"`.
   - When `stream=False` (default), `POST /v1/chat/completions` returns the standard `200 OK` `application/json` `chat.completion` payload.
   - When `stream=True`, `_RouterRequestHandler.do_POST` runs the gateway completion through `OpenAIRouterGateway.handle_chat_completion` (preserving `AgentBudgetThermostat`, shadow-mode counterfactuals, `SQLiteEconomicsStore` persistence, and `OTelSpanRecord` emission) and frames the response as `text/event-stream; charset=utf-8` (`Cache-Control: no-store`, `X-Request-Id`, and security headers) emitting OpenAI `chat.completion.chunk` SSE events (`data: {...}\n\n`) followed by `data: [DONE]\n\n`.
2. **Atomic `0o600` Auth-Config Writes via `_write_auth_config_atomic` (`cli.py`):**
   - Route `cost-router issue-key`, `cost-router revoke-key`, and `cost-router rotate-key` file writes through `_write_auth_config_atomic(path, content)`.
   - Create parent directories if needed, write the serialized `TenantAuthRegistry` JSON to a temporary file in the same directory using `os.open(..., 0o600)` (enforcing owner-only `0o600` permissions via `os.chmod` before rename), flush/sync, and atomically replace the target path with `os.replace`.

## Alternatives Considered
- **Rejecting `stream=True` with `400` or Ignoring `stream` and Returning JSON (Rejected):** Returning `application/json` when a client explicitly requests `stream=True` breaks OpenAI SDK streaming parsers (`Stream[ChatCompletionChunk]`) that expect `text/event-stream` `chat.completion.chunk` frames and `data: [DONE]\n\n`.
- **In-Place `Path.write_text()` + Post-Hoc `Path.chmod(0o600)` (Rejected):** Writing in place is not crash-safe and leaves a window between creation and `chmod` where the file has default umask permissions.

## Consequences & Failure Modes
- **Positive:**
  - OpenAI-compatible clients requesting `stream=True` on `POST /v1/chat/completions` receive valid `chat.completion.chunk` SSE frames and `data: [DONE]\n\n` without bypassing thermostat budgets, shadow evaluation, or FinOps telemetry.
  - `auth-config.json` updates from `issue-key`, `revoke-key`, and `rotate-key` are crash-safe, atomic at the filesystem level (`os.replace`), and restricted to `0o600`.
- **Failure Modes & Mitigations:**
  - **Gateway/Validation Failure Before Stream Start:** Request validation errors (`400`), auth/RBAC failures (`401`/`403`), rate limits (`429`), and thermostat circuit-open rejections are returned as standard JSON HTTP errors before any SSE stream begins.
  - **Cross-Device Temporary File Rename:** Creating the temporary file in `path.parent` guarantees same-filesystem `os.replace` semantics and cleans up the temporary file on write failure.
